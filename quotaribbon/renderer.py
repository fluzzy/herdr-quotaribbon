"""Pure, deterministic tab-bar renderer on plain dicts; never raises.

Numbers are shown only for a connected provider verified in the live Herdr server, from a
snapshot that passes the validity rule; anything unproven renders ``"<Name> …"`` or ``--``.
"""

from __future__ import annotations

import math

from . import cache as _cache
from . import constants as C
from . import scheduler as _scheduler

PROVIDERS = ("claude", "codex")
DISPLAY_NAMES = {"claude": "Claude", "codex": "Codex"}
SHORT_NAMES = {"claude": "C", "codex": "X"}
ELLIPSIS = "…"
INNER_SEP = " | "
OUTER_SEP = "  ·  "
COMPACT_INNER_SEP = " "
COMPACT_OUTER_SEP = " · "
MAX_LINE_CHARS = 200


def duration_label(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def reset_label(seconds: float) -> str:
    """Time left until a reset, rounded down: whole days from one day up (``6d``), hours and
    minutes below that (``5h 12m``, ``5h``, ``42m``); never below ``1m``."""
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


RESET_TOLERANCE_SECONDS = 3600


def _reset_suffix(w: dict, now: float) -> str | None:
    """The time left until ``w`` resets, or None when unknown, unparsable, already past, or
    implausibly far (beyond the window's own duration plus a 1h tolerance)."""
    resets = w.get("resets_at")
    if resets is None:
        return None
    d = w.get("duration_seconds")
    if not isinstance(d, int) or isinstance(d, bool) or d <= 0:
        return None
    try:
        left = _cache.parse_iso(resets) - now
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(left) or left <= 0 or left > d + RESET_TOLERANCE_SECONDS:
        return None
    return reset_label(left)


def sanitize_line(text: str) -> str:
    """One line without control characters or Unicode line separators, capped."""
    out = []
    for ch in text:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or ch in "\u2028\u2029":
            continue
        out.append(ch)
    return "".join(out)[:MAX_LINE_CHARS]


def _compact(settings: dict | None) -> bool:
    """``display.format == "compact"``: initials and tight separators."""
    return bool(settings) and settings.get("display", {}).get("format", "full") == "compact"  # type: ignore[union-attr]


def _pv(view: dict, provider: str) -> dict | None:
    return view.get("providers", {}).get(provider)


def hidden_reason_dicts(provider: str, settings: dict | None, view: dict) -> str | None:
    """Why ``provider`` renders nothing, or None when it renders something."""
    if settings is None:
        return "settings_invalid"
    if not settings["providers"][provider]["enabled"]:
        return "disabled"
    pv = _pv(view, provider)
    conn = pv.get("connection") if pv else None
    if conn is None or not conn.get("ever_connected"):
        return "never_connected"
    if conn.get("state") != "connected":
        return f"state:{conn.get('state')}"
    return None


def valid_snapshot_dict(pv: dict, server_epoch: str | None, now: float, hide_after: float, *, rollback: bool = False) -> dict | None:
    snap = pv.get("snapshot")
    conn = pv.get("connection")
    if snap is None or conn is None or rollback:
        return None
    try:
        fetched = _cache.parse_iso(snap["fetched_at"])
    except (ValueError, KeyError, TypeError):
        return None
    if snap.get("provider") != conn.get("provider"):
        return None
    if snap.get("auth_epoch") != conn.get("auth_epoch") or snap.get("profile_key") != conn.get("profile_key"):
        return None
    if fetched > now + C.FUTURE_SKEW_TOLERANCE:
        return None
    # This cutoff also bounds a window without a known reset time.
    if now - fetched >= hide_after:
        return None
    if conn.get("account_key") is not None:
        if snap.get("account_key") != conn["account_key"]:
            return None
    else:
        verified_at = conn.get("verified_at")
        if (
            snap.get("account_key") is not None
            or server_epoch is None
            or snap.get("server_epoch") != server_epoch
            or verified_at is None
            or fetched < verified_at
        ):
            return None
    return snap


def _valid_duration(d: object) -> bool:
    return isinstance(d, int) and not isinstance(d, bool) and d > 0


def _window_value(w: dict, now: float, mode: str = "used") -> str:
    """``used`` shows floor(used %); ``remaining`` shows floor(100 - used %), never overstating what is left."""
    used = w.get("used_percent")
    if used is None or not math.isfinite(used) or not 0 <= used <= 100:
        return "--"
    resets = w.get("resets_at")
    if resets is not None:
        try:
            if _cache.parse_iso(resets) <= now:
                return "--"  # never derive 0% after a reset
        except ValueError:
            return "--"
    if mode == "remaining":
        return f"{math.floor(100 - used)}%"
    return f"{math.floor(used)}%"


_FAILING_STATES = ("transient_error", "rate_limited")


def _failing_suffix(status: dict, now: float) -> str:
    failed = status.get("last_failure_at")
    success = status.get("last_success_at")
    if status.get("data_state") not in _FAILING_STATES or failed is None or (success is not None and failed <= success):
        return ""
    since = success
    if since is None:
        since = status.get("first_failure_at") or failed  # never succeeded: the streak start
    return f" [failing {max(1, int(max(0.0, now - since) // 60))}m]"


def render_provider_dicts(
    provider: str,
    settings: dict | None,
    view: dict,
    now: float,
    fingerprints: dict | None = None,
) -> str | None:
    if hidden_reason_dicts(provider, settings, view) is not None:
        return None
    pv = _pv(view, provider)
    conn = pv["connection"]  # type: ignore[index]
    status = pv.get("status")  # type: ignore[union-attr]
    compact = _compact(settings)
    name = (SHORT_NAMES if compact else DISPLAY_NAMES)[provider]
    server_epoch = view.get("server_epoch")
    fp_ok = True
    if fingerprints is not None:
        fp_ok = fingerprints.get(provider) == pv.get("settings_fingerprint")  # type: ignore[union-attr]
    server_ok = _scheduler.server_verified(conn, view.get("server"))
    if _scheduler.needs_reverify(conn, server_epoch, pv.get("generation", 0), fp_ok, server_ok):  # type: ignore[union-attr]
        return f"{name} {ELLIPSIS}"
    if status and status.get("data_state") == "protocol_unsupported":
        return f"{name} --"
    refresh = settings["refresh"]  # type: ignore[index]
    rollback = (_scheduler.clock_rolled_back(view.get("max_wall_seen"), now)
                or view.get("clock_trusted") is False)  # no trustworthy clock baseline
    snap = valid_snapshot_dict(pv, server_epoch, now, refresh["hide_values_after_seconds"], rollback=rollback)  # type: ignore[arg-type]
    if snap is None:
        if not status or status.get("attempt_started_at") is None or _scheduler.attempt_in_progress(status, now):
            return f"{name} {ELLIPSIS}"
        return f"{name} --" + _failing_suffix(status, now)
    display = settings.get("display", {})  # type: ignore[union-attr]
    mode = display.get("mode", "used")
    show_reset = display.get("show_reset", True) is not False
    windows = [w for w in snap.get("windows", []) if _valid_duration(w.get("duration_seconds"))]
    # A lone window needs no label; two or more keep theirs so the numbers stay unambiguous.
    labels = display.get("window_labels", True) is not False or len(windows) != 1
    parts = []
    for w in windows:
        value = _window_value(w, now, mode)
        part = f"{duration_label(w['duration_seconds'])} {value}" if labels else value
        if show_reset:
            left = _reset_suffix(w, now)
            if left is not None:
                part += f" ({left})"
        parts.append(part)
    if not parts:
        return f"{name} --"
    fetched = _cache.parse_iso(snap["fetched_at"])
    age = now - fetched
    text = f"{name} " + (COMPACT_INNER_SEP if compact else INNER_SEP).join(parts)
    last_failure = status.get("last_failure_at") if status else None
    if age >= refresh["stale_after_seconds"] or (last_failure is not None and last_failure > fetched):
        text += f" [stale {max(1, int(age // 60))}m]"
    return text


def render_dicts(settings: dict | None, view: dict, now: float, fingerprints: dict | None = None) -> str:
    """The whole tab-bar line; ``""`` on any error."""
    try:
        if settings is None:
            return ""
        segments = []
        for p in PROVIDERS:
            seg = render_provider_dicts(p, settings, view, now, fingerprints)
            if seg is not None:
                segments.append(seg)
        return sanitize_line((COMPACT_OUTER_SEP if _compact(settings) else OUTER_SEP).join(segments))
    except Exception:  # noqa: BLE001 - the renderer must never fail the tick
        return ""


def hidden_reason(provider: str, settings, view) -> str | None:
    """Dataclass form of :func:`hidden_reason_dicts` for diagnostics."""
    try:
        s = None if settings is None else settings.to_json()
        v = view.to_json()
    except Exception:  # noqa: BLE001
        return "state_unreadable"
    return hidden_reason_dicts(provider, s, v)


def tick(config_dir: str, state_dir: str, env: dict, now: float) -> dict:
    """Read-only tab-bar tick: the line, due-ness and whether to dispatch. Never raises."""
    result = {"line": "", "due": {}, "dispatch": False, "settings_source": "none"}
    try:
        from . import binding as _binding
        from . import settings as _settings

        s, source = _settings.load_tick(config_dir, state_dir)
        result["settings_source"] = source
        if s is None:
            return result
        bindings = {}
        for p in PROVIDERS:
            if s["providers"][p]["enabled"]:
                try:
                    bindings[p] = _binding.binding_for(p, s, env)
                except (ValueError, OSError):
                    continue
        fps = _settings.settings_fingerprints(s)
        view = _cache.load_view_dicts(state_dir, bindings)
        # Numbers only after verification in this server instance.
        view["server"] = _binding.server_identity(env)
        result["line"] = render_dicts(s, view, now, fps)
        due = _scheduler.work_due_dicts(s, view, now, fps)
        result["due"] = due
        result["dispatch"] = _scheduler.should_dispatch(due, view, now) and _scheduler.lease_free_dict(
            view.get("dispatch"), now)
    except Exception as exc:  # noqa: BLE001 - reported (class name only) by the caller, never printed
        result["error"] = exc.__class__.__name__
    return result


__all__ = [
    "ELLIPSIS",
    "duration_label",
    "reset_label",
    "sanitize_line",
    "hidden_reason_dicts",
    "valid_snapshot_dict",
    "render_provider_dicts",
    "render_dicts",
    "hidden_reason",
    "tick",
]
