"""Secret-free diagnostics report.

No identities, tokens, env values, raw CLI output or provider payloads: every string
from disk passes :func:`_clean` and every output line :func:`_safe_line`. Never starts
a provider CLI.
"""

from __future__ import annotations

import hashlib
import os
import time
import tomllib
from typing import Mapping

from . import BOOTSTRAP_API, PLUGIN_ID, __version__, _core, constants
from .fsutil import CorruptFile, read_json, read_regular
from .log import redact
from .paths import Paths
from .types import DISPLAY_NAMES, PROVIDERS

MAX_STR = 300


def _clean(v: object, limit: int = MAX_STR) -> str | None:
    if v is None:
        return None
    return redact(str(v))[:limit]


def _iso(ts: object) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (OverflowError, ValueError, OSError):
        return None


def _file_sha(path: str) -> str | None:
    try:
        return hashlib.sha256(read_regular(path, 1 << 20)[: 1 << 20]).hexdigest()
    except OSError:
        return None


def _plugin_root(env: Mapping[str, str]) -> str:
    root = env.get("HERDR_PLUGIN_ROOT")
    if root and os.path.isabs(root):
        return root
    return os.path.dirname(os.path.dirname(os.path.realpath(os.path.abspath(__file__))))


def _bootstrap_section(paths: Paths, env: Mapping[str, str]) -> dict:
    root = _plugin_root(env)
    out: dict = {"plugin_id": PLUGIN_ID, "version": __version__, "plugin_root": _clean(root),
                 "bootstrap_api": BOOTSTRAP_API}
    try:
        cfg = read_json(paths.bootstrap_json)
    except CorruptFile:
        cfg = "corrupt"
    if cfg is None:
        out["bootstrap_json"] = "missing (run setup)"
    elif not isinstance(cfg, dict):
        out["bootstrap_json"] = "corrupt"
    else:
        bv = cfg.get("bootstrap_version")
        out["bootstrap_json"] = "ok" if bv == BOOTSTRAP_API else f"version mismatch ({_clean(bv, 20)})"
        py = cfg.get("python")
        out["python"] = _clean(py)
        out["python_ok"] = isinstance(py, str) and os.path.isabs(py) and os.access(py, os.X_OK)
    src = _file_sha(os.path.join(root, "bootstrap", "tabbar.py"))
    dst = _file_sha(paths.tabbar_py)
    if dst is None:
        out["tabbar_copy"] = "missing (run setup)"
    elif src is not None and src != dst:
        out["tabbar_copy"] = "outdated (re-run setup)"
    else:
        out["tabbar_copy"] = "ok"
    out["launcher"] = _launcher_state(paths, cfg.get("python") if isinstance(cfg, dict) else None)
    return out


def _launcher_state(paths: Paths, python: object) -> str:
    """Whether ``tabbar.sh`` (the command Herdr runs) matches what setup would write."""
    sc = _core("setup_config")
    have = _file_sha(sc.wrapper_path(paths.tabbar_py))
    if have is None:
        return "missing (run setup)"
    try:
        want = hashlib.sha256(sc.wrapper_script(python, paths.tabbar_py).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return "unknown (bootstrap.json has no usable python)"
    return "ok" if have == want else "outdated (re-run setup)"


def _herdr_section(paths: Paths, env: Mapping[str, str], live: bool) -> dict:
    bridge = _core("herdr_bridge")
    setup_config = _core("setup_config")
    out: dict = {}
    out["version"] = bridge.herdr_version(dict(env)) if live else None
    try:
        cfg_path = bridge.resolve_config_path(dict(env)) if live else (env.get("HERDR_CONFIG_PATH") or "")
    except Exception:  # noqa: BLE001
        cfg_path = ""
    out["config_path"] = _clean(cfg_path)
    entry_status = "unknown"
    ui: dict = {}
    if cfg_path:
        try:
            doc = tomllib.loads(read_regular(cfg_path, 1 << 20)[: 1 << 20].decode("utf-8"))
            ui = setup_config.inspect_ui(doc, paths.tabbar_py)
            ours = [i for i in ui.get("tab_bar_right", []) if i.get("quotaribbon")]
            entry_status = "present" if ours else "missing (run setup)"
        except FileNotFoundError:
            entry_status = "missing (no config file)"
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            entry_status = "unreadable (config does not parse)"
    out["tab_bar_entry"] = entry_status
    out["ui"] = ui
    try:
        rec = read_json(paths.setup_record)
    except CorruptFile:
        rec = None
    out["setup_record"] = "present" if isinstance(rec, dict) else "absent"
    out["plugin_registered"] = None
    if live and env.get("HERDR_SOCKET_PATH"):
        try:
            entry = bridge.HerdrClient(env["HERDR_SOCKET_PATH"], 2.0).plugin_info(PLUGIN_ID)
            out["plugin_registered"] = "enabled" if bridge.plugin_enabled(entry) else (
                "disabled" if entry else "not registered")
        except bridge.HerdrBridgeError:
            out["plugin_registered"] = "herdr unreachable"
    return out


def _clock_section(paths: Paths, now: float) -> dict:
    """Clock-rollback state; any malformed record is reported as corrupt, never raised."""
    cache = _core("cache")
    status, obj = cache.read_json_status(paths.clock)
    if status == cache.MISSING:
        return {"state": "no record yet"}
    try:
        if status != cache.OK:
            raise ValueError("unreadable")
        mx = cache.validate_clock(obj)["max_wall_seen"]
    except (ValueError, KeyError, TypeError, OverflowError):
        return {"state": "corrupt (treated as unknown)"}
    rollback = now < float(mx) - constants.CLOCK_ROLLBACK_TOLERANCE
    return {"state": "ROLLBACK detected: values hidden until the next refresh" if rollback else "ok",
            "rollback": rollback, "max_wall_seen": _iso(mx)}


def _window_info(w) -> dict:
    from .types import duration_label

    label = None
    if isinstance(getattr(w, "duration_seconds", None), int):
        try:
            label = duration_label(w.duration_seconds)
        except ValueError:
            label = None
    return {"id": _clean(getattr(w, "id", None), 20), "label": label,
            "provided": getattr(w, "used_percent", None) is not None,
            "resets_at": _clean(getattr(w, "resets_at", None), 40)}


def _provider_section(provider: str, settings, view, host, profile_dir: str | None, live: bool) -> dict:
    renderer = _core("renderer")
    pv = view.providers.get(provider) if view is not None else None
    conn = pv.connection if pv is not None else None
    status = pv.status if pv is not None else None
    snap = pv.snapshot if pv is not None else None
    ps = settings.providers.get(provider) if settings is not None else None
    out: dict = {"name": DISPLAY_NAMES[provider], "enabled": bool(ps and ps.enabled)}
    try:
        out["hidden_reason"] = _clean(renderer.hidden_reason(provider, settings, view), 80)
    except Exception as exc:  # noqa: BLE001
        out["hidden_reason"] = f"unknown ({exc.__class__.__name__})"
    out["profile_dir"] = _clean(profile_dir)
    if conn is not None:
        ev = conn.evidence or {}
        out.update({
            "connection_state": conn.state,
            "ever_connected": conn.ever_connected,
            "evidence": {"source": ev.get("source"), "integration_state": ev.get("integration_state"),
                         "observed_at": _iso(ev.get("observed_at"))} if ev else None,
            "auth_category": conn.state,
            "cli_path": _clean(conn.executable),
            "cli_version": _clean(conn.cli_version, 40),
            "env_conflicts": [n for n in conn.env_conflicts if isinstance(n, str)][:20],
            "verified_at": _iso(conn.verified_at),
            "next_verify_at": _iso(conn.next_verify_at),
            "last_reason": _clean(conn.last_reason, 80),
        })
    else:
        out["connection_state"] = "never verified"
    if live and host is not None and ps is not None and ps.enabled and profile_dir:
        try:
            ev = host.evidence(provider, profile_dir)
            out["live_evidence"] = {"linked": ev.linked, "source": ev.source,
                                    "integration_state": ev.integration_state, "agent_detected": ev.agent_detected,
                                    "correspondence": ev.profile_correspondence, "error": ev.error}
            if ev.profile_correspondence == "unknown" and not ev.error:
                out["hint"] = (f"profile_unverified: run `herdr integration install {provider}` for the selected "
                               "profile, or start the agent from Herdr with the default profile")
        except Exception as exc:  # noqa: BLE001
            out["live_evidence"] = {"error": exc.__class__.__name__}
        try:
            exe = host.resolve_executable(provider, ps)
            out["cli_path_resolved"] = _clean(exe) if exe else "not found in trusted PATH entries"
        except Exception as exc:  # noqa: BLE001
            out["cli_path_resolved"] = f"error ({exc.__class__.__name__})"
    if status is not None:
        out.update({
            "data_state": status.data_state,
            "last_success_at": _iso(status.last_success_at),
            "last_failure_at": _iso(status.last_failure_at),
            "consecutive_failures": status.consecutive_failures,
            "next_retry_at": _iso(status.next_due_at),
            "retry_not_before": _iso(status.retry_not_before),
            "last_error_code": _clean(status.last_error_code, 60),
            "unsupported_cli_version": _clean(status.unsupported_cli_version, 40),
            "notes": [_clean(n, 60) for n in status.notes][:20],
        })
        if "managed_policy_present" in status.notes:
            out["managed_policy"] = ("a Claude managed policy defines hooks/policies that would run on session "
                                     "start; automatic collection is disabled (no bypass)")
    else:
        out["data_state"] = "none"
    if snap is not None:
        out["windows"] = [_window_info(w) for w in snap.windows]
        out["fetched_at"] = _clean(snap.fetched_at, 40)
    notes = out.get("notes") or []
    out["windows_not_provided"] = [n.split(":", 1)[1] for n in notes if n and n.startswith("window_not_provided:")]
    return out


def _managed_policy_files() -> list[str]:
    """Existing managed-policy sources; the account name in a per-user plist path is masked."""
    claude = _core("providers.claude")
    prefix = claude.MANAGED_PREFERENCES_DIR + "/"
    out = []
    for path in claude.managed_policy_sources():
        rest = path[len(prefix):] if path.startswith(prefix) else None
        if rest is not None and "/" in rest:
            path = prefix + "<user>/" + rest.rsplit("/", 1)[1]
        out.append(path)
    return out


def build_report(paths: Paths, env: Mapping[str, str], *, now: float | None = None, host=None,
                 live: bool = True) -> dict:
    """Assemble the diagnostics report. Never raises for missing/corrupt state."""
    now = time.time() if now is None else now
    report: dict = {"generated_at": _iso(now), "plugin": {}, "herdr": {}, "settings": {}, "providers": {}}
    try:
        report["plugin"] = _bootstrap_section(paths, env)
    except Exception as exc:  # noqa: BLE001
        report["plugin"] = {"error": exc.__class__.__name__}
    try:
        report["herdr"] = _herdr_section(paths, env, live)
    except Exception as exc:  # noqa: BLE001
        report["herdr"] = {"error": exc.__class__.__name__}
    settings = None
    try:
        loaded = _core("settings").load(paths, update_last_good=False)
        settings = loaded.settings
        report["settings"] = {"valid": loaded.valid, "source": loaded.source,
                              "errors": [_clean(e, 200) for e in loaded.errors][:10]}
    except Exception as exc:  # noqa: BLE001
        report["settings"] = {"valid": False, "source": "none", "errors": [exc.__class__.__name__]}
    try:
        report["clock"] = _clock_section(paths, now)
    except Exception as exc:  # noqa: BLE001
        report["clock"] = {"state": f"unknown ({exc.__class__.__name__})"}
    try:
        # Presence only, from the same check that gates Claude collection.
        report["managed_policy_files"] = _managed_policy_files()
    except Exception as exc:  # noqa: BLE001
        report["managed_policy_files"] = [f"unknown ({exc.__class__.__name__})"]
    view = None
    profiles: dict[str, str | None] = {p: None for p in PROVIDERS}
    if settings is not None:
        try:
            binding = _core("binding")
            bindings = {p: binding.binding_for(p, settings, env) for p in PROVIDERS}
            profiles = {p: bindings[p].profile_dir for p in PROVIDERS}
            report["herdr"]["binding_config_path"] = _clean(bindings[PROVIDERS[0]].herdr_config_path)
            view = _core("cache").load_view(paths, bindings)
        except Exception as exc:  # noqa: BLE001
            report["state_error"] = exc.__class__.__name__
    report["server_epoch"] = "present" if view is not None and view.server_epoch else "missing (refresh will create it)"
    disp = view.dispatch if view is not None else None
    if isinstance(disp, dict):
        held = disp.get("finished_at") is None and now - float(disp.get("dispatched_at") or 0) <= constants.LEASE_TTL
        report["dispatch"] = {"held": held, "dispatched_at": _iso(disp.get("dispatched_at")),
                              "finished_at": _iso(disp.get("finished_at"))}
    else:
        report["dispatch"] = {"held": False}
    if host is None and live and settings is not None:
        try:
            from .connection import LiveHostAdapter

            host = LiveHostAdapter(paths, env, settings)
        except Exception:  # noqa: BLE001
            host = None
    for p in PROVIDERS:
        try:
            report["providers"][p] = _provider_section(p, settings, view, host, profiles.get(p), live)
        except Exception as exc:  # noqa: BLE001
            report["providers"][p] = {"name": DISPLAY_NAMES[p], "error": exc.__class__.__name__}
    _display_section(report, paths, env, now)
    return report


def _display_section(report: dict, paths: Paths, env: Mapping[str, str], now: float) -> None:
    """What the tab bar shows right now, computed exactly like a tick."""
    try:
        renderer, scheduler = _core("renderer"), _core("scheduler")
        binding, cache, settings_mod = _core("binding"), _core("cache"), _core("settings")
        tick = renderer.tick(paths.config_dir, paths.state_dir, dict(env), now)
        report["tab_bar_line"] = _clean(tick.get("line"), 220) or "(empty)"
        s, _src = settings_mod.load_tick(paths.config_dir, paths.state_dir)
        if s is None:
            return
        bindings = {p: binding.binding_for(p, s, env) for p in PROVIDERS if s["providers"][p]["enabled"]}
        view = cache.load_view_dicts(paths.state_dir, bindings)
        server = binding.server_identity(env)
        view["server"] = server
        fps = settings_mod.settings_fingerprints(s)
        for p in PROVIDERS:
            sec = report["providers"].get(p)
            if not isinstance(sec, dict):
                continue
            seg = renderer.render_provider_dicts(p, s, view, now, fps)
            sec["display"] = _clean(seg, 120) if seg else "(hidden)"
            sec["due"] = (tick.get("due") or {}).get(p)
            conn = (view["providers"].get(p) or {}).get("connection")
            sec["verified_in_this_server"] = bool(conn) and scheduler.server_verified(conn, server)
    except Exception as exc:  # noqa: BLE001
        report["display_error"] = exc.__class__.__name__


def _fmt_value(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(_fmt_value(x) for x in v) if v else "(none)"
    if isinstance(v, dict):
        return "; ".join(f"{k}={_fmt_value(x)}" for k, x in v.items())
    return str(v)


MAX_LINE = 600


def _safe_line(text: str) -> str:
    """Output boundary: control chars stripped (one line stays one line), redacted, capped."""
    return redact(text)[:MAX_LINE]


def _ui_lines(v: dict) -> list[str]:
    items = v.get("tab_bar_right")
    items = items if isinstance(items, list) else []
    types = [f"{i.get('type')}{' (QuotaRibbon)' if i.get('quotaribbon') else ''}" for i in items if isinstance(i, dict)]
    warnings = v.get("warnings")
    return [
        f"  tab_bar_right: {_fmt_value(types)}",
        f"  hide_tab_bar_when_single_tab: {_fmt_value(v.get('hide_tab_bar_when_single_tab'))}",
        f"  tab_bar_position: {_fmt_value(v.get('tab_bar_position'))}",
        *(f"  note: {w}" for w in (warnings if isinstance(warnings, list) else [])),
    ]


def format_report(report: dict) -> str:
    """Human-readable text of :func:`build_report`; every line passes :func:`_safe_line`."""
    lines = [f"QuotaRibbon diagnostics ({report.get('generated_at') or '-'})", ""]

    def section(title: str, data: object) -> None:
        lines.append(f"[{title}]")
        if isinstance(data, dict):
            for k, v in data.items():
                if k == "ui" and isinstance(v, dict):
                    lines.extend(_ui_lines(v))
                    continue
                lines.append(f"  {k}: {_fmt_value(v)}")
        else:
            lines.append(f"  {_fmt_value(data)}")
        lines.append("")

    section("tab bar", {"line": report.get("tab_bar_line", "-")})
    section("plugin", report.get("plugin"))
    section("herdr", report.get("herdr"))
    section("settings", report.get("settings"))
    section("clock", report.get("clock"))
    section("state", {"server_epoch": report.get("server_epoch"), "dispatch": report.get("dispatch"),
                      "managed_policy_files": report.get("managed_policy_files") or []})
    for p in PROVIDERS:
        data = report.get("providers", {}).get(p, {})
        section(DISPLAY_NAMES[p], {k: v for k, v in data.items() if k != "name"})
    return "\n".join(_safe_line(line) for line in lines).rstrip() + "\n"


__all__ = ["build_report", "format_report"]
