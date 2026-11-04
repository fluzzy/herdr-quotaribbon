"""Ownership and permission checks for executables QuotaRibbon is about to run.

A path is trusted only when no account but the user or root can replace it or any
component on the way, walked as the kernel does (symlinks followed hop by hop, never
resolved first). Group write is tolerated only for groups whose members are the user or
root (Homebrew's ``admin`` group). On macOS, ACL grants that let others modify a
component also reject it.

``pwd``/``grp``/``subprocess`` are imported lazily to keep imports light.
"""

from __future__ import annotations

import os
import stat
import sys
import threading

MAX_SYMLINK_HOPS = 40
MAX_INTERPRETER_DEPTH = 4
SHEBANG_MAX_BYTES = 256

# macOS splits ``#!`` arguments; Linux passes them as one.
_SHEBANG_SPLITS_ARGS = sys.platform == "darwin"

_GROUP_PRIVATE: dict[int, bool] = {}

_ACL_CHECKS = sys.platform == "darwin"
_LS = "/bin/ls"
_LS_FLAGS = "-ldeqf"  # -f keeps the operands in the order given
_LS_TIMEOUT_S = 5.0
_LS_MAX_OUTPUT = 64 * 1024  # per listed path
_LS_BATCH = 128
_ACL_CACHE: dict[tuple[str, int, int, object], bool] = {}
_ACL_CACHE_MAX = 4096
_ACL_LOCK = threading.Lock()
# ``ls -e`` names that never let the grantee change the entry or its children.
_ACL_HARMLESS = frozenset({
    "read", "list", "execute", "search", "readattr", "readextattr", "readsecurity",
    "file_inherit", "directory_inherit", "limit_inherit", "only_inherit",
})
_DIGITS = frozenset("0123456789")

# (path as reached, lstat)
Component = tuple[str, os.stat_result]


def _trusted_owner(uid: int) -> bool:
    return uid == 0 or uid == os.geteuid()


def _group_is_private(gid: int) -> bool:
    """True when every member of ``gid`` is the user or root.

    An empty member list is not private: on macOS such groups contain every account.
    """
    cached = _GROUP_PRIVATE.get(gid)
    if cached is not None:
        return cached
    ok = False
    try:
        import grp  # noqa: PLC0415
        import pwd  # noqa: PLC0415

        me = pwd.getpwuid(os.geteuid()).pw_name
        allowed = {me, "root"}
        members = set(grp.getgrgid(gid).gr_mem)
        members.update(p.pw_name for p in pwd.getpwall() if p.pw_gid == gid)
        ok = bool(members) and members <= allowed
    except (KeyError, OSError, ImportError):
        ok = False
    _GROUP_PRIVATE[gid] = ok
    return ok


def _owner_principals() -> frozenset[str]:
    names = {"user:root"}
    try:
        import pwd  # noqa: PLC0415

        names.add("user:" + pwd.getpwuid(os.geteuid()).pw_name)
    except (KeyError, OSError, ImportError):
        pass
    return frozenset(names)


def _acl_entry_ok(line: str, owners: frozenset[str]) -> bool:
    """Judge one ``ls -e`` ACL line; anything unexpected is rejected."""
    idx, sep, rest = line.lstrip(" ").partition(": ")
    if not sep or not idx or not set(idx) <= _DIGITS:
        return False
    parts = rest.split()
    if len(parts) == 4 and parts[1] == "inherited":
        del parts[1]
    if len(parts) != 3:
        return False
    principal, kind, perms = parts
    names = perms.split(",")
    if not principal or "" in names:
        return False
    if kind == "deny":
        return True
    if kind != "allow":
        return False
    return principal in owners or set(names) <= _ACL_HARMLESS


def _acl_listing_ok(text: str) -> bool:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return False
    head = lines[0].split(None, 1)
    mode = head[0] if head else ""
    if len(mode) not in (10, 11) or mode[10:] not in ("", "+", "@"):
        return False
    entries = lines[1:]
    if mode[10:] == "+" and not entries:
        return False  # ACL marked but not listed
    if not entries:
        return True
    owners = _owner_principals()
    return all(_acl_entry_ok(line, owners) for line in entries)


def _run_ls(paths: list[str]) -> str | None:
    """``ls`` output or None on any failure. ``-q`` stops a newline in a name forging a line."""
    import subprocess  # noqa: PLC0415

    try:
        proc = subprocess.Popen(
            [_LS, _LS_FLAGS, "--", *paths],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            close_fds=True,
        )
    except (OSError, ValueError):
        return None
    try:
        out, _ = proc.communicate(timeout=_LS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None
    if proc.returncode != 0 or len(out) > _LS_MAX_OUTPUT * len(paths):
        return None
    return out.decode("utf-8", "replace")


def _ls_name(path: str) -> str:
    raw = os.fsencode(path)
    return "".join(chr(b) if 0x20 <= b < 0x7F else "?" for b in raw)


def _split_listing(text: str, paths: list[str]) -> list[str] | None:
    """One block per path, or None unless the blocks line up with ``paths`` exactly."""
    blocks: list[list[str]] = []
    for line in text.split("\n"):
        if line == "":
            continue
        if line.startswith(" "):
            if not blocks:
                return None
            blocks[-1].append(line)
        else:
            blocks.append([line])
    if len(blocks) != len(paths):
        return None
    for block, path in zip(blocks, paths):
        name = " " + _ls_name(path)
        head = block[0]
        if not (head.endswith(name) or (name + " -> ") in head):
            return None
    return ["\n".join(block) + "\n" for block in blocks]


def _ls_acl_listings(paths: list[str]) -> list[str | None]:
    if len(paths) > 1:
        text = _run_ls(paths)
        blocks = _split_listing(text, paths) if text is not None else None
        if blocks is not None:
            return list(blocks)
    return [_run_ls([p]) for p in paths]


def _acl_key(path: str, st: os.stat_result) -> tuple[str, int, int, object]:
    return (path, getattr(st, "st_dev", 0), getattr(st, "st_ino", 0),
            getattr(st, "st_ctime_ns", None) or getattr(st, "st_ctime", None))


def _identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid)


def _components_unchanged(components: list[Component]) -> bool:
    for path, st in components:
        try:
            now = os.lstat(path)
        except (OSError, ValueError):
            return False
        if _identity(now) != _identity(st):
            return False
    return True


def _acls_ok(components: list[Component]) -> bool:
    """No ACL entry lets another account modify any component (always true off macOS)."""
    if not _ACL_CHECKS:
        return True
    keys = list(dict.fromkeys(_acl_key(p, st) for p, st in components))
    with _ACL_LOCK:
        verdicts = {k: _ACL_CACHE[k] for k in keys if k in _ACL_CACHE}
        todo = [k for k in keys if k not in verdicts]
        fresh: dict[tuple[str, int, int, object], bool | None] = {}
        for i in range(0, len(todo), _LS_BATCH):
            chunk = todo[i:i + _LS_BATCH]
            texts = _ls_acl_listings([k[0] for k in chunk])
            for key, text in zip(chunk, texts):
                fresh[key] = None if text is None else _acl_listing_ok(text)
        # An entry may have been swapped between the walk and ``ls``: re-check identities.
        if not _components_unchanged(components):
            return False
        for key, ok in fresh.items():
            if ok is None:
                verdicts[key] = False  # not cached: retried later
                continue
            verdicts[key] = ok
            if len(_ACL_CACHE) >= _ACL_CACHE_MAX:
                _ACL_CACHE.clear()
            _ACL_CACHE[key] = ok
    return all(verdicts.get(k, False) for k in keys)


def _safe_write_bits(st: os.stat_result, *, directory: bool) -> bool:
    mode = st.st_mode
    if directory and mode & stat.S_ISVTX:
        return True
    if mode & stat.S_IWOTH:
        return False
    if mode & stat.S_IWGRP:
        return _group_is_private(st.st_gid)
    return True


def _trusted_dir_entry(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and _trusted_owner(st.st_uid) and _safe_write_bits(st, directory=True)


def _walk(path: str) -> tuple[str, os.stat_result, list[Component]] | None:
    """Walk ``path`` as the kernel does, checking every component (ACLs excluded).

    Returns ``(resolved_path, final_lstat, components)`` or None. May raise OSError.
    """
    root = os.lstat("/")
    if not _trusted_dir_entry(root):
        return None
    seen: list[Component] = [("/", root)]
    pending = [c for c in reversed(path.split("/")) if c]
    cur = "/"
    cur_st = root
    hops = 0
    while pending:
        name = pending.pop()
        if name == ".":
            continue
        if name == "..":
            cur = os.path.dirname(cur)
            cur_st = os.lstat(cur)
            continue
        nxt = os.path.join(cur, name)
        st = os.lstat(nxt)
        if not _trusted_owner(st.st_uid):
            return None
        seen.append((nxt, st))
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                return None
            target = os.readlink(nxt)
            if not target or "\0" in target:
                return None
            if target.startswith("/"):
                cur, cur_st = "/", root
            pending.extend(c for c in reversed(target.split("/")) if c)
            continue
        if stat.S_ISDIR(st.st_mode):
            if not _safe_write_bits(st, directory=True):
                return None
            cur, cur_st = nxt, st
            continue
        if pending:
            return None
        return nxt, st, seen
    return cur, cur_st, seen


def _absolute_path_arg(path: object) -> bool:
    return isinstance(path, str) and bool(path) and "\0" not in path and path.startswith("/")


def _directory_components(path: str) -> list[Component] | None:
    if not _absolute_path_arg(path):
        return None
    try:
        walked = _walk(path)
    except (OSError, ValueError):
        return None
    if walked is None or not _trusted_dir_entry(walked[1]):
        return None
    return walked[2]


def trusted_directory(path: str) -> bool:
    """True when no other account can replace directory ``path``. Never raises."""
    components = _directory_components(path)
    return components is not None and _acls_ok(components)


def trusted_executable(path: str) -> bool:
    """True when no other account can replace executable file ``path``. Never raises."""
    if not _absolute_path_arg(path) or path.endswith("/"):
        return False
    try:
        walked = _walk(path)
        if walked is None:
            return False
        final, st, components = walked
        # Fail closed unless the checked walk arrives where the OS does.
        if os.path.realpath(path) != final:
            return False
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode) or not _trusted_owner(st.st_uid):
        return False
    if not _safe_write_bits(st, directory=False):
        return False
    if not os.access(final, os.X_OK):
        return False
    return _acls_ok(components)


def trusted_path_entries(path_value: str | None) -> list[str]:
    """Trusted absolute entries of a ``PATH`` value, deduplicated, in order. Never raises."""
    if not isinstance(path_value, str) or not path_value:
        return []
    candidates: list[tuple[str, list[Component]]] = []
    seen: set[str] = set()
    for entry in path_value.split(os.pathsep):
        if entry in seen:
            continue
        seen.add(entry)
        components = _directory_components(entry)
        if components is not None:
            candidates.append((entry, components))
    if candidates:
        # One batched ``ls``; the per-entry checks below hit the cache.
        _acls_ok([c for _, components in candidates for c in components])
    return [entry for entry, components in candidates if _acls_ok(components)]


_BLANKS = b" \t"


def _read_head(path: str) -> bytes | None:
    """Head of regular file ``path``, or None. Never blocks on a FIFO."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) < SHEBANG_MAX_BYTES:
            chunk = os.read(fd, SHEBANG_MAX_BYTES - len(data))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def _blank_split(text: bytes) -> list[bytes]:
    return [t for t in text.replace(b"\t", b" ").split(b" ") if t]


def _parse_shebang(data: bytes) -> tuple[bytes, list[bytes]] | None:
    """``(interpreter, arguments)`` as the kernel reads them, or None when unsure."""
    newline = data.find(b"\n")
    if newline < 0:
        if len(data) >= SHEBANG_MAX_BYTES:
            return None
        line = data[2:]
    else:
        line = data[2:newline]
    if b"\0" in line:
        return None
    line = line.lstrip(_BLANKS)
    end = 0
    while end < len(line) and line[end] not in _BLANKS:
        end += 1
    interpreter, rest = line[:end], line[end:]
    if not interpreter:
        return None
    if _SHEBANG_SPLITS_ARGS:
        args = _blank_split(rest)
    else:
        arg = rest.strip(_BLANKS)
        args = [arg] if arg else []
    return interpreter, args


def _is_env(path: str) -> bool:
    return os.path.basename(path) == "env" or os.path.basename(os.path.realpath(path)) == "env"


def _env_command(args: list[bytes]) -> str | None:
    """``NAME`` of ``#!/usr/bin/env NAME``; None for options, assignments or paths."""
    if not args:
        return None
    name = args[0]
    if name.startswith(b"-") or b"=" in name or b"/" in name or b"\r" in name:
        return None
    return os.fsdecode(name)


def _search(name: str, search_path: list[str]) -> str | None:
    """The file ``env`` would run for ``name``.

    Every directory searched up to it must be trusted, or someone could add ``name`` first.
    """
    if not isinstance(search_path, (list, tuple)):
        return None
    for directory in search_path:
        if not _absolute_path_arg(directory) or not trusted_directory(directory):
            return None
        candidate = os.path.join(directory, name)
        try:
            st = os.stat(candidate)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        if stat.S_ISREG(st.st_mode) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _shebang_ok(path: str, search_path: list[str], depth: int) -> bool:
    data = _read_head(path)
    if data is None:
        return False
    if not data.startswith(b"#!"):
        return True
    if depth <= 0:
        return False
    parsed = _parse_shebang(data)
    if parsed is None:
        return False
    raw_interpreter, args = parsed
    if b"\r" in raw_interpreter:
        return False
    interpreter = os.fsdecode(raw_interpreter)
    if not interpreter.startswith("/") or not trusted_executable(interpreter):
        return False
    if not _shebang_ok(interpreter, search_path, depth - 1):
        return False
    if not _is_env(interpreter):
        return True
    name = _env_command(args)
    if name is None:
        return False
    command = _search(name, search_path)
    if command is None or not trusted_executable(command):
        return False
    return _shebang_ok(command, search_path, depth - 1)


def trusted_shebang(path: str, search_path: list[str]) -> bool:
    """True when the ``#!`` interpreter chain of trusted ``path`` is trusted too.

    ``env NAME`` is resolved in ``search_path``; the caller must run the program with
    ``PATH`` set to exactly those directories. Never raises.
    """
    if not _absolute_path_arg(path):
        return False
    try:
        return _shebang_ok(path, search_path, MAX_INTERPRETER_DEPTH)
    except (OSError, ValueError, TypeError):
        return False


__all__ = [
    "MAX_INTERPRETER_DEPTH",
    "MAX_SYMLINK_HOPS",
    "SHEBANG_MAX_BYTES",
    "trusted_directory",
    "trusted_executable",
    "trusted_path_entries",
    "trusted_shebang",
]
