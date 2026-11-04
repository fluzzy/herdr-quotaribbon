"""Stable tab-bar bootstrap: setup copies it to the plugin config dir; Herdr runs it every
few seconds without the plugin environment.

Only the standard library is imported until ``plugin_root`` (from ``plugin.list``) proves no
other account can change the code imported from it. Always exits 0 within a self-timeout,
never prints a traceback, and never blocks on a FIFO.
"""

from __future__ import annotations

try:
    import _socket as socket
    import json
    import os
    import signal
    import stat
    import sys
    import time
except BaseException:  # noqa: BLE001 - a broken interpreter must not print a traceback
    raise SystemExit(0) from None

BOOTSTRAP_VERSION = 1
SELF_TIMEOUT = 1.6
SOCKET_TIMEOUT = 0.5
MAX_LINE_CHARS = 200
MAX_BOOTSTRAP_JSON = 64 << 10
MAX_SOCKET_RESPONSE = 4 << 20
LOG_MAX_BYTES = 256 << 10
LOG_RATE_SECONDS = 300

_REQUIRED_KEYS = ("schema_version", "bootstrap_version", "plugin_id", "state_dir", "config_dir", "python")


def _on_timeout(_signum, _frame):  # pragma: no cover - exercised via subprocess tests
    os._exit(0)


def _arm_timeout() -> None:
    try:
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, SELF_TIMEOUT)
    except (AttributeError, ValueError, OSError):
        pass


def _open_regular(path: str, flags: int) -> int:
    fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_regular(path: str, limit: int) -> bytes:
    fd = _open_regular(path, os.O_RDONLY)
    try:
        chunks = []
        left = limit
        while left > 0:
            chunk = os.read(fd, min(left, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# Logs carry reason codes only, never payloads, env values or paths.


def _log(state_dir: str | None, reason: str, detail: str = "") -> None:
    try:
        if not state_dir or not os.path.isabs(state_dir) or not os.path.isdir(state_dir):
            return
        logs = os.path.join(state_dir, "logs")
        if not os.path.isdir(logs):
            os.mkdir(logs, 0o700)
        path = os.path.join(logs, "bootstrap.log")
        now = time.time()
        reason = "".join(c for c in reason if c.isalnum() or c in "_-:.")[:64]
        detail = "".join(c for c in detail if c.isalnum() or c in "_-:.")[:64]
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            return  # never write through a link, never block on a FIFO
        size = st.st_size if st is not None else 0
        if size:
            fd = _open_regular(path, os.O_RDONLY)
            try:
                os.lseek(fd, max(0, size - 4096), os.SEEK_SET)
                tail = os.read(fd, 8192).decode("utf-8", "replace").splitlines()
            finally:
                os.close(fd)
            for line in reversed(tail):
                parts = line.split(" ", 2)
                if len(parts) >= 2 and parts[1] == reason:
                    try:
                        if now - float(parts[0]) < LOG_RATE_SECONDS:
                            return
                    except ValueError:
                        pass
                    break
        if size > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            os.write(fd, f"{now:.3f} {reason} {detail}\n".encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - logging must never break the tick
        pass


def _load_config(config_dir: str) -> dict | None:
    path = os.path.join(config_dir, "bootstrap.json")
    try:
        data = _read_regular(path, MAX_BOOTSTRAP_JSON + 1)
    except OSError:
        return None
    if len(data) > MAX_BOOTSTRAP_JSON:
        return None
    try:
        cfg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(cfg, dict) or any(k not in cfg for k in _REQUIRED_KEYS):
        return None
    if cfg["schema_version"] != 1 or cfg["bootstrap_version"] != BOOTSTRAP_VERSION:
        return None
    for key in ("schema_version", "bootstrap_version"):
        if type(cfg[key]) is not int:
            return None
    for key in ("plugin_id", "state_dir", "config_dir", "python"):
        if not isinstance(cfg[key], str) or not cfg[key] or "\0" in cfg[key]:
            return None
    if not os.path.isabs(cfg["state_dir"]) or not os.path.isabs(cfg["config_dir"]):
        return None
    return cfg


def _plugin_entry(socket_path: str, plugin_id: str) -> tuple[str, dict | None]:
    """``(status, entry)``; status is ``ok`` / ``missing`` / ``unreachable`` / ``bad``."""
    req = json.dumps(
        {"id": f"qr-tabbar-{os.getpid()}", "method": "plugin.list", "params": {"plugin_id": plugin_id}},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    deadline = time.monotonic() + SOCKET_TIMEOUT
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return "unreachable", None
    try:
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(socket_path)
        sock.sendall(req)
        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "unreachable", None
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_SOCKET_RESPONSE:
                return "bad", None
    except OSError:
        return "unreachable", None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        return "unreachable", None
    try:
        answer = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "bad", None
    if not isinstance(answer, dict) or not isinstance(answer.get("result"), dict):
        return "bad", None
    plugins = answer["result"].get("plugins")
    if not isinstance(plugins, list):
        return "bad", None
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("plugin_id") == plugin_id:
            return "ok", entry
    return "missing", None


def _valid_root(root: object) -> str | None:
    if not isinstance(root, str) or not root or "\0" in root or not os.path.isabs(root):
        return None
    root = os.path.normpath(root)
    if not os.path.isdir(root):
        return None
    if not os.path.isfile(os.path.join(root, "herdr-plugin.toml")):
        return None
    if not os.path.isfile(os.path.join(root, "quotaribbon", "__init__.py")):
        return None
    return root


def _owner_ok(st: os.stat_result, uid: int) -> bool:
    return st.st_uid == uid or st.st_uid == 0


# Trust policy of quotaribbon/trust.py, inlined because nothing may be imported from the plugin
# root before it is trusted: owners are the user or root, no group or other write (an ancestor
# may be shared only with the sticky bit), no symlink below the resolved path, and on macOS no
# ACL entry that lets another account modify any of it. ACL verification that fails or cannot
# be parsed rejects the path.

_ACL_CHECKS = sys.platform == "darwin"
_LS = "/bin/ls"
_LS_FLAGS = "-ldeqf"  # -f keeps the operands in the order given
_LS_TIMEOUT = 1.0  # stays well under SELF_TIMEOUT; a normal listing takes a few ms
_LS_BATCH = 256
_LS_MAX_OUTPUT = 64 << 10  # per listed path
_MAX_TREE_ENTRIES = 4096
# ``ls -e`` names that never let the grantee change the entry or its children.
_ACL_HARMLESS = frozenset({
    "read", "list", "execute", "search", "readattr", "readextattr", "readsecurity",
    "file_inherit", "directory_inherit", "limit_inherit", "only_inherit",
})
_DIGITS = frozenset("0123456789")
# Below a character device nothing can exist: bytecode is then neither read nor written.
_NO_BYTECODE_PREFIX = os.path.join(os.devnull, "quotaribbon-pycache")


def _ancestor_chain(real: str, uid: int) -> list | None:
    """``(path, lstat)`` for every directory above resolved ``real``, or None if one is unsafe."""
    out = []
    cur = os.sep
    names = [p for p in real.split(os.sep) if p][:-1]
    for i in range(len(names) + 1):
        if i:
            cur = os.path.join(cur, names[i - 1])
        st = os.lstat(cur)
        if not stat.S_ISDIR(st.st_mode) or not _owner_ok(st, uid):
            return None
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            return None
        out.append((cur, st))
    return out


def _private_entry(st: os.stat_result, uid: int) -> bool:
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return False  # symlinks, FIFOs, sockets and devices are never part of a trusted tree
    return _owner_ok(st, uid) and not st.st_mode & 0o022


def _private_tree(top: str, uid: int, skip: frozenset = frozenset()) -> list | None:
    """``(path, lstat)`` for ``top`` and everything below it, or None if any entry is unsafe.

    Names in ``skip`` are not descended into: the caller guarantees they are never read.
    """
    st = os.lstat(top)
    if not stat.S_ISDIR(st.st_mode) or not _private_entry(st, uid):
        return None
    out = [(top, st)]
    todo = [top]
    while todo:
        cur = todo.pop()
        with os.scandir(cur) as it:
            for entry in it:
                if entry.name in skip:
                    continue
                path = os.path.join(cur, entry.name)
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue  # removed meanwhile: a missing entry is never read
                if not _private_entry(st, uid):
                    return None
                out.append((path, st))
                if len(out) > _MAX_TREE_ENTRIES:
                    return None
                if stat.S_ISDIR(st.st_mode):
                    todo.append(path)
    return out


def _owner_principals() -> frozenset:
    names = {"user:root"}
    try:
        import pwd  # noqa: PLC0415 - standard library, needed only for the ACL check

        names.add("user:" + pwd.getpwuid(os.geteuid()).pw_name)
    except (KeyError, OSError, ImportError):
        pass
    return frozenset(names)


def _acl_entry_ok(line: str, owners: frozenset) -> bool:
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


def _acl_listing_ok(text: str, owners: frozenset) -> bool:
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
    return all(_acl_entry_ok(line, owners) for line in entries)


def _ls_name(path: str) -> str:
    raw = os.fsencode(path)
    return "".join(chr(b) if 0x20 <= b < 0x7F else "?" for b in raw)


def _split_listing(text: str, paths: list) -> list | None:
    """One block per path, or None unless the blocks line up with ``paths`` exactly."""
    blocks: list = []
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


def _run_ls(paths: list) -> str | None:
    """``ls`` output, or None on any failure. Spawned directly: ``subprocess`` is too heavy here."""
    import select  # noqa: PLC0415 - standard library, needed only for the ACL check

    limit = _LS_MAX_OUTPUT * len(paths)
    r, w = os.pipe()
    pid = 0
    try:
        try:
            pid = os.posix_spawn(
                _LS, [_LS, _LS_FLAGS, "--", *paths], {"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, w, 1),
                    (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                ],
            )
        finally:
            os.close(w)
        deadline = time.monotonic() + _LS_TIMEOUT
        chunks = []
        size = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([r], [], [], left)[0]:
                return None
            chunk = os.read(r, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        _, status = os.waitpid(pid, 0)
        pid = 0
        if os.waitstatus_to_exitcode(status) != 0:
            return None
        return b"".join(chunks).decode("utf-8", "replace")
    except (OSError, ValueError, AttributeError):
        return None
    finally:
        os.close(r)
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass


class _AclCheckFailed(Exception):
    """``ls`` failed or timed out, so the ACLs are unknown; callers still fail closed."""


def _acls_ok(components: list) -> bool:
    """No ACL entry lets another account modify any component (always true off macOS).

    Raises ``_AclCheckFailed`` when ``ls`` itself fails or times out."""
    if not _ACL_CHECKS:
        return True
    paths = list(dict.fromkeys(p for p, _ in components))
    owners = _owner_principals()
    for i in range(0, len(paths), _LS_BATCH):
        chunk = paths[i:i + _LS_BATCH]
        text = _run_ls(chunk)
        if text is None:
            raise _AclCheckFailed
        blocks = _split_listing(text, chunk)
        if blocks is None or not all(_acl_listing_ok(b, owners) for b in blocks):
            return False
    # An entry may have been swapped between the walk and ``ls``.
    for path, st in components:
        now = os.lstat(path)
        if (now.st_dev, now.st_ino, now.st_mode, now.st_uid) != (st.st_dev, st.st_ino, st.st_mode, st.st_uid):
            return False
    return True


def _trusted_root(root: str) -> str | None:
    """The resolved ``root`` when no other account can change the code imported from it, else None."""
    return _check_root(root)[0]


def _check_root(root: str) -> tuple[str | None, str]:
    """``(resolved_root, "")`` when trusted, else ``(None, reason)``.

    The reason is ``acl_check_failed`` when ``ls`` could not verify the ACLs and
    ``untrusted_plugin_root`` otherwise; both reject the root."""
    try:
        real = _trusted_root_or_none(root)
    except _AclCheckFailed:
        return None, "acl_check_failed"
    return real, "" if real is not None else "untrusted_plugin_root"


def _trusted_root_or_none(root: str) -> str | None:
    try:
        uid = os.geteuid()
        real = os.path.realpath(root)
        if not os.path.isabs(real):
            return None
        chain = _ancestor_chain(real, uid)
        if chain is None:
            return None
        st = os.lstat(real)
        if not stat.S_ISDIR(st.st_mode) or not _private_entry(st, uid):
            return None
        # Every file of the package counts (sources, subpackages, sourceless .pyc, extensions);
        # ``__pycache__`` is never read because a pycache prefix is always set before importing.
        tree = _private_tree(os.path.join(real, "quotaribbon"), uid, frozenset({"__pycache__"}))
        if tree is None:
            return None
        if not _acls_ok(chain + [(real, st)] + tree):
            return None
        return real
    except (OSError, ValueError):
        return None


def _bytecode_prefix(state_dir: str) -> tuple[str, bool] | None:
    """``(pycache_prefix, trusted)``: the state cache when no other account can change it, else
    a prefix where nothing can exist. None when even that cannot be established."""
    try:
        st = os.lstat(os.devnull)
        if not stat.S_ISCHR(st.st_mode) or st.st_uid != 0:
            return None
    except OSError:
        return None
    try:
        uid = os.geteuid()
        real = os.path.realpath(state_dir)
        chain = _ancestor_chain(real, uid) if os.path.isabs(real) else None
        if chain is not None:
            st = os.lstat(real)
            if stat.S_ISDIR(st.st_mode) and _private_entry(st, uid):
                pycache = os.path.join(real, "pycache")
                if os.path.lexists(pycache):
                    tree = _private_tree(pycache, uid)
                else:
                    tree = []  # created on demand inside the checked state dir
                if tree is not None and _acls_ok(chain + [(real, st)] + tree):
                    return pycache, True
    except (OSError, ValueError, _AclCheckFailed):
        pass
    return _NO_BYTECODE_PREFIX, False


def _sanitize(line: object) -> str:
    if not isinstance(line, str):
        return ""
    out = []
    for ch in line.replace("\r", " ").replace("\n", " "):
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or ch in "\u2028\u2029":
            continue
        out.append(ch)
    return "".join(out).strip()[:MAX_LINE_CHARS]


def _tick() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = _load_config(here)
    if cfg is None:
        return ""
    state_dir = cfg["state_dir"]
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        _log(state_dir, "no_socket")
        return ""
    status, entry = _plugin_entry(socket_path, cfg["plugin_id"])
    if status != "ok" or entry is None:
        if status != "missing":
            _log(state_dir, "plugin_list_" + status)
        return ""
    if entry.get("enabled") is not True:
        return ""
    root = _valid_root(entry.get("plugin_root"))
    if root is None:
        _log(state_dir, "invalid_plugin_root")
        return ""
    root, reason = _check_root(root)
    if root is None:
        _log(state_dir, reason)
        return ""
    # Bytecode is executable input too: only a cache no other account can change is read.
    prefix = _bytecode_prefix(state_dir)
    if prefix is None:
        _log(state_dir, "no_bytecode_guard")
        return ""
    if not prefix[1]:
        _log(state_dir, "untrusted_pycache")
        sys.dont_write_bytecode = True
    sys.pycache_prefix = prefix[0]
    sys.path.insert(0, root)
    try:
        import quotaribbon  # noqa: PLC0415 - imported from the validated plugin root only

        pkg_file = os.path.realpath(getattr(quotaribbon, "__file__", "") or "")
        if not pkg_file.startswith(os.path.realpath(root).rstrip(os.sep) + os.sep):
            _log(state_dir, "package_outside_root")
            return ""
        if getattr(quotaribbon, "BOOTSTRAP_API", None) != BOOTSTRAP_VERSION:
            _log(state_dir, "bootstrap_api_mismatch")
            return ""
        from quotaribbon import tabbar_entry  # noqa: PLC0415

        return _sanitize(tabbar_entry.run(cfg, socket_path, entry))
    except Exception as exc:  # noqa: BLE001 - the tab bar must never see a traceback
        _log(state_dir, "entry_failed", exc.__class__.__name__)
        return ""


def main() -> None:
    # Stays armed through the stdout write: a stalled reader must not outlive the budget.
    _arm_timeout()
    line = ""
    try:
        line = _tick()
    except BaseException:  # noqa: BLE001
        line = ""
    try:
        if line:
            sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - never a traceback, never a non-zero exit
        try:
            os._exit(0)
        except BaseException:  # noqa: BLE001
            raise SystemExit(0) from None
