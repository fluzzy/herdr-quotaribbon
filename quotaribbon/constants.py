"""Tunable constants; durations are in seconds."""

from __future__ import annotations

# Refresh interval R and its bounds.
DEFAULT_INTERVAL = 120
INTERVAL_MIN = 60
INTERVAL_MAX = 900

# Values turn stale, then are hidden once their age reaches the hide cutoff.
DEFAULT_STALE_AFTER = 300
DEFAULT_HIDE_AFTER = 900
HIDE_AFTER_MAX = 3600

# Choices offered by the "Set refresh interval" action.
INTERVAL_CHOICES = (60, 120, 300, 600)

# Marks a settings key that is absent (distinct from an explicit null).
MISSING = object()


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def derived_refresh(interval: int) -> tuple[int, int]:
    """``(stale_after, hide_after)`` that fit ``interval``: the defaults, raised for long intervals."""
    stale = max(DEFAULT_STALE_AFTER, 2 * interval)
    hide = min(HIDE_AFTER_MAX, max(DEFAULT_HIDE_AFTER, 3 * interval))
    return stale, hide


def resolve_refresh(interval: object, stale: object = MISSING, hide: object = MISSING) -> tuple[object, object]:
    """Fill in ``stale_after`` / ``hide_after`` the user left out.

    Absent values are derived from the interval. The old default pair (300 / 900), which earlier
    versions always wrote out, is treated as absent too, so raising only the interval works.
    Values of the wrong type come back unchanged for the validator to reject.
    """
    if not _is_int(interval) or not 0 < interval <= INTERVAL_MAX:  # type: ignore[operator]
        return (DEFAULT_STALE_AFTER if stale is MISSING else stale,
                DEFAULT_HIDE_AFTER if hide is MISSING else hide)
    d_stale, d_hide = derived_refresh(interval)  # type: ignore[arg-type]
    legacy = _is_int(stale) and _is_int(hide) and (stale, hide) == (DEFAULT_STALE_AFTER, DEFAULT_HIDE_AFTER)
    if legacy or (stale is MISSING and hide is MISSING):
        return d_stale, d_hide
    if stale is MISSING:
        stale = d_stale
    if hide is MISSING:
        if not _is_int(stale):
            return stale, d_hide
        hide = d_hide if d_hide > stale else min(HIDE_AFTER_MAX, stale + interval)  # type: ignore[operator]
    return stale, hide

# Transient-failure backoff: R, 2R, 4R, ... capped at BACKOFF_CAP.
BACKOFF_CAP = 900

# Re-probe cadence for unconnected / logged-out / API-auth / unsupported-protocol states.
VERIFY_RECHECK_SECONDS = 300
# A manual refresh may re-verify at most this often.
MANUAL_MIN_SPACING = 60

# Provider process budgets.
COLLECT_TIMEOUT = 60
TERM_GRACE = 30
# Dispatch lease lifetime; an older unfinished lease is considered abandoned.
LEASE_TTL = COLLECT_TIMEOUT + TERM_GRACE + 10

# Timestamps in the future are invalid: no tolerance.
FUTURE_SKEW_TOLERANCE = 0
# Clock rollback: if now < max_wall_seen - this, every snapshot is invalid until the next refresh.
CLOCK_ROLLBACK_TOLERANCE = 5

# Per provider session; exceeding the stdout budget is a protocol error, stderr is discarded.
RESPONSE_BUDGET_BYTES = 1 << 20
STDERR_BUDGET_BYTES = 64 << 10

# Log rotation.
LOG_MAX_BYTES = 5 << 20
LOG_BACKUPS = 1

# Socket timeout for renderer-side Herdr API calls.
SOCKET_TIMEOUT = 0.5
