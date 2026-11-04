"""Tab-bar tick entry point: render the line and, when due, dispatch ``refresh``.

Runs every few seconds: only ``os``/``time`` at module level, the logger on error paths only.
Errors are logged, never printed (Herdr would show stdout in the tab bar).
"""

from __future__ import annotations

import os
import time

from . import _core

SOCKET_TIMEOUT = 0.5
ERROR_LOG_INTERVAL = 300


def _log_error(state_dir: str, reason: str, exc: BaseException | str | None = None) -> None:
    """Rate-limited log of ``reason`` with the exception's class name only, never its message."""
    try:
        logs = os.path.join(state_dir, "logs")
        marker = os.path.join(logs, f".tabbar-{reason}")
        try:
            if time.time() - os.stat(marker).st_mtime < ERROR_LOG_INTERVAL:
                return
        except OSError:
            pass
        paths = _core("paths").Paths(config_dir=state_dir, state_dir=state_dir)
        logger = _core("log").get_logger(paths)
        name = exc if isinstance(exc, str) else (exc.__class__.__name__ if exc is not None else None)
        if name is not None:
            name = "".join(ch for ch in name if ch.isascii() and (ch.isalnum() or ch in "_."))[:64]
        logger.warning("tabbar %s%s", reason, f" ({name})" if name else "")
        fd = _core("fsutil").open_regular(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.utime(marker, None)
    except Exception:  # noqa: BLE001 - logging never breaks a tick
        pass


def _dirs(boot_cfg: object) -> tuple[str, str] | None:
    if not isinstance(boot_cfg, dict):
        return None
    cfg, state = boot_cfg.get("config_dir"), boot_cfg.get("state_dir")
    if not (isinstance(cfg, str) and os.path.isabs(cfg) and isinstance(state, str) and os.path.isabs(state)):
        return None
    return cfg, state


def render_line(boot_cfg: dict, env=None, now: float | None = None) -> str:
    """Render once without dispatching (``tabbar-preview``)."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    dirs = _dirs(boot_cfg)
    if dirs is None:
        return ""
    line = _core("renderer").tick(dirs[0], dirs[1], env, now).get("line")
    return line if isinstance(line, str) else ""


# HerdrUnavailable messages raised before anything was sent (as is "socket: ...").
_NOT_DELIVERED = ("no socket path", "FileNotFoundError", "ConnectionRefusedError")


# Our own codes for a garbled answer: ambiguous, the invoke may have been accepted.
_OWN_ERROR_CODES = ("invalid_response", "response_too_large", "id_mismatch")


def _not_started(bridge, exc: BaseException) -> bool:
    """True only when the action provably did not start; any failure after the request was
    written is ambiguous (Herdr may already run ``refresh``)."""
    if isinstance(exc, bridge.HerdrError):
        return exc.code not in _OWN_ERROR_CODES
    if not isinstance(exc, bridge.HerdrUnavailable):
        return False
    msg = str(exc)
    return msg in _NOT_DELIVERED or msg.startswith("socket: ")


def _dispatch(state_dir: str, now: float, socket_path: str, plugin_id: str) -> str | None:
    """Take the dispatch lease and start ``refresh``; return the lease id."""
    scheduler = _core("scheduler")
    lease = scheduler.acquire_dispatch_at(state_dir, now)
    if not lease:
        return None
    bridge = _core("herdr_bridge")
    try:
        bridge.HerdrClient(socket_path, SOCKET_TIMEOUT).invoke_refresh(plugin_id, lease)
    except bridge.HerdrBridgeError as exc:
        if _not_started(bridge, exc):
            # The action never started: release our lease so a later tick can retry.
            try:
                scheduler.finish_dispatch_at(state_dir, lease, time.time())
            except Exception:  # noqa: BLE001
                pass
        # Otherwise the lease is kept: a refresh Herdr did start finds it held and runs
        # as the scheduled run (finishing it); if none started it expires (LEASE_TTL).
        _log_error(state_dir, "invoke_failed", exc)
        return None
    return lease


def run(boot_cfg: dict, socket_path: str, plugin_info: dict, now: float | None = None, env=None) -> str:
    """One tick: return the tab-bar line ("" when nothing should be shown)."""
    now = time.time() if now is None else now
    env = os.environ if env is None else env
    dirs = _dirs(boot_cfg)
    if dirs is None:
        return ""
    config_dir, state_dir = dirs
    try:
        res = _core("renderer").tick(config_dir, state_dir, env, now)
    except Exception as exc:  # noqa: BLE001 - tick() never raises; this is a last guard
        _log_error(state_dir, "tick_failed", exc)
        return ""
    if isinstance(res, dict) and isinstance(res.get("error"), str):
        # tick() swallowed an internal error: leave a rate-limited trace.
        _log_error(state_dir, "tick_error", res["error"])
    line = res.get("line") if isinstance(res, dict) else None
    if not isinstance(line, str):
        line = ""
    plugin_id = boot_cfg.get("plugin_id")
    if (
        isinstance(res, dict)
        and res.get("dispatch")
        and isinstance(plugin_info, dict)
        and plugin_info.get("enabled") is True
        and isinstance(plugin_id, str)
    ):
        try:
            _dispatch(state_dir, now, socket_path, plugin_id)
        except Exception as exc:  # noqa: BLE001
            _log_error(state_dir, "dispatch_failed", exc)
    return line


__all__ = ["run", "render_line"]
