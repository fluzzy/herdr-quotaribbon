"""Herdr socket client (newline-delimited JSON) and ``herdr`` CLI helpers.

Imported on the tab-bar tick path: module-level imports stay within the tick budget;
anything heavier is imported lazily inside the CLI helpers.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time

MAX_RESPONSE_BYTES = 4 << 20
CLI_OUTPUT_LIMIT = 256 << 10
CLI_TIMEOUT = 5.0
CLI_KILL_GRACE = 1.0
TABBAR_INVOCATION_SOURCE = "quotaribbon-tabbar"

_COUNTER = [0]


class HerdrBridgeError(Exception):
    """Base error of this module."""


class HerdrUnavailable(HerdrBridgeError):
    """The socket is missing, refused the connection, or timed out."""


class HerdrError(HerdrBridgeError):
    """Herdr error answer, or a malformed/oversized response (``code`` names which)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _next_id() -> str:
    _COUNTER[0] += 1
    return f"qr-{os.getpid()}-{int(time.time() * 1000)}-{_COUNTER[0]}"


class HerdrClient:
    """One connection per request; ``timeout`` bounds connect + send + receive as a whole."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        self.socket_path = socket_path
        self.timeout = float(timeout)

    def request(self, method: str, params: dict | None = None) -> dict:
        """Return the ``result`` object; raise HerdrUnavailable or HerdrError on failure."""
        if not isinstance(self.socket_path, str) or not self.socket_path or "\0" in self.socket_path:
            raise HerdrUnavailable("no socket path")
        req_id = _next_id()
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}, separators=(",", ":"))
        deadline = time.monotonic() + self.timeout
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            raise HerdrUnavailable(f"socket: {exc.__class__.__name__}") from exc
        try:
            try:
                sock.settimeout(max(0.001, self.timeout))
                sock.connect(self.socket_path)
                sock.sendall(payload.encode("utf-8") + b"\n")
                buf = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HerdrUnavailable("timeout")
                    sock.settimeout(remaining)
                    nl = buf.find(b"\n")
                    if nl >= 0:
                        line = bytes(buf[:nl])
                        del buf[: nl + 1]
                        answer = self._decode(line)
                        if answer is None:
                            continue  # blank line
                        return self._result(answer, req_id)
                    chunk = sock.recv(65536)
                    if not chunk:
                        if buf.strip():
                            return self._result(self._decode(bytes(buf)) or {}, req_id)
                        raise HerdrUnavailable("connection closed without a response")
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise HerdrError("response_too_large", f"more than {MAX_RESPONSE_BYTES} bytes")
            except socket.timeout as exc:
                raise HerdrUnavailable("timeout") from exc
            except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError) as exc:
                raise HerdrUnavailable(exc.__class__.__name__) from exc
            except OSError as exc:
                raise HerdrUnavailable(f"{exc.__class__.__name__}:{errno.errorcode.get(exc.errno or 0, '?')}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _decode(line: bytes) -> dict | None:
        text = line.strip()
        if not text:
            return None
        try:
            obj = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise HerdrError("invalid_response", "not JSON") from exc
        if not isinstance(obj, dict):
            raise HerdrError("invalid_response", "not an object")
        return obj

    @staticmethod
    def _result(answer: dict, req_id: str) -> dict:
        if "error" in answer:
            err = answer.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message")
                raise HerdrError(
                    str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else "unknown_error",
                    msg[:200] if isinstance(msg, str) else "",
                )
            raise HerdrError("unknown_error")
        if answer.get("id") not in (req_id, None):
            raise HerdrError("id_mismatch")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise HerdrError("invalid_response", "missing result")
        return result

    def plugin_info(self, plugin_id: str) -> dict | None:
        """``plugin.list`` entry for ``plugin_id``, or None when not registered."""
        result = self.request("plugin.list", {"plugin_id": plugin_id})
        return find_plugin(result, plugin_id)

    def integration_list(self) -> list[dict]:
        items = self.request("integration.list", {}).get("integrations")
        if not isinstance(items, list):
            raise HerdrError("invalid_response", "integrations is not a list")
        return [i for i in items if isinstance(i, dict)]

    def agent_list(self) -> list[dict]:
        items = self.request("agent.list", {}).get("agents")
        if not isinstance(items, list):
            raise HerdrError("invalid_response", "agents is not a list")
        return [i for i in items if isinstance(i, dict)]

    def invoke_refresh(self, plugin_id: str, correlation_id: str) -> dict:
        """Start our ``refresh`` action; the returned record must never reach the tab bar."""
        return self.request(
            "plugin.action.invoke",
            {
                "plugin_id": plugin_id,
                "action_id": "refresh",
                "context": {"invocation_source": TABBAR_INVOCATION_SOURCE, "correlation_id": correlation_id},
            },
        )

    def reload_config(self) -> dict:
        return self.request("server.reload_config", {})


def find_plugin(plugin_list_result: object, plugin_id: str) -> dict | None:
    """``plugin_id``'s entry in a ``plugin.list`` result, or None if absent/malformed."""
    if not isinstance(plugin_list_result, dict):
        return None
    plugins = plugin_list_result.get("plugins")
    if not isinstance(plugins, list):
        return None
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("plugin_id") == plugin_id:
            return entry
    return None


def plugin_enabled(entry: object) -> bool:
    """Fail closed: only an ``enabled`` that is literally ``True`` counts."""
    return isinstance(entry, dict) and entry.get("enabled") is True


# herdr CLI helpers (never on the tick path)


def herdr_executable(env: dict | None = None) -> str | None:
    """Trusted ``HERDR_BIN_PATH``, else ``herdr`` from trusted ``PATH`` entries (provider-CLI trust rules)."""
    from .connection import trusted_executable_ok, trusted_path_lookup  # noqa: PLC0415 - import cycle, tick path

    env = os.environ if env is None else env
    explicit = env.get("HERDR_BIN_PATH")
    if trusted_executable_ok(explicit, env):
        return explicit
    return trusted_path_lookup("herdr", env)


def run_herdr(args: list[str], env: dict | None = None, *, extra_env: dict | None = None,
              timeout: float = CLI_TIMEOUT) -> tuple[int | None, str]:
    """Run ``herdr <args>``; return ``(exit_code, stdout)``, exit_code None on any failure.

    Time and stdout are bounded; the child's process group is killed on overrun.
    """
    from .procutil import run_capture  # noqa: PLC0415 - never on the tick path

    from .connection import child_env_with_path  # noqa: PLC0415 - import cycle, tick path

    env = dict(os.environ if env is None else env)
    exe = herdr_executable(env)
    if exe is None:
        return None, ""
    # The child sees only trusted PATH entries.
    env = child_env_with_path(env)
    if extra_env:
        env.update(extra_env)
    res = run_capture([exe, *args], cwd="/", env=env, timeout_s=timeout, stdout_budget=CLI_OUTPUT_LIMIT,
                      grace_s=CLI_KILL_GRACE)
    if res.spawn_error is not None or res.timed_out or res.overflow or res.returncode is None:
        return None, ""
    return res.returncode, res.stdout.decode("utf-8", "replace")


def herdr_version(env: dict | None = None) -> str | None:
    """``herdr --version`` → ``"X.Y.Z"``, or None."""
    import re

    code, out = run_herdr(["--version"], env)
    if code != 0:
        return None
    m = re.search(r"\b([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\b", out)
    return m.group(1) if m else None


def integration_status_text(env: dict | None = None, *, extra_env: dict | None = None) -> str | None:
    """``herdr integration status`` text for the profile in ``extra_env``, or None."""
    code, out = run_herdr(["integration", "status"], env, extra_env=extra_env)
    if code != 0:
        return None
    return out


_STATE_TOKENS = {"current": "current", "outdated": "outdated", "not installed": "not_installed"}


def parse_integration_states(text: str | None) -> dict[str, str]:
    """``{target: "current"|"outdated"|"not_installed"}``; unrecognised lines are skipped."""
    import re

    out: dict[str, str] = {}
    if not isinstance(text, str):
        return out
    pat = re.compile(r"^([a-z0-9_-]+)(?: \(experimental\))?: (current|outdated|not installed)\b")
    for line in text.splitlines():
        m = pat.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = _STATE_TOKENS[m.group(2)]
    return out


def reload_config(env: dict | None = None, *, socket_timeout: float = 5.0) -> tuple[str, list[str]]:
    """Reload the server config; return ``(status, diagnostics)``.

    ``status``: ``applied``/``partial``/``failed``, ``unreachable`` or ``error:<code>``.
    Uses the socket, or the ``herdr`` CLI when no socket is configured.
    """
    env = os.environ if env is None else env
    sock = env.get("HERDR_SOCKET_PATH")
    result: dict | None = None
    if sock:
        try:
            result = HerdrClient(sock, socket_timeout).reload_config()
        except HerdrUnavailable:
            return "unreachable", []
        except HerdrError as exc:
            return f"error:{exc.code}", []
    else:
        code, out = run_herdr(["server", "reload-config"], dict(env), timeout=15.0)
        if code is None:
            return "unreachable", []
        try:
            obj = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
        except (ValueError, IndexError):
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            result = obj["result"]
        elif isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            return f"error:{obj['error'].get('code')}", []
        else:
            return ("applied" if code == 0 else "error:cli"), []
    status = result.get("status") if isinstance(result, dict) else None
    diags = result.get("diagnostics") if isinstance(result, dict) else None
    if status not in ("applied", "partial", "failed"):
        status = "error:invalid_response"
    diagnostics = [str(d)[:300] for d in diags] if isinstance(diags, list) else []
    return status, diagnostics


def open_plugin_pane(entrypoint: str, env: dict | None = None, *, plugin_id: str | None = None) -> tuple[bool, str | None]:
    """Open a plugin pane; return ``(True, None)`` or ``(False, <error code>)``."""
    env = os.environ if env is None else env
    pid = plugin_id or env.get("HERDR_PLUGIN_ID")
    if not pid:
        from . import PLUGIN_ID

        pid = PLUGIN_ID
    if herdr_executable(dict(env)) is None:
        return False, "herdr_missing"
    code, out = run_herdr(["plugin", "pane", "open", "--plugin", pid, "--entrypoint", entrypoint], dict(env), timeout=10.0)
    if code is None:
        return False, "herdr_failed"
    try:
        obj = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except (ValueError, IndexError):
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        c = obj["error"].get("code")
        return False, str(c) if isinstance(c, str) else "error"
    if code != 0:
        return False, f"exit:{code}"
    return True, None


def resolve_config_path(env: dict | None = None) -> str:
    """Herdr config path: absolute ``HERDR_CONFIG_PATH``, else ``herdr --help``, else the default."""
    env = os.environ if env is None else env
    override = env.get("HERDR_CONFIG_PATH")
    if isinstance(override, str) and override and os.path.isabs(override):
        return os.path.normpath(override)
    code, out = run_herdr(["--help"], dict(env))
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Config:"):
                cand = line[len("Config:"):].strip()
                if cand and os.path.isabs(cand):
                    return os.path.normpath(cand)
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "herdr", "config.toml")


__all__ = [
    "MAX_RESPONSE_BYTES",
    "TABBAR_INVOCATION_SOURCE",
    "HerdrBridgeError",
    "HerdrUnavailable",
    "HerdrError",
    "HerdrClient",
    "find_plugin",
    "plugin_enabled",
    "herdr_executable",
    "run_herdr",
    "herdr_version",
    "integration_status_text",
    "parse_integration_states",
    "reload_config",
    "open_plugin_pane",
    "resolve_config_path",
]
