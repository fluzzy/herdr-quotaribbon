"""Filesystem helpers: atomic private writes, bounded strict JSON reads, flock locks.

Readers never block on a FIFO. Lock files are never deleted: deleting one races with other lockers.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
import tempfile
from typing import Iterator

from .binding import sha256_hex
from .cache import loads_strict

MAX_JSON_BYTES = 1 << 20
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


class CorruptFile(ValueError):
    """A file exists but is unreadable, too large, not UTF-8, or not strict JSON."""


class LockBusy(RuntimeError):
    """A non-blocking :func:`file_lock` could not be acquired."""


def ensure_private_dir(path: str) -> None:
    """Create ``path`` and missing parents 0700; tighten ``path`` if we own it."""
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, PRIVATE_DIR_MODE)
        except FileExistsError:
            if not os.path.isdir(d):
                raise
            continue
        os.chmod(d, PRIVATE_DIR_MODE)  # mkdir's mode is filtered by the umask
    st = os.stat(path)
    if st.st_uid == os.geteuid() and (st.st_mode & 0o777) != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE)


def _fsync_dir(directory: str) -> None:
    """Best-effort, so a completed rename survives a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, data: bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Atomically replace ``path``: a crash leaves the old or the new file, never a partial one."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def atomic_write_text(path: str, text: str, mode: int = PRIVATE_FILE_MODE) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def atomic_write_json(path: str, obj: object, mode: int = PRIVATE_FILE_MODE) -> None:
    """Raises ``ValueError`` before touching the disk for NaN/Infinity or unserialisable data."""
    try:
        text = json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    except TypeError as exc:
        raise ValueError(f"not JSON-serialisable: {exc}") from exc
    atomic_write_text(path, text, mode)


class NotRegularFile(OSError):
    """A path that must be a regular file is a FIFO, device, socket or directory."""


def open_regular(path: str, flags: int = os.O_RDONLY, mode: int = PRIVATE_FILE_MODE) -> int:
    """``os.open`` that never blocks on a FIFO and accepts only regular files (:class:`NotRegularFile`)."""
    fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file", path)
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_regular(path: str, max_bytes: int) -> bytes:
    """Up to ``max_bytes + 1`` bytes of a regular file (FIFO-safe)."""
    fd = open_regular(path)
    try:
        chunks = []
        left = max_bytes + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def special_file(path: str) -> bool:
    """True when ``path`` (following links) is a FIFO, socket or device."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))


def read_json(path: str, max_bytes: int = MAX_JSON_BYTES) -> object | None:
    """Strict bounded JSON read: None when missing, :class:`CorruptFile` for anything unusable."""
    try:
        data = read_regular(path, max_bytes)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CorruptFile(f"{path}: unreadable ({exc.__class__.__name__})") from exc
    if len(data) > max_bytes:
        raise CorruptFile(f"{path}: larger than {max_bytes} bytes")
    try:
        return loads_strict(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CorruptFile(f"{path}: invalid JSON") from exc


@contextlib.contextmanager
def file_lock(path: str, blocking: bool = False) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` for the ``with`` block.

    Non-blocking by default: raises :class:`LockBusy` instead of entering the block.
    Locks are per open file, so two calls on one path exclude each other even in one process.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        ensure_private_dir(parent)
    fd = open_regular(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), PRIVATE_FILE_MODE)
    try:
        op = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, op)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise LockBusy(path) from exc
            raise
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "MAX_JSON_BYTES",
    "PRIVATE_FILE_MODE",
    "PRIVATE_DIR_MODE",
    "CorruptFile",
    "LockBusy",
    "NotRegularFile",
    "open_regular",
    "read_regular",
    "special_file",
    "ensure_private_dir",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
    "loads_strict",
    "read_json",
    "file_lock",
    "sha256_hex",
]
