"""Due / backoff / dispatch-lease policy for usage refreshes, on plain dicts (tick path).

A CLI-stated ``retry_not_before`` is a floor nothing bypasses, manual refresh included.
The lease is taken under non-blocking ``locks/dispatch.lock`` and finished by compare-and-set.
"""

from __future__ import annotations

import json
import os

from . import cache as _cache
from . import constants as C

TABBAR_INVOCATION_SOURCE = "quotaribbon-tabbar"

REASON_CLOCK = "clock_rollback"
REASON_NEVER = "never_verified"
REASON_RECHECK = "verify_recheck"
REASON_MANUAL_VERIFY = "manual_verify"
REASON_REVERIFY = "reverify"
REASON_EVIDENCE = "evidence_recheck"
REASON_USAGE = "usage_due"
REASON_VERSION = "version_recheck"
REASON_MANUAL_REPROBE = "manual_reprobe"
REASON_MANUAL_RETRY = "manual_retry"


def _interval(settings: object) -> int:
    if isinstance(settings, dict):
        return settings["refresh"]["interval_seconds"]
    return settings.interval_seconds  # type: ignore[union-attr]


def _enabled(settings: dict, provider: str) -> bool:
    return bool(settings["providers"][provider]["enabled"])


def clock_rolled_back(max_wall_seen: float | None, now: float) -> bool:
    """The wall clock is behind the highest time any refresh/startup has seen."""
    return max_wall_seen is not None and now < max_wall_seen - C.CLOCK_ROLLBACK_TOLERANCE


def attempt_in_progress(status: dict | None, now: float) -> bool:
    if not status:
        return False
    started = status.get("attempt_started_at")
    if started is None:
        return False
    finished = status.get("attempt_finished_at") or 0
    return started > finished and 0 <= now - started < C.LEASE_TTL


def server_verified(conn: dict | None, server: object) -> bool:
    """True when ``conn`` was verified in the live server instance; an unknown identity fails
    closed, ``server=None`` skips the check."""
    if server is None:
        return True
    if conn is None or not isinstance(server, (tuple, list)) or len(server) != 2:
        return False
    path, ident = server
    if not ident:
        return False
    return (conn.get("verified_servers") or {}).get(path) == ident


def needs_reverify(conn: dict | None, server_epoch: str | None, generation: int, fingerprint_ok: bool = True,
                   server_ok: bool = True) -> bool:
    """True when the connection must be verified again; missing metadata fails closed."""
    if conn is None:
        return True
    return (
        not server_ok
        or
        server_epoch is None
        or conn.get("verified_server_epoch") is None
        or conn.get("verified_server_epoch") != server_epoch
        or conn.get("verified_generation") != generation
        or conn.get("verified_at") is None
        or not fingerprint_ok
    )


def manual_spacing_ok(status: dict | None, now: float) -> bool:
    if not status or status.get("attempt_started_at") is None:
        return True
    return now - status["attempt_started_at"] >= C.MANUAL_MIN_SPACING or now < status["attempt_started_at"]


def manual_retry_ok(status: dict | None, now: float) -> bool:
    """A manual refresh may cut short the transient-failure backoff, never ``retry_not_before``."""
    if not status or (status.get("consecutive_failures") or 0) <= 0:
        return False
    if status.get("data_state") == "protocol_unsupported":
        return False
    rnb = status.get("retry_not_before")
    if rnb is not None and now < rnb:
        return False
    return manual_spacing_ok(status, now)


def evidence_due(conn: dict, now: float) -> bool:
    ev = conn.get("evidence")
    if not ev:
        return True
    observed = ev["observed_at"]
    return now - observed >= C.VERIFY_RECHECK_SECONDS or now < observed


def usage_not_before(status: dict | None) -> float:
    if not status:
        return 0.0
    rnb = status.get("retry_not_before")
    return max(status.get("next_due_at") or 0.0, rnb if rnb is not None else 0.0)


def provider_due(
    pv: dict,
    server_epoch: str | None,
    now: float,
    *,
    fingerprint_ok: bool = True,
    server: object = None,
    rollback: bool = False,
    manual: bool = False,
    force: bool = False,
) -> str | None:
    """Why an enabled provider needs refresh work now, or None.

    ``force`` treats stored ``next_*`` times as reached, but never ``retry_not_before``.
    """
    conn = pv.get("connection")
    status = pv.get("status")
    if rollback:
        return REASON_CLOCK
    if attempt_in_progress(status, now):
        return None
    if conn is None:
        return REASON_NEVER
    verify_ok_time = force or now >= conn["next_verify_at"]
    if conn["state"] != "connected":
        if verify_ok_time:
            return REASON_RECHECK
        if manual and manual_spacing_ok(status, now):
            return REASON_MANUAL_VERIFY
        return None
    if needs_reverify(conn, server_epoch, pv.get("generation", 0), fingerprint_ok, server_verified(conn, server)):
        if verify_ok_time:
            return REASON_REVERIFY
        if manual and manual_spacing_ok(status, now):
            return REASON_MANUAL_VERIFY
        return None
    if evidence_due(conn, now):
        return REASON_EVIDENCE
    if status is None:
        return REASON_USAGE
    rnb = status.get("retry_not_before")
    if status["data_state"] == "protocol_unsupported":
        if rnb is not None and now < rnb:
            return None
        if force or now >= status["next_due_at"]:
            return REASON_VERSION
        if manual and manual_spacing_ok(status, now):
            return REASON_MANUAL_REPROBE
        return None
    if rnb is not None and now < rnb:
        return None
    if force or now >= status["next_due_at"]:
        return REASON_USAGE
    if manual and manual_retry_ok(status, now):
        return REASON_MANUAL_RETRY
    return None


def work_due_dicts(settings: dict | None, view: dict, now: float, fingerprints: dict | None = None) -> dict:
    out: dict = {}
    if settings is None:
        return out
    rollback = clock_rolled_back(view.get("max_wall_seen"), now) or view.get("clock_trusted") is False
    for p, pv in view.get("providers", {}).items():
        if not _enabled(settings, p):
            out[p] = None
            continue
        fp_ok = True
        if fingerprints is not None:
            fp_ok = fingerprints.get(p) == pv.get("settings_fingerprint")
        out[p] = provider_due(pv, view.get("server_epoch"), now, fingerprint_ok=fp_ok, server=view.get("server"),
                              rollback=rollback)
    return out


# Routine work may wait this long so one refresh serves providers that come due together.
ALIGN_WINDOW_SECONDS = 30

_ROUTINE = (REASON_USAGE, REASON_EVIDENCE)


def _routine_times(pv: dict) -> tuple[float | None, float | None]:
    conn = pv.get("connection") or {}
    status = pv.get("status") or {}
    if conn.get("state") != "connected" or status.get("data_state") == "protocol_unsupported":
        return None, None
    usage = usage_not_before(status) if status else None
    ev = conn.get("evidence")
    evidence = (ev["observed_at"] + C.VERIFY_RECHECK_SECONDS) if ev else None
    return usage, evidence


def should_dispatch(due: dict, view: dict, now: float) -> bool:
    """Whether the tab bar should start a refresh now; only routine work waits to align."""
    reasons = {p: r for p, r in due.items() if r}
    if not reasons:
        return False
    if any(r not in _ROUTINE for r in reasons.values()):
        return True
    providers = view.get("providers", {})
    upcoming = []
    for p, pv in providers.items():
        usage, evidence = _routine_times(pv)
        times = [t for t in (usage, evidence) if t is not None]
        if p in reasons:
            reached = [t for t in times if t <= now]
            if not reached or now - min(reached) > ALIGN_WINDOW_SECONDS:
                return True  # overdue (or not explainable): go now
        upcoming.extend(t for t in times if now < t <= now + ALIGN_WINDOW_SECONDS)
    return not upcoming


def backoff_seconds(settings: object, consecutive_failures: int) -> float:
    k = max(1, int(consecutive_failures))
    return float(min(_interval(settings) * 2 ** min(k - 1, 32), C.BACKOFF_CAP))


def next_due_after_success(settings, now: float) -> float:
    return float(now + _interval(settings))


def next_due_after_failure(settings, consecutive_failures: int, now: float, retry_not_before: float | None) -> float:
    due = now + backoff_seconds(settings, consecutive_failures)
    if retry_not_before is not None:
        due = max(due, retry_not_before)
    return float(due)


def lease_free_dict(dispatch: dict | None, now: float) -> bool:
    if not dispatch:
        return True
    finished = dispatch.get("finished_at")
    started = dispatch["dispatched_at"]
    if finished is not None and finished >= started:
        return True
    if started > now + C.CLOCK_ROLLBACK_TOLERANCE:
        return True  # dated in the future: clock rollback, the lease is meaningless
    return now - started > C.LEASE_TTL


def acquire_dispatch_at(state_dir: str, now: float) -> str | None:
    """Acquire the cross-server dispatch lease; return a lease id or None."""
    try:
        fd = _cache.lock_fd(_cache.dispatch_lock_path(state_dir), blocking=False)
    except OSError:
        return None
    if fd is None:
        return None
    try:
        if not lease_free_dict(_cache.read_dispatch_at(state_dir), now):
            return None
        lease_id = os.urandom(12).hex()
        _cache.write_json_atomic(
            _cache.dispatch_path(state_dir), {"id": lease_id, "dispatched_at": float(now), "finished_at": None}
        )
        return lease_id
    except (OSError, ValueError):
        return None
    finally:
        _cache.unlock_fd(fd)


def finish_dispatch_at(state_dir: str, lease_id: str | None, now: float) -> bool:
    """Compare-and-set: mark the lease finished only if it is still ``lease_id``."""
    if not lease_id:
        return False
    try:
        fd = _cache.lock_fd(_cache.dispatch_lock_path(state_dir), blocking=True)
    except OSError:
        return False
    try:
        current = _cache.read_dispatch_at(state_dir)
        if current is None or current["id"] != lease_id or current["finished_at"] is not None:
            return False
        finished = max(float(now), current["dispatched_at"])
        _cache.write_json_atomic(
            _cache.dispatch_path(state_dir),
            {"id": lease_id, "dispatched_at": current["dispatched_at"], "finished_at": finished},
        )
        return True
    except (OSError, ValueError):
        return False
    finally:
        if fd is not None:
            _cache.unlock_fd(fd)


def finish_dispatch(paths, lease_id: str | None, now: float) -> None:
    finish_dispatch_at(paths.state_dir, lease_id, now)


BUSY_OUTCOME = "skipped_busy"


def busy_backoff(outcomes) -> bool:
    """Whether a run must leave its dispatch lease to expire instead of finishing it.

    A provider skipped because its lock is busy writes no status, so it stays due; a
    finished lease would let every tick dispatch again. The unfinished lease holds the
    next tab-bar dispatch off for ``LEASE_TTL``; a manual refresh is not affected.
    """
    return any(getattr(o, "action", None) == BUSY_OUTCOME for o in outcomes or ())


def is_manual_invocation(context_json: str | None, view, now: float) -> bool:
    """True unless dispatched by our tab bar for the lease currently held.

    A stale or forged correlation id counts as manual and never touches the lease.
    """
    try:
        ctx = json.loads(context_json) if context_json else None
    except ValueError:
        return True
    if not isinstance(ctx, dict) or ctx.get("invocation_source") != TABBAR_INVOCATION_SOURCE:
        return True
    corr = ctx.get("correlation_id")
    disp = view.get("dispatch") if isinstance(view, dict) else view.dispatch
    if not isinstance(corr, str) or not disp or disp.get("id") != corr:
        return True
    if disp.get("finished_at") is not None:
        return True
    return not (0 <= now - disp["dispatched_at"] <= C.LEASE_TTL)


__all__ = [
    "TABBAR_INVOCATION_SOURCE",
    "clock_rolled_back",
    "attempt_in_progress",
    "needs_reverify",
    "server_verified",
    "provider_due",
    "work_due_dicts",
    "should_dispatch",
    "ALIGN_WINDOW_SECONDS",
    "backoff_seconds",
    "next_due_after_success",
    "next_due_after_failure",
    "lease_free_dict",
    "acquire_dispatch_at",
    "finish_dispatch_at",
    "finish_dispatch",
    "BUSY_OUTCOME",
    "busy_backoff",
    "is_manual_invocation",
]
