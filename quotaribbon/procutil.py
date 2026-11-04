"""Bounded subprocess helpers for provider CLIs: JSON-lines sessions and one-shot captures.

Child output is budgeted and never appears in exceptions or logs; every child runs in its
own process group and is always reaped.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from . import cache as _cache
from . import constants
from .fsutil import loads_strict

# Must never reach a provider child, or its agent-state hooks report into the user's pane.
HERDR_IDENTITY_VARS: tuple[str, ...] = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")
HERDR_PLUGIN_PREFIX = "HERDR_PLUGIN_"

_READ_CHUNK = 64 << 10


class SessionError(Exception):
    """Base class. Messages are fixed strings and never contain child output."""


ADMISSION_REVOKED = "admission_revoked"


@contextlib.contextmanager
def admission_section(admit):
    """Yield whether the next spawn / message is admitted.

    ``admit`` is None, a callable, or an object whose ``critical()`` holds the settings
    lock for the block, so an OFF toggle cannot land between the check and the spawn or
    write. Keep the block short.
    """
    if admit is None:
        yield True
        return
    critical = getattr(admit, "critical", None)
    if critical is not None:
        with critical() as ok:
            yield ok is True
        return
    try:
        ok = admit() is True
    except Exception:  # noqa: BLE001 - a failing check never admits
        ok = False
    yield ok


SEND_UNDER_ADMISSION_SECONDS = 2.0
"""Upper bound for one message write while admission is held."""


class SessionSpawnError(SessionError):
    """The child could not be started (missing / non-executable binary, bad cwd, ...)."""


class SessionAdmissionRevoked(SessionSpawnError):
    """The spawn guard refused the start (provider turned OFF, plugin gone, ...)."""


class SessionTimeout(SessionError):
    """The deadline passed before the expected message arrived."""


class SessionProtocolError(SessionError):
    """The child wrote something that is not one strict JSON object per line."""


class SessionBudgetExceeded(SessionError):
    """The child wrote more stdout bytes than the session budget allows."""


class SessionExited(SessionError):
    """The child closed stdout (or stdin broke) before the expected message arrived."""


class SessionStderrExceeded(SessionError):
    """The child wrote more stderr bytes than the budget allows (treated as transient)."""


def child_env(base: Mapping[str, str], *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``base`` without Herdr pane/plugin identity variables, plus ``extra``."""
    out = {
        k: v
        for k, v in base.items()
        if k not in HERDR_IDENTITY_VARS and not k.startswith(HERDR_PLUGIN_PREFIX)
    }
    if extra:
        out.update(extra)
    return out


def _signal_group(pgid: int, sig: int) -> bool:
    """Returns False when the group is gone."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Only zombies (or a foreign group after pid reuse) remain.
        return False


def _group_alive(pgid: int) -> bool:
    return _signal_group(pgid, 0)


def _wait_group_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _cleanup_group(pgid: int, grace_s: float) -> None:
    """Terminate any members of ``pgid`` left after the leader was reaped."""
    if not _signal_group(pgid, signal.SIGTERM):
        return
    if _wait_group_gone(pgid, min(grace_s, 5.0)):
        return
    _signal_group(pgid, signal.SIGKILL)
    _wait_group_gone(pgid, 2.0)


# Live children, so a signal handler can terminate every provider process group.
_LIVE_LOCK = threading.Lock()
_LIVE: set = set()


class _CaptureHandle:
    __slots__ = ("pgid",)

    def __init__(self, pgid: int) -> None:
        self.pgid = pgid


# Set under _LIVE_LOCK at shutdown; a child registering afterwards is killed at once.
_CANCELLED = False


class _SpawnCancelled(Exception):
    pass


def _register(obj) -> bool:
    """False when cancellation already started (caller kills the child).

    Same lock as cancellation, so no child slips between a shutdown scan and registration.
    """
    with _LIVE_LOCK:
        _LIVE.add(obj)
        return not _CANCELLED


def cancel_all() -> None:
    global _CANCELLED
    with _LIVE_LOCK:
        _CANCELLED = True


def reset_cancellation() -> None:
    global _CANCELLED
    with _LIVE_LOCK:
        _CANCELLED = False


def cancelled() -> bool:
    with _LIVE_LOCK:
        return _CANCELLED


def shutdown_all(grace_s: float, total_s: float) -> bool:
    """Cancel, then SIGTERM/SIGKILL every registered group until none is alive or
    ``total_s`` passed. Returns True when all are gone; owner threads still reap."""
    cancel_all()
    end = time.monotonic() + max(0.0, total_s)
    termed: set = set()
    while True:
        with _LIVE_LOCK:
            sessions = [s for s in _LIVE if _group_alive(s.pgid)]
        if not sessions:
            return True
        if time.monotonic() >= end:
            return False
        fresh = [s for s in sessions if id(s) not in termed]
        for s in fresh:
            _signal_group(s.pgid, signal.SIGTERM)
            termed.add(id(s))
        if fresh:
            stop = min(end, time.monotonic() + max(0.0, grace_s))
            for s in fresh:
                _wait_group_gone(s.pgid, max(0.0, stop - time.monotonic()))
        for s in sessions:
            _signal_group(s.pgid, signal.SIGKILL)
        time.sleep(0.02)


def live_session_count() -> int:
    with _LIVE_LOCK:
        return len(_LIVE)


# The provider lock stays in the refresh process: a registered lock fd is never inherited,
# so no CLI child or detached descendant can hold it, and closing the fd in the refresh
# releases it. Nothing here signals a process this process did not spawn: a refresh killed
# mid-run leaves its CLI child to exit on its own when its stdin/stdout pipes close.

_OWNER_LOCK = threading.Lock()
_LOCK_FDS: set[int] = set()


def register_lock_owner(lock_fd: int) -> None:
    """Withhold ``lock_fd`` from every child spawned while it is registered."""
    with _OWNER_LOCK:
        _LOCK_FDS.add(lock_fd)


def unregister_lock_owner(lock_fd: int) -> None:
    with _OWNER_LOCK:
        _LOCK_FDS.discard(lock_fd)


def _spawn_fds(pass_fds: Iterable[int]) -> tuple[int, ...]:
    """The fds a child inherits: ``pass_fds`` without registered lock fds."""
    with _OWNER_LOCK:
        return tuple(fd for fd in pass_fds if fd not in _LOCK_FDS)


@dataclass
class SessionLimits:
    timeout_s: float
    grace_s: float = constants.TERM_GRACE
    stdout_budget: int = constants.RESPONSE_BUDGET_BYTES
    stderr_budget: int = constants.STDERR_BUDGET_BYTES


class JsonlSession:
    """One child speaking JSON lines on stdin/stdout; ``close`` always reaps it."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        limits: SessionLimits,
        pass_fds: Iterable[int] = (),
        admit=None,
    ) -> None:
        self.limits = limits
        self._queue: collections.deque[tuple[dict, float]] = collections.deque()
        self._buf = bytearray()
        self._stdout_bytes = 0
        self._stderr_bytes = 0
        self._eof = False
        self._poisoned: SessionError | None = None
        self._closed = False
        self._returncode: int | None = None
        self._sel: selectors.BaseSelector | None = None
        self._err_open = True
        self.last_received_at: float | None = None
        inherited = _spawn_fds(tuple(pass_fds))
        with admission_section(admit) as admitted:
            if not admitted:
                raise SessionAdmissionRevoked("admission revoked") from None
            try:
                self._proc = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=inherited,
                    bufsize=0,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise SessionSpawnError(f"spawn failed ({exc.__class__.__name__})") from None
        self.pid = self._proc.pid
        self.pgid = self._proc.pid
        # Register before any further setup so no live child is ever untracked.
        admitted = _register(self)
        try:
            if not admitted:
                raise _SpawnCancelled
            self._setup()
        except BaseException as exc:
            self._abort_spawn()
            if isinstance(exc, _SpawnCancelled):
                raise SessionAdmissionRevoked("cancelled") from None
            if isinstance(exc, Exception):
                raise SessionSpawnError(f"session setup failed ({exc.__class__.__name__})") from None
            raise

    def _setup(self) -> None:
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            raise OSError("missing pipe")
        self._in_fd = self._proc.stdin.fileno()
        self._out_fd = self._proc.stdout.fileno()
        self._err_fd = self._proc.stderr.fileno()
        os.set_blocking(self._in_fd, False)
        os.set_blocking(self._out_fd, False)
        os.set_blocking(self._err_fd, False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._out_fd, selectors.EVENT_READ, "out")
        self._sel.register(self._err_fd, selectors.EVENT_READ, "err")

    def _abort_spawn(self) -> None:
        """Kill and reap a child whose session setup failed, then unregister it."""
        try:
            _signal_group(self.pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                self._proc.kill()
            with contextlib.suppress(Exception):
                self._proc.wait(timeout=10.0)
            _cleanup_group(self.pgid, 0.5)
        finally:
            self._returncode = self._proc.poll()
            self._closed = True
            if self._sel is not None:
                with contextlib.suppress(Exception):
                    self._sel.close()
            for stream in (self._proc.stdout, self._proc.stderr, self._proc.stdin):
                with contextlib.suppress(Exception):
                    if stream is not None:
                        stream.close()
            with _LIVE_LOCK:
                _LIVE.discard(self)

    def __enter__(self) -> JsonlSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(terminate=exc_type is not None)

    def send(self, obj: dict, deadline: float | None = None) -> None:
        """Write ``obj`` as one JSON line by a monotonic ``deadline``, never blocking.

        Output keeps being drained while stdin is full, so a child that writes before it
        reads cannot deadlock us. Nothing is written to a child whose output is invalid.
        """
        if self._closed or self._proc.stdin is None or self._proc.stdin.closed:
            raise SessionExited("stdin closed")
        if self._poisoned is not None:
            raise self._poisoned
        if deadline is None:
            deadline = time.monotonic() + max(0.0, float(self.limits.timeout_s))
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        view = memoryview(data)
        while view:
            try:
                n = os.write(self._in_fd, view)
            except BlockingIOError:
                n = 0
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                raise SessionExited("stdin broken") from None
            view = view[n:]
            if not view:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionTimeout("write deadline reached")
            self._wait_writable(remaining)
            if self._poisoned is not None:
                raise self._poisoned

    def _wait_writable(self, timeout_s: float) -> None:
        assert self._sel is not None
        try:
            self._sel.register(self._in_fd, selectors.EVENT_WRITE, "in")
        except (KeyError, ValueError, OSError):
            raise SessionExited("stdin broken") from None
        try:
            for key, _ in self._sel.select(timeout=max(0.0, min(timeout_s, 0.5))):
                if key.data == "err":
                    self._drain_stderr()
                elif key.data == "out":
                    self._read_stdout()
        finally:
            with contextlib.suppress(KeyError, ValueError, OSError):
                self._sel.unregister(self._in_fd)

    def close_stdin(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.close()

    def read_until(self, predicate: Callable[[dict], bool], deadline: float) -> dict:
        """Return the first object matching ``predicate`` by a monotonic ``deadline``.

        Nothing from a poisoned stream is ever returned, even if queued before the bad output.
        """
        while True:
            if self._poisoned is not None:
                raise self._poisoned
            while self._queue:
                obj, received_at = self._queue.popleft()
                if predicate(obj):
                    self.last_received_at = received_at
                    return obj
            if self._poisoned is not None:
                raise self._poisoned
            if self._eof:
                raise SessionExited("stdout closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionTimeout("deadline reached")
            self._pump(remaining)

    def finish(self, check: Callable[[dict], None] | None = None) -> int | None:
        """Close gracefully, then pass every unread object to ``check`` (raises to reject)."""
        rc = self.close()
        if self._poisoned is not None:
            raise self._poisoned
        while self._queue:
            obj, _ = self._queue.popleft()
            if check is not None:
                check(obj)
        return rc

    def _pump(self, timeout_s: float) -> None:
        assert self._sel is not None
        events = self._sel.select(timeout=min(timeout_s, 0.5))
        for key, _ in events:
            if key.data == "err":
                self._drain_stderr()
            else:
                self._read_stdout()

    def _drain_stderr(self) -> None:
        if not self._err_open:
            return
        try:
            chunk = os.read(self._err_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._err_open = False
            with contextlib.suppress(KeyError, ValueError):
                self._sel.unregister(self._err_fd)
            return
        self._stderr_bytes += len(chunk)  # content discarded, never logged
        if self._stderr_bytes > self.limits.stderr_budget and self._poisoned is None:
            self._buf.clear()
            self._poisoned = SessionStderrExceeded("stderr budget exceeded")

    def _read_stdout(self) -> None:
        if self._eof:
            return
        try:
            chunk = os.read(self._out_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            with contextlib.suppress(KeyError, ValueError):
                self._sel.unregister(self._out_fd)
            if self._buf and self._poisoned is None:
                tail = bytes(self._buf)
                self._buf.clear()
                self._parse_line(tail)
            return
        if self._poisoned is not None:
            return  # keep draining so the child never blocks, but parse nothing more
        self._stdout_bytes += len(chunk)
        if self._stdout_bytes > self.limits.stdout_budget:
            self._buf.clear()
            self._poisoned = SessionBudgetExceeded("stdout budget exceeded")
            return
        self._buf.extend(chunk)
        while self._poisoned is None:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            self._parse_line(line)

    def _parse_line(self, line: bytes) -> None:
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            return
        try:
            obj = loads_strict(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._poisoned = SessionProtocolError("malformed output line")
            return
        if not isinstance(obj, dict):
            self._poisoned = SessionProtocolError("output line is not a JSON object")
            return
        self._queue.append((obj, time.time()))

    def _drain_once(self, timeout_s: float) -> None:
        if self._eof and not self._err_open:
            time.sleep(min(timeout_s, 0.005))  # pipes closed: the exit is imminent
            return
        assert self._sel is not None
        for key, _ in self._sel.select(timeout=min(timeout_s, 0.05)):
            if key.data == "err":
                self._drain_stderr()
            else:
                self._read_stdout()

    def _wait_exit(self, timeout_s: float, *, polite: bool = False) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if self._proc.poll() is not None:
                return True
            if polite and isinstance(self._poisoned, SessionStderrExceeded):
                return False  # flooding stderr during a graceful close: escalate now
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._drain_once(remaining)

    def _drain_tail(self, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.0, min(timeout_s, 5.0))
        while not self._eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._drain_once(remaining)

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def close(self, *, terminate: bool = False) -> int | None:
        """Close stdin, wait, SIGTERM the group, wait, SIGKILL; always reaps.

        ``terminate=True`` skips the polite wait.
        """
        if self._closed:
            return self._returncode
        grace = max(0.0, float(self.limits.grace_s))
        if cancelled():
            terminate = True  # shutting down: no polite wait
        try:
            self.close_stdin()
            exited = False
            if not terminate:
                exited = self._wait_exit(grace, polite=True)
            if not exited:
                _signal_group(self.pgid, signal.SIGTERM)
                exited = self._wait_exit(grace)
            if not exited:
                _signal_group(self.pgid, signal.SIGKILL)
                exited = self._wait_exit(10.0)
            if not exited:  # pragma: no cover - SIGKILL cannot be ignored
                self._proc.wait()
            _cleanup_group(self.pgid, grace)
            if not terminate:
                self._drain_tail(grace)
        finally:
            self._returncode = self._proc.poll()
            self._closed = True
            if self._sel is not None:
                with contextlib.suppress(Exception):
                    self._sel.close()
            for stream in (self._proc.stdout, self._proc.stderr, self._proc.stdin):
                with contextlib.suppress(Exception):
                    if stream is not None:
                        stream.close()
            with _LIVE_LOCK:
                _LIVE.discard(self)
        return self._returncode


@dataclass(frozen=True)
class CaptureResult:
    """Result of :func:`run_capture`. ``stdout`` is empty when it overflowed."""

    returncode: int | None
    stdout: bytes
    stderr_bytes: int
    timed_out: bool
    overflow: bool
    spawn_error: str | None  # exception class name only, never a message

    @property
    def ok(self) -> bool:
        return self.spawn_error is None and not self.timed_out and not self.overflow


def run_capture(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_s: float,
    stdout_budget: int = 1 << 20,
    grace_s: float = constants.TERM_GRACE,
    pass_fds: Iterable[int] = (),
    admit=None,
    stderr_budget: int = constants.STDERR_BUDGET_BYTES,
) -> CaptureResult:
    """Run ``argv`` with stdin at EOF and a bounded stdout; never raises for child failures."""
    inherited = _spawn_fds(tuple(pass_fds))
    with admission_section(admit) as admitted:
        if not admitted:
            return CaptureResult(None, b"", 0, False, False, ADMISSION_REVOKED)
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                pass_fds=inherited,
                bufsize=0,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return CaptureResult(None, b"", 0, False, False, exc.__class__.__name__)
    pgid = proc.pid
    handle = _CaptureHandle(pgid)
    admitted = _register(handle)
    sel: selectors.BaseSelector | None = None
    try:
        if not admitted:
            raise _SpawnCancelled
        assert proc.stdout is not None and proc.stderr is not None
        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        os.set_blocking(out_fd, False)
        os.set_blocking(err_fd, False)
        sel = selectors.DefaultSelector()
        sel.register(out_fd, selectors.EVENT_READ, "out")
        sel.register(err_fd, selectors.EVENT_READ, "err")
    except BaseException as exc:
        # Never leave a child running or registered after a failed setup.
        _signal_group(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10.0)
        if sel is not None:
            with contextlib.suppress(Exception):
                sel.close()
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()
        with _LIVE_LOCK:
            _LIVE.discard(handle)
        if isinstance(exc, _SpawnCancelled):
            return CaptureResult(proc.poll(), b"", 0, False, False, ADMISSION_REVOKED)
        if isinstance(exc, Exception):
            return CaptureResult(proc.poll(), b"", 0, False, False, exc.__class__.__name__)
        raise
    out = bytearray()
    err_len = 0
    open_fds = {out_fd, err_fd}
    overflow = timed_out = False
    deadline = time.monotonic() + max(0.0, timeout_s)

    def pump(wait_s: float) -> None:
        nonlocal err_len, overflow
        for key, _ in sel.select(timeout=max(0.0, min(wait_s, 0.25))):
            fd = key.fd
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                open_fds.discard(fd)
                with contextlib.suppress(KeyError, ValueError):
                    sel.unregister(fd)
                continue
            if key.data == "err":
                err_len += len(chunk)
                if err_len > stderr_budget and not overflow:
                    overflow = True
                    out.clear()
            elif not overflow:
                if len(out) + len(chunk) > stdout_budget:
                    overflow = True
                    out.clear()
                else:
                    out.extend(chunk)

    try:
        while open_fds or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if overflow:
                break
            if open_fds:
                pump(remaining)
            else:
                time.sleep(min(remaining, 0.005))  # pipes closed: the exit is imminent
        if timed_out or overflow:
            _signal_group(pgid, signal.SIGTERM)
            end = time.monotonic() + max(0.0, grace_s)
            while proc.poll() is None and time.monotonic() < end:
                pump(end - time.monotonic()) if open_fds else time.sleep(0.02)
            if proc.poll() is None:
                _signal_group(pgid, signal.SIGKILL)
                proc.wait()
        _cleanup_group(pgid, grace_s)
    finally:
        with contextlib.suppress(Exception):
            sel.close()
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        if proc.poll() is None:  # pragma: no cover - defensive
            _signal_group(pgid, signal.SIGKILL)
            proc.wait()
        with _LIVE_LOCK:
            _LIVE.discard(handle)
    return CaptureResult(
        returncode=proc.returncode,
        stdout=b"" if overflow else bytes(out),
        stderr_bytes=err_len,
        timed_out=timed_out,
        overflow=overflow,
        spawn_error=None,
    )


class ProfileConflict(Exception):
    """The inherited environment pins the provider to a different profile directory."""


def _realpath_against(value: str, cwd: str) -> str:
    return os.path.realpath(value if os.path.isabs(value) else os.path.join(cwd, value))


def profile_env(
    env: Mapping[str, str],
    var: str,
    profile_dir: str,
    *,
    cwd: str,
    set_when_unset: bool = True,
) -> dict[str, str]:
    """Extra variables that bind a child to ``profile_dir`` through ``var``.

    A user value that resolves elsewhere raises :class:`ProfileConflict`; a matching one
    is passed through unchanged.
    """
    current = env.get(var)
    if current:
        if _realpath_against(current, cwd) != os.path.realpath(profile_dir):
            raise ProfileConflict(var)
        return {}
    return {var: profile_dir} if set_when_unset else {}


def present_vars(env: Mapping[str, str], names: Iterable[str]) -> tuple[str, ...]:
    return tuple(n for n in names if env.get(n))


_VERSION_RE = re.compile(rb"(?<![0-9.])(\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z.]{1,32})?)(?![0-9])")
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE: dict[tuple, str | None] = {}
_VERSION_STORE: list = [None]
_VERSION_STORE_MAX = 8


def configure_version_store(path: str | None) -> None:
    """Persist ``--version`` results across runs in ``path`` (None: memory only)."""
    with _VERSION_CACHE_LOCK:
        _VERSION_STORE[0] = path


def _store_entries(path: str) -> dict:
    status, obj = _cache.read_json_status(path)
    if status != _cache.OK or not isinstance(obj, dict) or obj.get("schema_version") != 1:
        return {}
    entries = obj.get("entries")
    return entries if isinstance(entries, dict) else {}


def _stored_version(key: tuple) -> str | None:
    path = _VERSION_STORE[0]
    if path is None:
        return None
    e = _store_entries(path).get(key[0])
    if not isinstance(e, dict) or [e.get("ino"), e.get("mtime_ns"), e.get("size")] != list(key[1:]):
        return None
    v = e.get("version")
    if not isinstance(v, str) or parse_version_output(v.encode("ascii", "replace")) != v:
        return None
    return v


def _store_version(key: tuple, version: str) -> None:
    path = _VERSION_STORE[0]
    if path is None:
        return
    try:
        entries = _store_entries(path)
        entries.pop(key[0], None)
        entries[key[0]] = {"ino": key[1], "mtime_ns": key[2], "size": key[3], "version": version}
        while len(entries) > _VERSION_STORE_MAX:
            entries.pop(next(iter(entries)))
        _cache.write_json_atomic(path, {"schema_version": 1, "entries": entries})
    except (OSError, ValueError):
        pass


def parse_version_output(data: bytes) -> str | None:
    """First ``X.Y.Z[-pre]`` in ``--version`` output, else None."""
    m = _VERSION_RE.search(data[:4096])
    return m.group(1).decode("ascii") if m else None


def cli_version(
    executable: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_s: float = 10.0,
    pass_fds: Iterable[int] = (),
    admit=None,
) -> str | None:
    """``<executable> --version``, cached per (realpath, inode, mtime_ns, size)."""
    try:
        real = os.path.realpath(executable)
        st = os.stat(real)
    except OSError:
        return None
    key = (real, st.st_ino, st.st_mtime_ns, st.st_size)
    with _VERSION_CACHE_LOCK:
        if key in _VERSION_CACHE:
            return _VERSION_CACHE[key]
        stored = _stored_version(key)
        if stored is not None:
            _VERSION_CACHE[key] = stored
            return stored
    res = run_capture(
        [executable, "--version"],
        cwd=cwd,
        env=env,
        timeout_s=timeout_s,
        stdout_budget=64 << 10,
        grace_s=2.0,
        pass_fds=pass_fds,
        admit=admit,
    )
    version = parse_version_output(res.stdout) if res.ok and res.returncode == 0 else None
    if version is not None:
        with _VERSION_CACHE_LOCK:
            _VERSION_CACHE[key] = version
            _store_version(key, version)
    return version


def clear_version_cache() -> None:
    with _VERSION_CACHE_LOCK:
        _VERSION_CACHE.clear()


__all__ = [
    "HERDR_IDENTITY_VARS",
    "SessionError",
    "SessionSpawnError",
    "SessionAdmissionRevoked",
    "ADMISSION_REVOKED",
    "SEND_UNDER_ADMISSION_SECONDS",
    "admission_section",
    "SessionTimeout",
    "SessionProtocolError",
    "SessionBudgetExceeded",
    "SessionExited",
    "SessionStderrExceeded",
    "SessionLimits",
    "JsonlSession",
    "CaptureResult",
    "run_capture",
    "child_env",
    "ProfileConflict",
    "profile_env",
    "present_vars",
    "cli_version",
    "parse_version_output",
    "clear_version_cache",
    "configure_version_store",
    "register_lock_owner",
    "unregister_lock_owner",
    "live_session_count",
    "cancel_all",
    "reset_cancellation",
    "cancelled",
    "shutdown_all",
]
