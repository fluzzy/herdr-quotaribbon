"""Shared data types, strict JSON validators and protocols.

``from_json`` raises ``ValueError`` on any mismatch (callers treat it as "record absent");
``to_json`` validates first so invalid state is never persisted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Self, runtime_checkable

from . import cache as _cache
from . import constants
from .renderer import duration_label as _format_duration

# In display order.
PROVIDERS: tuple[str, ...] = ("claude", "codex")

DISPLAY_NAMES: Mapping[str, str] = {"claude": "Claude", "codex": "Codex"}

SCHEMA_VERSION = 1

# Claude: five_hour/seven_day; Codex: primary/secondary.
WINDOW_IDS: frozenset[str] = frozenset({"five_hour", "seven_day", "primary", "secondary"})

# Only account-wide windows; model-specific buckets are never emitted.
WINDOW_SCOPES: frozenset[str] = frozenset({"all"})

AUTH_STATES: frozenset[str] = frozenset(
    {"subscription", "logged_out", "api_auth", "unsupported_auth", "profile_mismatch", "error"}
)

COLLECT_OUTCOMES: frozenset[str] = frozenset(
    {
        "ok",
        "transient_error",
        "auth_error",
        "protocol_unsupported",
        "unavailable",
        "rate_limited",
        "identity_changed",
    }
)

# The single allowed data source per provider.
PROVIDER_SOURCES: Mapping[str, str] = {
    "claude": "claude-control-get-usage",
    "codex": "codex-app-server-rate-limits",
}

SOURCES: frozenset[str] = frozenset(PROVIDER_SOURCES.values())

EVIDENCE_SOURCES: frozenset[str] = frozenset({"integration", "agent"})
INTEGRATION_STATES: frozenset[str] = frozenset({"not_installed", "current", "outdated"})
# How Herdr's evidence relates to the selected profile; only match/inferred connect.
PROFILE_CORRESPONDENCE: frozenset[str] = frozenset({"match", "inferred", "mismatch", "unknown"})

CONNECTION_STATES: frozenset[str] = frozenset(
    {
        "unverified",
        "connected",
        "not_connected",
        "logged_out",
        "api_auth",
        "unsupported_auth",
        "profile_mismatch",
        "cli_missing",
    }
)

DATA_STATES: frozenset[str] = frozenset(
    {"none", "ok", "transient_error", "protocol_unsupported", "unavailable", "rate_limited"}
)

SNAPSHOT_STATUSES: frozenset[str] = frozenset({"ok"})
DISPLAY_MODES: frozenset[str] = frozenset({"used", "remaining"})
DISPLAY_FORMATS: frozenset[str] = frozenset({"full", "compact"})

EVIDENCE_RECORD_KEYS: tuple[str, ...] = ("source", "integration_state", "observed_at")

# finished_at is None while the lease is held.
DISPATCH_KEYS: tuple[str, ...] = ("id", "dispatched_at", "finished_at")

# Provider-supplied ids are never copied into notes; notes carry a count instead.
NOTE_CODES: frozenset[str] = _cache.NOTE_CODES

CONTROL_PROVIDER_KEYS: tuple[str, ...] = ("generation", "settings_fingerprint")

# CollectResult.error_code when a collector stopped because admit() returned False.
ADMISSION_REVOKED = "admission_revoked"

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# ASCII digits only: int() would otherwise accept full-width and other Unicode digits.
_ISO_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


# Larger JSON integers would overflow float conversion; they are invalid input.
MAX_JSON_INT = 1 << 53


def is_finite_number(v: object) -> bool:
    """True for a non-bool ``int`` within ``±MAX_JSON_INT`` or a finite ``float``; never raises."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -MAX_JSON_INT <= v <= MAX_JSON_INT
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def iso_utc(ts: float) -> str:
    """Format Unix seconds as ``YYYY-MM-DDTHH:MM:SSZ`` (fraction truncated)."""
    if not is_finite_number(ts):
        raise ValueError("invalid timestamp")
    dt = datetime.fromtimestamp(math.floor(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc_precise(ts: float) -> str:
    """Format Unix seconds as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``; round-trips with
    :func:`parse_iso_utc` to the microsecond."""
    if not is_finite_number(ts):
        raise ValueError("invalid timestamp")
    seconds, micros = divmod(round(ts * 1_000_000), 1_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{micros:06d}Z"


def parse_iso_utc(s: str) -> float:
    """Parse ISO-8601 with ``Z`` or ``±HH:MM`` into Unix seconds; anything else raises ``ValueError``."""
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string")
    m = _ISO_RE.match(s)
    if m is None:
        raise ValueError("invalid ISO-8601 UTC timestamp")
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    frac, offset = m.group(7), m.group(8)
    if offset == "Z":
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            raise ValueError("invalid UTC offset")
        tz = timezone(sign * timedelta(hours=oh, minutes=om))
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError as exc:
        raise ValueError("invalid ISO-8601 UTC timestamp") from exc
    whole = int(dt.timestamp())
    if not frac:
        return float(whole)
    digits = frac[1:]
    return (whole * 10 ** len(digits) + int(digits)) / 10 ** len(digits)


def is_valid_percent(x: object) -> bool:
    return is_finite_number(x) and 0 <= x <= 100


def duration_label(seconds: int) -> str:
    """Tab-bar label for a window duration; raises ``ValueError`` for non-positive input."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("invalid duration")
    return _format_duration(seconds)


def raw_percent_or_none(value: object) -> float | int | None:
    """``Window.raw_used_percent``: only finite numbers; other untrusted provider data is never persisted."""
    return value if is_finite_number(value) else None


def make_note(code: str, count: int | None = None) -> str:
    """Build ``code`` or ``"{code}:{count}"`` from ``NOTE_CODES``; raises ``ValueError`` otherwise."""
    if not isinstance(code, str) or code not in NOTE_CODES:
        raise ValueError("unknown note code")
    if count is None:
        return code
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("invalid note count")
    return f"{code}:{count}"


is_valid_note = _cache.is_valid_note


def _obj(d: object, name: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise ValueError(f"{name}: expected object, got {type(d).__name__}")
    for k in d:
        if not isinstance(k, str):
            raise ValueError(f"{name}: non-string key")
    return d


def _keys(d: dict[str, Any], name: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    missing = [k for k in required if k not in d]
    if missing:
        raise ValueError(f"{name}: missing keys {missing}")
    allowed = set(required) | set(optional)
    unknown = sorted(k for k in d if k not in allowed)
    if unknown:
        raise ValueError(f"{name}: unknown key")


def _str(v: object, name: str, *, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{name}: expected string, got {type(v).__name__}")
    return v


def _bool(v: object, name: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{name}: expected bool, got {type(v).__name__}")
    return v


def _int(v: object, name: str, *, nullable: bool = False, minimum: int | None = None) -> int | None:
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{name}: expected int, got {type(v).__name__}")
    if not -MAX_JSON_INT <= v <= MAX_JSON_INT:
        raise ValueError(f"{name}: integer out of range")
    if minimum is not None and v < minimum:
        raise ValueError(f"{name}: must be >= {minimum}, got {v}")
    return v


def _float(v: object, name: str, *, nullable: bool = False) -> float | None:
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name}: expected number, got {type(v).__name__}")
    if not is_finite_number(v):
        raise ValueError(f"{name}: must be finite and in range")
    return float(v)


def _enum(v: object, name: str, allowed: frozenset[str] | tuple[str, ...], *, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str) or v not in allowed:
        raise ValueError(f"{name}: unknown value")
    return v


def _iso(v: object, name: str, *, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    s = _str(v, name)
    parse_iso_utc(s)  # raises ValueError
    return s


def _str_seq(v: object, name: str, *, env_names: bool = False) -> list[str]:
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"{name}: expected list, got {type(v).__name__}")
    out = []
    for i, item in enumerate(v):
        s = _str(item, f"{name}[{i}]")
        if env_names and not _ENV_NAME_RE.match(s):
            raise ValueError(f"{name}[{i}]: not an environment variable name")
        out.append(s)
    return out


def _notes(v: object, name: str) -> list[str]:
    out = _str_seq(v, name)
    for i, note in enumerate(out):
        if not is_valid_note(note):
            raise ValueError(f"{name}[{i}]: not a known note code")
    return out


MAX_VERIFIED_SERVERS = 8

_SERVER_IDENTITY_RE = re.compile(r"[0-9]{1,24}:[0-9]{1,24}:[0-9]{1,24}\Z")


def _verified_servers(v: object, name: str) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"{name}: expected object")
    if len(v) > MAX_VERIFIED_SERVERS:
        raise ValueError(f"{name}: too many entries")
    for k, ident in v.items():
        if not isinstance(k, str) or not k.startswith("/") or len(k) > 1024 or "\0" in k:
            raise ValueError(f"{name}: invalid socket path")
        if not isinstance(ident, str) or not _SERVER_IDENTITY_RE.match(ident):
            raise ValueError(f"{name}: invalid server identity")
    return dict(v)


def _schema(v: object, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v != SCHEMA_VERSION:
        raise ValueError(f"{name}: unsupported schema_version")
    return v


def _raw_percent(v: object, name: str) -> float | int | None:
    if v is None:
        return None
    if not is_finite_number(v):
        raise ValueError(f"{name}: must be a finite number or null")
    return v


@dataclass(frozen=True)
class Window:
    """One account-wide usage window; ``used_percent`` is None when unusable, never clamped."""

    id: str
    scope: str
    duration_seconds: int | None
    used_percent: float | None
    raw_used_percent: float | int | None
    resets_at: str | None

    _FIELDS = ("id", "scope", "duration_seconds", "used_percent", "raw_used_percent", "resets_at")

    def validate(self) -> None:
        _enum(self.id, "window.id", WINDOW_IDS)
        _enum(self.scope, "window.scope", WINDOW_SCOPES)
        _int(self.duration_seconds, "window.duration_seconds", nullable=True, minimum=1)
        if self.used_percent is not None:
            _float(self.used_percent, "window.used_percent")
            if not is_valid_percent(self.used_percent):
                raise ValueError("window.used_percent: out of range")
        _raw_percent(self.raw_used_percent, "window.raw_used_percent")
        _iso(self.resets_at, "window.resets_at", nullable=True)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "scope": self.scope,
            "duration_seconds": self.duration_seconds,
            "used_percent": None if self.used_percent is None else float(self.used_percent),
            "raw_used_percent": self.raw_used_percent,
            "resets_at": self.resets_at,
        }

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "window")
        _keys(o, "window", cls._FIELDS)
        used = _float(o["used_percent"], "window.used_percent", nullable=True)
        w = cls(
            id=o["id"],
            scope=o["scope"],
            duration_seconds=o["duration_seconds"],
            used_percent=used,
            raw_used_percent=o["raw_used_percent"],
            resets_at=o["resets_at"],
        )
        w.validate()
        return w


@dataclass(frozen=True)
class AuthResult:
    """Result of an authentication check; only ``subscription`` allows usage to be shown.

    ``account_key`` is a salted hash (None: identity unproven); ``detail`` is a machine
    code, never a secret. ``retryable`` failures must never change a connection's state.
    """

    state: str
    account_key: str | None
    config_dir: str | None
    detail: str | None
    retryable: bool
    env_conflicts: tuple[str, ...] = ()

    _FIELDS = ("state", "account_key", "config_dir", "detail", "retryable", "env_conflicts")

    def validate(self) -> None:
        _enum(self.state, "auth.state", AUTH_STATES)
        _str(self.account_key, "auth.account_key", nullable=True)
        _str(self.config_dir, "auth.config_dir", nullable=True)
        _str(self.detail, "auth.detail", nullable=True)
        _bool(self.retryable, "auth.retryable")
        if not isinstance(self.env_conflicts, tuple):
            raise ValueError("auth.env_conflicts: expected tuple")
        _str_seq(self.env_conflicts, "auth.env_conflicts", env_names=True)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "state": self.state,
            "account_key": self.account_key,
            "config_dir": self.config_dir,
            "detail": self.detail,
            "retryable": self.retryable,
            "env_conflicts": list(self.env_conflicts),
        }

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "auth")
        _keys(o, "auth", cls._FIELDS)
        r = cls(
            state=o["state"],
            account_key=o["account_key"],
            config_dir=o["config_dir"],
            detail=o["detail"],
            retryable=o["retryable"],
            env_conflicts=tuple(_str_seq(o["env_conflicts"], "auth.env_conflicts", env_names=True)),
        )
        r.validate()
        return r


@dataclass(frozen=True)
class CollectResult:
    """Outcome of one ``Collector.collect`` call.

    ``retry_not_before`` is set only when the CLI states it, never guessed. Snapshot age
    is measured from ``usage_received_at`` (response arrival), which an ``ok`` result must carry.
    """

    outcome: str
    windows: tuple[Window, ...]
    auth_before: AuthResult | None
    auth_after: AuthResult | None
    source: str
    source_observed_at: str | None
    error_code: str | None
    retry_not_before: float | None
    cli_version: str | None
    notes: tuple[str, ...] = ()
    usage_received_at: float | None = None

    _FIELDS = (
        "outcome",
        "windows",
        "auth_before",
        "auth_after",
        "source",
        "source_observed_at",
        "error_code",
        "retry_not_before",
        "cli_version",
        "notes",
        "usage_received_at",
    )

    def validate(self) -> None:
        _enum(self.outcome, "collect.outcome", COLLECT_OUTCOMES)
        if not isinstance(self.windows, tuple):
            raise ValueError("collect.windows: expected tuple")
        for w in self.windows:
            if not isinstance(w, Window):
                raise ValueError("collect.windows: expected Window items")
            w.validate()
        for name in ("auth_before", "auth_after"):
            a = getattr(self, name)
            if a is not None:
                if not isinstance(a, AuthResult):
                    raise ValueError(f"collect.{name}: expected AuthResult")
                a.validate()
        _enum(self.source, "collect.source", SOURCES)
        _iso(self.source_observed_at, "collect.source_observed_at", nullable=True)
        _str(self.error_code, "collect.error_code", nullable=True)
        _float(self.retry_not_before, "collect.retry_not_before", nullable=True)
        _str(self.cli_version, "collect.cli_version", nullable=True)
        if not isinstance(self.notes, tuple):
            raise ValueError("collect.notes: expected tuple")
        _notes(self.notes, "collect.notes")
        _float(self.usage_received_at, "collect.usage_received_at", nullable=True)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "outcome": self.outcome,
            "windows": [w.to_json() for w in self.windows],
            "auth_before": None if self.auth_before is None else self.auth_before.to_json(),
            "auth_after": None if self.auth_after is None else self.auth_after.to_json(),
            "source": self.source,
            "source_observed_at": self.source_observed_at,
            "error_code": self.error_code,
            "retry_not_before": self.retry_not_before,
            "cli_version": self.cli_version,
            "notes": list(self.notes),
            "usage_received_at": self.usage_received_at,
        }

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "collect")
        _keys(o, "collect", cls._FIELDS)
        if not isinstance(o["windows"], list):
            raise ValueError("collect.windows: expected list")
        r = cls(
            outcome=o["outcome"],
            windows=tuple(Window.from_json(w) for w in o["windows"]),
            auth_before=None if o["auth_before"] is None else AuthResult.from_json(o["auth_before"]),
            auth_after=None if o["auth_after"] is None else AuthResult.from_json(o["auth_after"]),
            source=o["source"],
            source_observed_at=o["source_observed_at"],
            error_code=o["error_code"],
            retry_not_before=_float(o["retry_not_before"], "collect.retry_not_before", nullable=True),
            cli_version=o["cli_version"],
            notes=tuple(_notes(o["notes"], "collect.notes")),
            usage_received_at=_float(o["usage_received_at"], "collect.usage_received_at", nullable=True),
        )
        r.validate()
        return r


@dataclass(frozen=True)
class Evidence:
    """Herdr-side evidence that a provider is connected to Herdr.

    ``error="herdr_unreachable"`` means unknown: a transport failure must never change a
    recorded connection.
    """

    linked: bool
    source: str | None
    integration_state: str | None
    agent_detected: bool
    profile_correspondence: str
    error: str | None

    _FIELDS = ("linked", "source", "integration_state", "agent_detected", "profile_correspondence", "error")

    def validate(self) -> None:
        _bool(self.linked, "evidence.linked")
        _enum(self.source, "evidence.source", EVIDENCE_SOURCES, nullable=True)
        _enum(self.integration_state, "evidence.integration_state", INTEGRATION_STATES, nullable=True)
        _bool(self.agent_detected, "evidence.agent_detected")
        _enum(self.profile_correspondence, "evidence.profile_correspondence", PROFILE_CORRESPONDENCE)
        _str(self.error, "evidence.error", nullable=True)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "evidence")
        _keys(o, "evidence", cls._FIELDS)
        r = cls(**{k: o[k] for k in cls._FIELDS})
        r.validate()
        return r


def _abs_path_or_none(v: object, name: str) -> str | None:
    s = _str(v, name, nullable=True)
    if s is not None and not s.startswith("/"):
        raise ValueError(f"{name}: must be an absolute path")
    return s


@dataclass(frozen=True)
class ProviderSettings:
    """Per-provider user settings; ``binary``/``profile_dir`` are optional absolute overrides."""

    enabled: bool
    binary: str | None = None
    profile_dir: str | None = None

    def validate(self) -> None:
        _bool(self.enabled, "provider_settings.enabled")
        _abs_path_or_none(self.binary, "provider_settings.binary")
        _abs_path_or_none(self.profile_dir, "provider_settings.profile_dir")

    def to_json(self) -> dict[str, Any]:
        self.validate()
        out: dict[str, Any] = {"enabled": self.enabled}
        if self.binary is not None:
            out["binary"] = self.binary
        if self.profile_dir is not None:
            out["profile_dir"] = self.profile_dir
        return out

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "provider_settings")
        _keys(o, "provider_settings", ("enabled",), ("binary", "profile_dir"))
        r = cls(enabled=o["enabled"], binary=o.get("binary"), profile_dir=o.get("profile_dir"))
        r.validate()
        return r


@dataclass(frozen=True)
class Settings:
    """Validated user settings (``settings.json``); the JSON form nests ``display``/``refresh``."""

    schema_version: int
    providers: Mapping[str, ProviderSettings]
    display_mode: str
    interval_seconds: int
    stale_after_seconds: int
    hide_values_after_seconds: int
    display_format: str = "full"
    display_show_reset: bool = True
    display_window_labels: bool = True

    @classmethod
    def default(cls) -> Self:
        """Settings used when ``settings.json`` is missing."""
        return cls(
            schema_version=SCHEMA_VERSION,
            providers={p: ProviderSettings(enabled=True) for p in PROVIDERS},
            display_mode="used",
            interval_seconds=constants.DEFAULT_INTERVAL,
            stale_after_seconds=constants.DEFAULT_STALE_AFTER,
            hide_values_after_seconds=constants.DEFAULT_HIDE_AFTER,
        )

    def validate(self) -> None:
        _schema(self.schema_version, "settings.schema_version")
        if not isinstance(self.providers, Mapping) or set(self.providers) != set(PROVIDERS):
            raise ValueError(f"settings.providers: must contain exactly {list(PROVIDERS)}")
        for p in PROVIDERS:
            ps = self.providers[p]
            if not isinstance(ps, ProviderSettings):
                raise ValueError(f"settings.providers.{p}: expected ProviderSettings")
            ps.validate()
        _enum(self.display_mode, "settings.display.mode", DISPLAY_MODES)
        _enum(self.display_format, "settings.display.format", DISPLAY_FORMATS)
        _bool(self.display_show_reset, "settings.display.show_reset")
        _bool(self.display_window_labels, "settings.display.window_labels")
        interval = _int(self.interval_seconds, "settings.refresh.interval_seconds")
        stale = _int(self.stale_after_seconds, "settings.refresh.stale_after_seconds")
        hide = _int(self.hide_values_after_seconds, "settings.refresh.hide_values_after_seconds")
        assert interval is not None and stale is not None and hide is not None
        if not constants.INTERVAL_MIN <= interval <= constants.INTERVAL_MAX:
            raise ValueError(
                f"settings.refresh.interval_seconds: must be {constants.INTERVAL_MIN}..{constants.INTERVAL_MAX}"
            )
        if stale < 2 * interval:
            raise ValueError("settings.refresh.stale_after_seconds: must be >= 2 * interval_seconds")
        if not interval < stale < hide:
            raise ValueError("settings.refresh: require interval < stale_after < hide_values_after")
        if hide > constants.HIDE_AFTER_MAX:
            raise ValueError(f"settings.refresh.hide_values_after_seconds: must be <= {constants.HIDE_AFTER_MAX}")

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "providers": {p: self.providers[p].to_json() for p in PROVIDERS},
            "display": {"mode": self.display_mode, "format": self.display_format,
                        "show_reset": self.display_show_reset, "window_labels": self.display_window_labels},
            "refresh": {
                "interval_seconds": self.interval_seconds,
                "stale_after_seconds": self.stale_after_seconds,
                "hide_values_after_seconds": self.hide_values_after_seconds,
            },
        }

    def to_file_json(self) -> dict[str, Any]:
        """``settings.json`` form: stale/hide values derived from the interval are left out, so
        editing only the interval later keeps them in step."""
        out = self.to_json()
        if self.display_format == "full":
            del out["display"]["format"]  # the default: keep files written before the setting existed unchanged
        if self.display_show_reset:
            del out["display"]["show_reset"]  # same convention as format
        if self.display_window_labels:
            del out["display"]["window_labels"]  # same convention as format
        refresh = out["refresh"]
        if (self.stale_after_seconds, self.hide_values_after_seconds) == constants.derived_refresh(self.interval_seconds):
            del refresh["stale_after_seconds"], refresh["hide_values_after_seconds"]
        return out

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "settings")
        _keys(o, "settings", ("schema_version", "providers"), ("display", "refresh"))
        _schema(o["schema_version"], "settings.schema_version")
        provs = _obj(o["providers"], "settings.providers")
        _keys(provs, "settings.providers", PROVIDERS)
        display = _obj(o.get("display", {}), "settings.display")
        _keys(display, "settings.display", (), ("mode", "format", "show_reset", "window_labels"))
        refresh = _obj(o.get("refresh", {}), "settings.refresh")
        _keys(refresh, "settings.refresh", (), ("interval_seconds", "stale_after_seconds", "hide_values_after_seconds"))
        interval = refresh.get("interval_seconds", constants.DEFAULT_INTERVAL)
        stale, hide = constants.resolve_refresh(
            interval,
            refresh.get("stale_after_seconds", constants.MISSING),
            refresh.get("hide_values_after_seconds", constants.MISSING),
        )
        r = cls(
            schema_version=o["schema_version"],
            providers={p: ProviderSettings.from_json(provs[p]) for p in PROVIDERS},
            display_mode=display.get("mode", "used"),
            display_format=display.get("format", "full"),
            display_show_reset=display.get("show_reset", True),
            display_window_labels=display.get("window_labels", True),
            interval_seconds=interval,
            stale_after_seconds=stale,  # type: ignore[arg-type]
            hide_values_after_seconds=hide,  # type: ignore[arg-type]
        )
        r.validate()
        return r


@dataclass
class Snapshot:
    """Last published usage for one provider.

    ``fetched_at`` is the earlier of response arrival and ``source_observed_at``, so a
    cached CLI answer never looks younger. Without ``account_key``, ``server_epoch`` is the
    identity fallback: such a snapshot is never reused across a server restart.
    """

    schema_version: int
    provider: str
    scope_key: str
    profile_key: str
    account_key: str | None
    auth_epoch: int
    source: str
    fetched_at: str
    source_observed_at: str | None
    status: str
    windows: list[Window]
    cli_version: str | None
    server_epoch: str | None

    _FIELDS = (
        "schema_version",
        "provider",
        "scope_key",
        "profile_key",
        "account_key",
        "auth_epoch",
        "source",
        "fetched_at",
        "source_observed_at",
        "status",
        "windows",
        "cli_version",
        "server_epoch",
    )

    def validate(self) -> None:
        _schema(self.schema_version, "snapshot.schema_version")
        _enum(self.provider, "snapshot.provider", PROVIDERS)
        _str(self.scope_key, "snapshot.scope_key")
        _str(self.profile_key, "snapshot.profile_key")
        _str(self.account_key, "snapshot.account_key", nullable=True)
        _int(self.auth_epoch, "snapshot.auth_epoch", minimum=0)
        if self.source != PROVIDER_SOURCES[self.provider]:
            raise ValueError("snapshot.source: not this provider's source")
        _iso(self.fetched_at, "snapshot.fetched_at")
        _iso(self.source_observed_at, "snapshot.source_observed_at", nullable=True)
        _enum(self.status, "snapshot.status", SNAPSHOT_STATUSES)
        if not isinstance(self.windows, list):
            raise ValueError("snapshot.windows: expected list")
        for w in self.windows:
            if not isinstance(w, Window):
                raise ValueError("snapshot.windows: expected Window items")
            w.validate()
        _str(self.cli_version, "snapshot.cli_version", nullable=True)
        _str(self.server_epoch, "snapshot.server_epoch", nullable=True)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        out = {k: getattr(self, k) for k in self._FIELDS}
        out["windows"] = [w.to_json() for w in self.windows]
        return out

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "snapshot")
        _keys(o, "snapshot", cls._FIELDS)
        _schema(o["schema_version"], "snapshot.schema_version")
        if not isinstance(o["windows"], list):
            raise ValueError("snapshot.windows: expected list")
        kwargs = {k: o[k] for k in cls._FIELDS}
        kwargs["windows"] = [Window.from_json(w) for w in o["windows"]]
        r = cls(**kwargs)
        r.validate()
        return r


def _validate_evidence_record(v: object, name: str) -> dict[str, Any] | None:
    if v is None:
        return None
    o = _obj(v, name)
    _keys(o, name, EVIDENCE_RECORD_KEYS)
    _enum(o["source"], f"{name}.source", EVIDENCE_SOURCES)
    _enum(o["integration_state"], f"{name}.integration_state", INTEGRATION_STATES, nullable=True)
    return {
        "source": o["source"],
        "integration_state": o["integration_state"],
        "observed_at": _float(o["observed_at"], f"{name}.observed_at"),
    }


@dataclass
class ConnectionRecord:
    """Per-provider connection / verification state.

    A binding change resets the record and bumps ``auth_epoch``. Unless
    ``verified_server_epoch``/``verified_generation`` match the current ones the renderer
    shows ``"<Name> …"``.
    """

    schema_version: int
    provider: str
    binding_key: str
    profile_key: str
    state: str
    ever_connected: bool
    evidence: dict | None
    executable: str | None
    cli_version: str | None
    account_key: str | None
    auth_epoch: int
    verified_at: float | None
    verified_server_epoch: str | None
    verified_generation: int | None
    next_verify_at: float
    last_reason: str | None
    env_conflicts: list[str]
    verified_servers: dict = field(default_factory=dict)

    _FIELDS = (
        "schema_version",
        "provider",
        "binding_key",
        "profile_key",
        "state",
        "ever_connected",
        "evidence",
        "executable",
        "cli_version",
        "account_key",
        "auth_epoch",
        "verified_at",
        "verified_server_epoch",
        "verified_generation",
        "next_verify_at",
        "last_reason",
        "env_conflicts",
        "verified_servers",
    )

    def validate(self) -> None:
        _schema(self.schema_version, "connection.schema_version")
        _enum(self.provider, "connection.provider", PROVIDERS)
        _str(self.binding_key, "connection.binding_key")
        _str(self.profile_key, "connection.profile_key")
        _enum(self.state, "connection.state", CONNECTION_STATES)
        _bool(self.ever_connected, "connection.ever_connected")
        _validate_evidence_record(self.evidence, "connection.evidence")
        _abs_path_or_none(self.executable, "connection.executable")
        _str(self.cli_version, "connection.cli_version", nullable=True)
        _str(self.account_key, "connection.account_key", nullable=True)
        _int(self.auth_epoch, "connection.auth_epoch", minimum=0)
        _float(self.verified_at, "connection.verified_at", nullable=True)
        _str(self.verified_server_epoch, "connection.verified_server_epoch", nullable=True)
        _int(self.verified_generation, "connection.verified_generation", nullable=True, minimum=0)
        _float(self.next_verify_at, "connection.next_verify_at")
        _str(self.last_reason, "connection.last_reason", nullable=True)
        if not isinstance(self.env_conflicts, list):
            raise ValueError("connection.env_conflicts: expected list")
        _str_seq(self.env_conflicts, "connection.env_conflicts", env_names=True)
        _verified_servers(self.verified_servers, "connection.verified_servers")

    def to_json(self) -> dict[str, Any]:
        self.validate()
        out = {k: getattr(self, k) for k in self._FIELDS}
        out["verified_servers"] = dict(self.verified_servers)
        out["evidence"] = _validate_evidence_record(self.evidence, "connection.evidence")
        out["verified_at"] = None if self.verified_at is None else float(self.verified_at)
        out["next_verify_at"] = float(self.next_verify_at)
        out["env_conflicts"] = list(self.env_conflicts)
        return out

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "connection")
        _keys(o, "connection", cls._FIELDS)
        _schema(o["schema_version"], "connection.schema_version")
        kwargs = {k: o[k] for k in cls._FIELDS}
        kwargs["evidence"] = _validate_evidence_record(o["evidence"], "connection.evidence")
        kwargs["verified_at"] = _float(o["verified_at"], "connection.verified_at", nullable=True)
        kwargs["next_verify_at"] = _float(o["next_verify_at"], "connection.next_verify_at")
        kwargs["env_conflicts"] = _str_seq(o["env_conflicts"], "connection.env_conflicts", env_names=True)
        kwargs["verified_servers"] = _verified_servers(o["verified_servers"], "connection.verified_servers")
        r = cls(**kwargs)
        r.validate()
        return r


@dataclass
class ProviderStatus:
    """Per-provider data/scheduling state.

    An attempt is in progress while ``attempt_started_at > (attempt_finished_at or 0)``
    and younger than ``LEASE_TTL``. ``first_failure_at`` is optional on disk.
    """

    schema_version: int
    provider: str
    data_state: str
    attempt_started_at: float | None
    attempt_finished_at: float | None
    last_success_at: float | None
    last_failure_at: float | None
    consecutive_failures: int
    next_due_at: float
    retry_not_before: float | None
    last_error_code: str | None
    unsupported_cli_version: str | None
    notes: list[str]
    first_failure_at: float | None = None

    _FIELDS = (
        "schema_version",
        "provider",
        "data_state",
        "attempt_started_at",
        "attempt_finished_at",
        "last_success_at",
        "last_failure_at",
        "consecutive_failures",
        "next_due_at",
        "retry_not_before",
        "last_error_code",
        "unsupported_cli_version",
        "notes",
    )
    _TIME_FIELDS = (
        "attempt_started_at",
        "attempt_finished_at",
        "last_success_at",
        "last_failure_at",
        "retry_not_before",
    )
    _OPTIONAL_FIELDS = ("first_failure_at",)

    def validate(self) -> None:
        _float(self.first_failure_at, "status.first_failure_at", nullable=True)
        _schema(self.schema_version, "status.schema_version")
        _enum(self.provider, "status.provider", PROVIDERS)
        _enum(self.data_state, "status.data_state", DATA_STATES)
        for k in self._TIME_FIELDS:
            _float(getattr(self, k), f"status.{k}", nullable=True)
        _int(self.consecutive_failures, "status.consecutive_failures", minimum=0)
        _float(self.next_due_at, "status.next_due_at")
        _str(self.last_error_code, "status.last_error_code", nullable=True)
        _str(self.unsupported_cli_version, "status.unsupported_cli_version", nullable=True)
        if not isinstance(self.notes, list):
            raise ValueError("status.notes: expected list")
        _notes(self.notes, "status.notes")

    def to_json(self) -> dict[str, Any]:
        self.validate()
        out = {k: getattr(self, k) for k in self._FIELDS}
        for k in self._TIME_FIELDS:
            out[k] = None if out[k] is None else float(out[k])
        out["next_due_at"] = float(self.next_due_at)
        out["notes"] = list(self.notes)
        out["first_failure_at"] = None if self.first_failure_at is None else float(self.first_failure_at)
        return out

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "status")
        _keys(o, "status", cls._FIELDS, cls._OPTIONAL_FIELDS)
        _schema(o["schema_version"], "status.schema_version")
        kwargs = {k: o[k] for k in cls._FIELDS}
        kwargs["first_failure_at"] = _float(o.get("first_failure_at"), "status.first_failure_at", nullable=True)
        for k in cls._TIME_FIELDS:
            kwargs[k] = _float(o[k], f"status.{k}", nullable=True)
        kwargs["next_due_at"] = _float(o["next_due_at"], "status.next_due_at")
        kwargs["notes"] = _notes(o["notes"], "status.notes")
        r = cls(**kwargs)
        r.validate()
        return r


@dataclass(frozen=True)
class ProviderView:
    """Everything the renderer/scheduler read for one provider; invalid records are None."""

    connection: ConnectionRecord | None
    status: ProviderStatus | None
    snapshot: Snapshot | None
    generation: int

    _FIELDS = ("connection", "status", "snapshot", "generation")

    def validate(self) -> None:
        for name, typ in (("connection", ConnectionRecord), ("status", ProviderStatus), ("snapshot", Snapshot)):
            v = getattr(self, name)
            if v is not None:
                if not isinstance(v, typ):
                    raise ValueError(f"view.{name}: expected {typ.__name__}")
                v.validate()
        _int(self.generation, "view.generation", minimum=0)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "connection": None if self.connection is None else self.connection.to_json(),
            "status": None if self.status is None else self.status.to_json(),
            "snapshot": None if self.snapshot is None else self.snapshot.to_json(),
            "generation": self.generation,
        }

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "view")
        _keys(o, "view", cls._FIELDS)
        r = cls(
            connection=None if o["connection"] is None else ConnectionRecord.from_json(o["connection"]),
            status=None if o["status"] is None else ProviderStatus.from_json(o["status"]),
            snapshot=None if o["snapshot"] is None else Snapshot.from_json(o["snapshot"]),
            generation=o["generation"],
        )
        r.validate()
        return r


def validate_dispatch(d: object) -> dict[str, Any] | None:
    """Validate a dispatch-lease record and return a normalised copy (None passes through)."""
    if d is None:
        return None
    o = _obj(d, "dispatch")
    _keys(o, "dispatch", DISPATCH_KEYS)
    return {
        "id": _str(o["id"], "dispatch.id"),
        "dispatched_at": _float(o["dispatched_at"], "dispatch.dispatched_at"),
        "finished_at": _float(o["finished_at"], "dispatch.finished_at", nullable=True),
    }


def default_control() -> dict[str, Any]:
    """``control.json`` content used when the file is missing."""
    return {
        "schema_version": SCHEMA_VERSION,
        "providers": {p: {"generation": 0, "settings_fingerprint": None} for p in PROVIDERS},
    }


def validate_control(d: object) -> dict[str, Any]:
    """Validate ``control.json`` and return a normalised copy.

    A ``settings_fingerprint`` mismatch bumps ``generation`` under ``locks/settings.lock``.
    """
    o = _obj(d, "control")
    _keys(o, "control", ("schema_version", "providers"))
    _schema(o["schema_version"], "control.schema_version")
    provs = _obj(o["providers"], "control.providers")
    _keys(provs, "control.providers", PROVIDERS)
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "providers": {}}
    for p in PROVIDERS:
        name = f"control.providers.{p}"
        entry = _obj(provs[p], name)
        _keys(entry, name, CONTROL_PROVIDER_KEYS)
        out["providers"][p] = {
            "generation": _int(entry["generation"], f"{name}.generation", minimum=0),
            "settings_fingerprint": _str(entry["settings_fingerprint"], f"{name}.settings_fingerprint", nullable=True),
        }
    return out


@dataclass(frozen=True)
class StateView:
    """Persisted state read by one renderer tick; a None ``server_epoch`` fails closed."""

    server_epoch: str | None
    providers: Mapping[str, ProviderView]
    dispatch: dict | None

    _FIELDS = ("server_epoch", "providers", "dispatch")

    def validate(self) -> None:
        _str(self.server_epoch, "state.server_epoch", nullable=True)
        if not isinstance(self.providers, Mapping):
            raise ValueError("state.providers: expected mapping")
        for p, v in self.providers.items():
            _enum(p, "state.providers key", PROVIDERS)
            if not isinstance(v, ProviderView):
                raise ValueError(f"state.providers.{p}: expected ProviderView")
            v.validate()
        validate_dispatch(self.dispatch)

    def to_json(self) -> dict[str, Any]:
        self.validate()
        return {
            "server_epoch": self.server_epoch,
            "providers": {p: self.providers[p].to_json() for p in PROVIDERS if p in self.providers},
            "dispatch": validate_dispatch(self.dispatch),
        }

    @classmethod
    def from_json(cls, d: object) -> Self:
        o = _obj(d, "state")
        _keys(o, "state", cls._FIELDS)
        provs = _obj(o["providers"], "state.providers")
        for p in provs:
            _enum(p, "state.providers key", PROVIDERS)
        r = cls(
            server_epoch=o["server_epoch"],
            providers={p: ProviderView.from_json(provs[p]) for p in PROVIDERS if p in provs},
            dispatch=validate_dispatch(o["dispatch"]),
        )
        r.validate()
        return r


@runtime_checkable
class Collector(Protocol):
    """A provider usage collector bound to one executable + profile dir.

    Never raises for expected failures; returns a result describing them. Every new
    subprocess and protocol message is guarded by ``procutil.admission_section(admit)``.
    When admission is refused, send nothing more and stop gracefully (never SIGKILL an
    in-flight credential refresh); return ``error_code``/``detail`` ``ADMISSION_REVOKED``.
    """

    provider: str

    def cli_version(self) -> str | None:
        ...

    def check_auth(self, timeout_s: float, admit: Callable[[], bool] | None = None) -> AuthResult:
        """Non-usage authentication check; ``admit=None`` means always admitted."""
        ...

    def collect(self, timeout_s: float, admit: Callable[[], bool] | None = None) -> CollectResult:
        """Auth before → usage → identity proof, within ``timeout_s`` overall."""
        ...


@runtime_checkable
class HostAdapter(Protocol):
    """Environment access used by refresh orchestration."""

    def herdr_config_path(self) -> str:
        ...

    def evidence(self, provider: str, profile_dir: str, ps: ProviderSettings | None = None) -> Evidence:
        ...

    def resolve_profile(self, provider: str, ps: ProviderSettings) -> str:
        """Realpath of the provider profile dir in use."""
        ...

    def resolve_executable(self, provider: str, ps: ProviderSettings) -> str | None:
        """Absolute, trusted CLI path, or None when the CLI is missing."""
        ...

    def make_collector(self, provider: str, executable: str, profile_dir: str, *, lock_fd: int | None = None,
                       ps: ProviderSettings | None = None) -> Collector:
        """Build a ``Collector``.

        ``lock_fd`` (the provider lock) stays in the refresh: a fd registered with
        ``procutil.register_lock_owner`` is never inherited. Credentials are selected from
        ``ps``, never a separately loaded copy.
        """
        ...

    def server_identity(self) -> tuple[str, str | None]:
        ...

    def credential_namespace(self, provider: str, ps: ProviderSettings) -> str:
        ...

    def plugin_active(self) -> bool:
        """Live check that the plugin is registered and enabled; False on any error. Never raises."""
        ...


__all__ = [
    "PROVIDERS",
    "DISPLAY_NAMES",
    "SCHEMA_VERSION",
    "WINDOW_IDS",
    "WINDOW_SCOPES",
    "AUTH_STATES",
    "COLLECT_OUTCOMES",
    "PROVIDER_SOURCES",
    "SOURCES",
    "EVIDENCE_SOURCES",
    "INTEGRATION_STATES",
    "PROFILE_CORRESPONDENCE",
    "CONNECTION_STATES",
    "DATA_STATES",
    "SNAPSHOT_STATUSES",
    "DISPLAY_MODES",
    "DISPLAY_FORMATS",
    "EVIDENCE_RECORD_KEYS",
    "DISPATCH_KEYS",
    "NOTE_CODES",
    "CONTROL_PROVIDER_KEYS",
    "ADMISSION_REVOKED",
    "MAX_JSON_INT",
    "MAX_VERIFIED_SERVERS",
    "is_finite_number",
    "iso_utc",
    "iso_utc_precise",
    "parse_iso_utc",
    "is_valid_percent",
    "duration_label",
    "raw_percent_or_none",
    "make_note",
    "is_valid_note",
    "validate_dispatch",
    "default_control",
    "validate_control",
    "Window",
    "AuthResult",
    "CollectResult",
    "Evidence",
    "ProviderSettings",
    "Settings",
    "Snapshot",
    "ConnectionRecord",
    "ProviderStatus",
    "ProviderView",
    "StateView",
    "Collector",
    "HostAdapter",
]
