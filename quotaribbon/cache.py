"""JSON state stores: a lean plain-dict layer for the tab-bar tick and a lazy dataclass layer.

Unusable records become None, never "fixed up". ``control.json`` and ``server_epoch.json``
writes require ``locks/settings.lock``; per-binding records are written only by the
provider-lock holder.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import stat
import time

# Mirrors of types.py vocabularies (tests assert they stay identical).

PROVIDERS = ("claude", "codex")
SCHEMA_VERSION = 1
WINDOW_IDS = frozenset({"five_hour", "seven_day", "primary", "secondary"})
WINDOW_SCOPES = frozenset({"all"})
PROVIDER_SOURCES = {"claude": "claude-control-get-usage", "codex": "codex-app-server-rate-limits"}
EVIDENCE_SOURCES = frozenset({"integration", "agent"})
INTEGRATION_STATES = frozenset({"not_installed", "current", "outdated"})
CONNECTION_STATES = frozenset(
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
DATA_STATES = frozenset({"none", "ok", "transient_error", "protocol_unsupported", "unavailable", "rate_limited"})
NOTE_CODES = frozenset(
    {
        "window_not_provided:five_hour",
        "window_not_provided:seven_day",
        "ignored_model_buckets",
        "no_common_bucket",
        "invalid_percent",
        "invalid_reset",
        "percent_out_of_range",
        "unknown_duration",
        "unexpected_turn",
        "managed_policy_present",
        "rate_limits_null",
        "rate_limits_unavailable",
        "legacy_bucket_used",
        "profile_unverified",
    }
)

MAX_JSON_BYTES = 1 << 20
_HEX = frozenset("0123456789abcdef")

CONNECTION_FIELDS = (
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
STATUS_FIELDS = (
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
STATUS_OPTIONAL_FIELDS = ("first_failure_at",)
_STATUS_TIME_FIELDS = ("attempt_started_at", "attempt_finished_at", "last_success_at", "last_failure_at", "retry_not_before")
SNAPSHOT_FIELDS = (
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
WINDOW_FIELDS = ("id", "scope", "duration_seconds", "used_percent", "raw_used_percent", "resets_at")

# Path layout mirrors paths.Paths (tests assert equality).


def is_binding_key(value: object) -> bool:
    return isinstance(value, str) and len(value) == 24 and all(c in _HEX for c in value)


def control_path(state_dir: str) -> str:
    return os.path.join(state_dir, "control.json")


def server_epoch_path(state_dir: str) -> str:
    return os.path.join(state_dir, "server_epoch.json")


def clock_path(state_dir: str) -> str:
    return os.path.join(state_dir, "clock.json")


def dispatch_path(state_dir: str) -> str:
    return os.path.join(state_dir, "dispatch.json")


def dispatch_lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, "locks", "dispatch.lock")


def record_path(state_dir: str, provider: str, binding_key: str, name: str) -> str:
    if provider not in PROVIDERS or not is_binding_key(binding_key):
        raise ValueError("invalid provider or binding key")
    return os.path.join(state_dir, "providers", provider, binding_key, name + ".json")


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-standard JSON constant {name}")


def _no_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def loads_strict(text: str) -> object:
    """Parse strict JSON: no NaN/Infinity, no duplicate keys. Raises ``ValueError``."""
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)


MISSING = "missing"
CORRUPT = "corrupt"
OK = "ok"


def read_regular_bytes(path: str, limit: int) -> bytes:
    """Read at most ``limit`` bytes of a regular file; a planted FIFO or device raises
    ``OSError`` instead of hanging the caller."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        os.set_blocking(fd, True)
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json_status(path: str, max_bytes: int = MAX_JSON_BYTES) -> tuple[str, object]:
    """Return ``("ok", obj)``, ``("missing", None)`` or ``("corrupt", reason)``. Never raises."""
    try:
        data = read_regular_bytes(path, max_bytes + 1)
    except FileNotFoundError:
        return MISSING, None
    except OSError as exc:
        return CORRUPT, f"unreadable ({exc.__class__.__name__})"
    if len(data) > max_bytes:
        return CORRUPT, "too large"
    try:
        return OK, loads_strict(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return CORRUPT, "invalid JSON"


def write_json_atomic(path: str, obj: object) -> None:
    """Atomic private JSON write using only ``os`` (tick path)."""
    data = (json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, "." + os.path.basename(path) + "." + os.urandom(6).hex() + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, 0o700, exist_ok=True)


def lock_fd(path: str, *, blocking: bool) -> int | None:
    """Exclusive ``flock``; return the fd, or None when busy. Lock files are never deleted."""
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


def unlock_fd(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


# Lean strict validators: accept exactly what types.*.from_json accepts.


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _obj(d: object, name: str) -> dict:
    if not isinstance(d, dict):
        _fail(f"{name}: expected object")
    for k in d:  # type: ignore[union-attr]
        if not isinstance(k, str):
            _fail(f"{name}: non-string key")
    return d  # type: ignore[return-value]


def _exact_keys(d: dict, name: str, required: tuple, optional: tuple = ()) -> None:
    for k in required:
        if k not in d:
            _fail(f"{name}: missing key {k}")
    allowed = set(required) | set(optional)
    for k in d:
        if k not in allowed:
            _fail(f"{name}: unknown key")


MAX_JSON_INT = 1 << 53


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and -MAX_JSON_INT <= v <= MAX_JSON_INT


def _is_num(v: object) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -MAX_JSON_INT <= v <= MAX_JSON_INT
    return isinstance(v, float) and math.isfinite(v)


def _opt_str(v: object, name: str) -> None:
    if v is not None and not isinstance(v, str):
        _fail(f"{name}: expected string or null")


def _req_str(v: object, name: str) -> None:
    if not isinstance(v, str):
        _fail(f"{name}: expected string")


def _num(v: object, name: str, *, nullable: bool = False) -> float | None:
    if v is None and nullable:
        return None
    if not _is_num(v):
        _fail(f"{name}: expected finite number")
    return float(v)  # type: ignore[arg-type]


def _int_min(v: object, name: str, minimum: int, *, nullable: bool = False) -> int | None:
    if v is None and nullable:
        return None
    if not _is_int(v) or v < minimum:  # type: ignore[operator]
        _fail(f"{name}: expected int >= {minimum}")
    return v  # type: ignore[return-value]


def _schema(v: object, name: str) -> None:
    if not _is_int(v) or v != SCHEMA_VERSION:
        _fail(f"{name}: unsupported schema_version")


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _digits(s: str) -> bool:
    return s.isascii() and s.isdigit()


_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def parse_iso(s: object) -> float:
    """Lean equivalent of ``types.parse_iso_utc``."""
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string")
    n = len(s)
    if n < 20 or s[4] != "-" or s[7] != "-" or s[10] != "T" or s[13] != ":" or s[16] != ":":
        raise ValueError("invalid ISO-8601 timestamp")
    fields = (s[0:4], s[5:7], s[8:10], s[11:13], s[14:16], s[17:19])
    if not all(_digits(f) for f in fields):
        raise ValueError("invalid ISO-8601 timestamp")
    year, month, day, hour, minute, second = (int(f) for f in fields)
    rest = s[19:]
    frac = ""
    if rest.startswith("."):
        i = 1
        while i < len(rest) and rest[i].isascii() and rest[i].isdigit():
            i += 1
        frac = rest[1:i]
        if not 1 <= len(frac) <= 9:
            raise ValueError("invalid fraction")
        rest = rest[i:]
    if rest == "Z":
        offset = 0
    elif len(rest) == 6 and rest[0] in "+-" and rest[3] == ":" and _digits(rest[1:3]) and _digits(rest[4:6]):
        oh, om = int(rest[1:3]), int(rest[4:6])
        if oh > 23 or om > 59:
            raise ValueError("invalid UTC offset")
        offset = (1 if rest[0] == "+" else -1) * (oh * 3600 + om * 60)
    else:
        raise ValueError("invalid ISO-8601 timestamp")
    if not (1 <= year <= 9999 and 1 <= month <= 12 and hour <= 23 and minute <= 59 and second <= 59):
        raise ValueError("invalid ISO-8601 timestamp")
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    mdays = 29 if (month == 2 and leap) else _MONTH_DAYS[month - 1]
    if not 1 <= day <= mdays:
        raise ValueError("invalid ISO-8601 timestamp")
    whole = _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second - offset
    if not frac:
        return float(whole)
    return (whole * 10 ** len(frac) + int(frac)) / 10 ** len(frac)


def _iso(v: object, name: str, *, nullable: bool = False) -> None:
    if v is None and nullable:
        return
    try:
        parse_iso(v)
    except ValueError:
        _fail(f"{name}: invalid timestamp")


def _is_env_name(s: str) -> bool:
    if not s or not s.isascii():
        return False
    if not (s[0].isalpha() or s[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in s)


def _env_names(v: object, name: str) -> list:
    if not isinstance(v, list):
        _fail(f"{name}: expected list")
    for item in v:  # type: ignore[union-attr]
        if not isinstance(item, str) or not _is_env_name(item):
            _fail(f"{name}: invalid environment variable name")
    return list(v)  # type: ignore[arg-type]


def is_valid_note(note: object) -> bool:
    if not isinstance(note, str):
        return False
    if note in NOTE_CODES:
        return True
    code, sep, count = note.rpartition(":")
    return bool(sep) and code in NOTE_CODES and count.isascii() and count.isdigit() and str(int(count)) == count


def _notes(v: object, name: str) -> list:
    if not isinstance(v, list):
        _fail(f"{name}: expected list")
    for note in v:  # type: ignore[union-attr]
        if not is_valid_note(note):
            _fail(f"{name}: unknown note")
    return list(v)  # type: ignore[arg-type]


def validate_window(d: object) -> dict:
    o = _obj(d, "window")
    _exact_keys(o, "window", WINDOW_FIELDS)
    if not isinstance(o["id"], str) or o["id"] not in WINDOW_IDS:
        _fail("window.id: unknown")
    if not isinstance(o["scope"], str) or o["scope"] not in WINDOW_SCOPES:
        _fail("window.scope: unknown")
    _int_min(o["duration_seconds"], "window.duration_seconds", 1, nullable=True)
    used = _num(o["used_percent"], "window.used_percent", nullable=True)
    if used is not None and not 0 <= used <= 100:
        _fail("window.used_percent: out of range")
    raw = o["raw_used_percent"]
    if raw is not None and not _is_num(raw):
        _fail("window.raw_used_percent: must be a finite number or null")
    _iso(o["resets_at"], "window.resets_at", nullable=True)
    return {
        "id": o["id"],
        "scope": o["scope"],
        "duration_seconds": o["duration_seconds"],
        "used_percent": used,
        "raw_used_percent": raw,
        "resets_at": o["resets_at"],
    }


def validate_snapshot(d: object) -> dict:
    o = _obj(d, "snapshot")
    _exact_keys(o, "snapshot", SNAPSHOT_FIELDS)
    _schema(o["schema_version"], "snapshot.schema_version")
    if not isinstance(o["windows"], list):
        _fail("snapshot.windows: expected list")
    windows = [validate_window(w) for w in o["windows"]]
    provider = o["provider"]
    if not isinstance(provider, str) or provider not in PROVIDERS:
        _fail("snapshot.provider: unknown")
    _req_str(o["scope_key"], "snapshot.scope_key")
    _req_str(o["profile_key"], "snapshot.profile_key")
    _opt_str(o["account_key"], "snapshot.account_key")
    _int_min(o["auth_epoch"], "snapshot.auth_epoch", 0)
    if o["source"] != PROVIDER_SOURCES[provider]:
        _fail("snapshot.source: wrong source")
    _iso(o["fetched_at"], "snapshot.fetched_at")
    _iso(o["source_observed_at"], "snapshot.source_observed_at", nullable=True)
    if o["status"] != "ok":
        _fail("snapshot.status: unknown")
    _opt_str(o["cli_version"], "snapshot.cli_version")
    _opt_str(o["server_epoch"], "snapshot.server_epoch")
    out = dict(o)
    out["windows"] = windows
    return out


def _evidence_record(v: object) -> dict | None:
    if v is None:
        return None
    o = _obj(v, "connection.evidence")
    _exact_keys(o, "connection.evidence", ("source", "integration_state", "observed_at"))
    if not isinstance(o["source"], str) or o["source"] not in EVIDENCE_SOURCES:
        _fail("connection.evidence.source: unknown")
    st = o["integration_state"]
    if st is not None and (not isinstance(st, str) or st not in INTEGRATION_STATES):
        _fail("connection.evidence.integration_state: unknown")
    return {"source": o["source"], "integration_state": st, "observed_at": _num(o["observed_at"], "evidence.observed_at")}


def validate_connection(d: object) -> dict:
    o = _obj(d, "connection")
    _exact_keys(o, "connection", CONNECTION_FIELDS)
    _schema(o["schema_version"], "connection.schema_version")
    if not isinstance(o["provider"], str) or o["provider"] not in PROVIDERS:
        _fail("connection.provider: unknown")
    _req_str(o["binding_key"], "connection.binding_key")
    _req_str(o["profile_key"], "connection.profile_key")
    if not isinstance(o["state"], str) or o["state"] not in CONNECTION_STATES:
        _fail("connection.state: unknown")
    if not isinstance(o["ever_connected"], bool):
        _fail("connection.ever_connected: expected bool")
    evidence = _evidence_record(o["evidence"])
    exe = o["executable"]
    if exe is not None and (not isinstance(exe, str) or not exe.startswith("/")):
        _fail("connection.executable: must be an absolute path")
    _opt_str(o["cli_version"], "connection.cli_version")
    _opt_str(o["account_key"], "connection.account_key")
    _int_min(o["auth_epoch"], "connection.auth_epoch", 0)
    verified_at = _num(o["verified_at"], "connection.verified_at", nullable=True)
    _opt_str(o["verified_server_epoch"], "connection.verified_server_epoch")
    _int_min(o["verified_generation"], "connection.verified_generation", 0, nullable=True)
    next_verify_at = _num(o["next_verify_at"], "connection.next_verify_at")
    _opt_str(o["last_reason"], "connection.last_reason")
    env_conflicts = _env_names(o["env_conflicts"], "connection.env_conflicts")
    servers = _verified_servers(o["verified_servers"])
    out = dict(o)
    out.update(evidence=evidence, verified_at=verified_at, next_verify_at=next_verify_at, env_conflicts=env_conflicts,
               verified_servers=servers)
    return out


MAX_VERIFIED_SERVERS = 8


def _is_server_identity(s: object) -> bool:
    if not isinstance(s, str) or s.count(":") != 2 or len(s) > 74:
        return False
    return all(1 <= len(part) <= 24 and _digits(part) for part in s.split(":"))


def _verified_servers(v: object) -> dict:
    if not isinstance(v, dict) or len(v) > MAX_VERIFIED_SERVERS:
        _fail("connection.verified_servers: invalid")
    for k, ident in v.items():  # type: ignore[union-attr]
        if not isinstance(k, str) or not k.startswith("/") or len(k) > 1024 or "\0" in k:
            _fail("connection.verified_servers: invalid socket path")
        if not _is_server_identity(ident):
            _fail("connection.verified_servers: invalid server identity")
    return dict(v)  # type: ignore[arg-type]


def validate_status(d: object) -> dict:
    o = _obj(d, "status")
    _exact_keys(o, "status", STATUS_FIELDS, STATUS_OPTIONAL_FIELDS)
    _schema(o["schema_version"], "status.schema_version")
    if not isinstance(o["provider"], str) or o["provider"] not in PROVIDERS:
        _fail("status.provider: unknown")
    if not isinstance(o["data_state"], str) or o["data_state"] not in DATA_STATES:
        _fail("status.data_state: unknown")
    out = dict(o)
    for k in _STATUS_TIME_FIELDS:
        out[k] = _num(o[k], f"status.{k}", nullable=True)
    _int_min(o["consecutive_failures"], "status.consecutive_failures", 0)
    out["next_due_at"] = _num(o["next_due_at"], "status.next_due_at")
    _opt_str(o["last_error_code"], "status.last_error_code")
    _opt_str(o["unsupported_cli_version"], "status.unsupported_cli_version")
    out["notes"] = _notes(o["notes"], "status.notes")
    out["first_failure_at"] = _num(o.get("first_failure_at"), "status.first_failure_at", nullable=True)
    return out


def validate_control(d: object) -> dict:
    o = _obj(d, "control")
    _exact_keys(o, "control", ("schema_version", "providers"))
    _schema(o["schema_version"], "control.schema_version")
    provs = _obj(o["providers"], "control.providers")
    _exact_keys(provs, "control.providers", PROVIDERS)
    out: dict = {"schema_version": SCHEMA_VERSION, "providers": {}}
    for p in PROVIDERS:
        entry = _obj(provs[p], f"control.providers.{p}")
        _exact_keys(entry, f"control.providers.{p}", ("generation", "settings_fingerprint"))
        gen = _int_min(entry["generation"], f"control.providers.{p}.generation", 0)
        _opt_str(entry["settings_fingerprint"], f"control.providers.{p}.settings_fingerprint")
        out["providers"][p] = {"generation": gen, "settings_fingerprint": entry["settings_fingerprint"]}
    return out


def default_control_dict() -> dict:
    return {"schema_version": SCHEMA_VERSION, "providers": {p: {"generation": 0, "settings_fingerprint": None} for p in PROVIDERS}}


def validate_dispatch(d: object) -> dict | None:
    if d is None:
        return None
    o = _obj(d, "dispatch")
    _exact_keys(o, "dispatch", ("id", "dispatched_at", "finished_at"))
    _req_str(o["id"], "dispatch.id")
    return {
        "id": o["id"],
        "dispatched_at": _num(o["dispatched_at"], "dispatch.dispatched_at"),
        "finished_at": _num(o["finished_at"], "dispatch.finished_at", nullable=True),
    }


def validate_server_epoch(d: object) -> dict:
    o = _obj(d, "server_epoch")
    _exact_keys(o, "server_epoch", ("schema_version", "epoch", "started_at"))
    _schema(o["schema_version"], "server_epoch.schema_version")
    if not isinstance(o["epoch"], str) or not o["epoch"]:
        _fail("server_epoch.epoch: expected non-empty string")
    return {"schema_version": SCHEMA_VERSION, "epoch": o["epoch"], "started_at": _num(o["started_at"], "server_epoch.started_at")}


def validate_clock(d: object) -> dict:
    o = _obj(d, "clock")
    _exact_keys(o, "clock", ("max_wall_seen",))
    return {"max_wall_seen": _num(o["max_wall_seen"], "clock.max_wall_seen")}


def _file_signature(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return "absent"
    return f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}"


def report_bad_file(state_dir: str, path: str, reason: str) -> None:
    """Log ``path`` as unusable once per file version. Never raises."""
    try:
        seen_path = os.path.join(state_dir, "logs", "bad_files.json")
        sig = _file_signature(path)
        status, seen = read_json_status(seen_path)
        if status != OK or not isinstance(seen, dict):
            seen = {}
        rel = os.path.relpath(path, state_dir)
        if seen.get(rel) == sig:
            return
        seen[rel] = sig
        if len(seen) > 64:
            seen = {rel: sig}
        _ensure_dir(os.path.dirname(seen_path))
        write_json_atomic(seen_path, seen)
        from .log import get_logger  # lazy: logging is not allowed on the tick path
        from .paths import Paths

        get_logger(Paths(config_dir=state_dir, state_dir=state_dir)).warning(
            "ignoring unusable state file %s: %s", rel, reason
        )
    except Exception:  # noqa: BLE001 - reporting must never break a reader
        pass


def _read_validated(state_dir: str, path: str, validator) -> dict | None:
    status, obj = read_json_status(path)
    if status == MISSING:
        return None
    if status == CORRUPT:
        report_bad_file(state_dir, path, str(obj))
        return None
    try:
        return validator(obj)
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        # Fixed reason only: validator messages may quote file content.
        report_bad_file(state_dir, path, "schema mismatch")
        return None


def read_control_dict(state_dir: str) -> dict:
    c = _read_validated(state_dir, control_path(state_dir), validate_control)
    return c if c is not None else default_control_dict()


def read_server_epoch_at(state_dir: str) -> str | None:
    e = _read_validated(state_dir, server_epoch_path(state_dir), validate_server_epoch)
    return None if e is None else e["epoch"]


def read_clock_at(state_dir: str) -> float | None:
    c = _read_validated(state_dir, clock_path(state_dir), validate_clock)
    return None if c is None else c["max_wall_seen"]


CLOCK_OK, CLOCK_MISSING, CLOCK_CORRUPT = "ok", "missing", "corrupt"


def read_clock_state_at(state_dir: str) -> tuple[str, float | None]:
    """``(state, max_wall_seen)``; a corrupt clock is never mistaken for "no rollback"."""
    path = clock_path(state_dir)
    status, obj = read_json_status(path)
    if status == MISSING:
        return CLOCK_MISSING, None
    if status == CORRUPT:
        report_bad_file(state_dir, path, str(obj))
        return CLOCK_CORRUPT, None
    try:
        return CLOCK_OK, validate_clock(obj)["max_wall_seen"]
    except (ValueError, KeyError, TypeError, OverflowError):
        report_bad_file(state_dir, path, "schema mismatch")
        return CLOCK_CORRUPT, None


def any_snapshot_exists(state_dir: str) -> bool:
    """Whether any snapshot file may exist; fails closed (anything uncertain counts as yes)."""
    root = os.path.join(state_dir, "providers")
    for provider in PROVIDERS:
        try:
            keys = os.listdir(os.path.join(root, provider))
        except FileNotFoundError:
            continue
        except OSError:
            return True
        for key in keys:
            if not is_binding_key(key):
                continue
            try:
                os.lstat(os.path.join(root, provider, key, "snapshot.json"))
                return True
            except FileNotFoundError:
                continue
            except OSError:
                return True
    return False


def read_dispatch_at(state_dir: str) -> dict | None:
    return _read_validated(state_dir, dispatch_path(state_dir), validate_dispatch)


def _binding_attr(b: object, name: str) -> object:
    return b.get(name) if isinstance(b, dict) else getattr(b, name, None)


def read_provider_dicts(state_dir: str, provider: str, binding: object) -> dict:
    """Records of one binding; those naming another provider or binding become None."""
    out: dict = {"connection": None, "status": None, "snapshot": None}
    bk = _binding_attr(binding, "binding_key")
    pk = _binding_attr(binding, "profile_key")
    if provider not in PROVIDERS or not is_binding_key(bk):
        return out
    conn = _read_validated(state_dir, record_path(state_dir, provider, bk, "connection"), validate_connection)  # type: ignore[arg-type]
    if conn is not None and (conn["provider"] != provider or conn["binding_key"] != bk or conn["profile_key"] != pk):
        report_bad_file(state_dir, record_path(state_dir, provider, bk, "connection"), "binding mismatch")  # type: ignore[arg-type]
        conn = None
    status = _read_validated(state_dir, record_path(state_dir, provider, bk, "status"), validate_status)  # type: ignore[arg-type]
    if status is not None and status["provider"] != provider:
        status = None
    snap = _read_validated(state_dir, record_path(state_dir, provider, bk, "snapshot"), validate_snapshot)  # type: ignore[arg-type]
    if snap is not None and snap["provider"] != provider:
        snap = None
    out.update(connection=conn, status=status, snapshot=snap)
    return out


def load_view_dicts(state_dir: str, bindings: dict) -> dict:
    """Everything one tick needs, as validated dicts; never raises on bad files.

    ``clock_trusted`` is False when ``clock.json`` is unusable, or missing while a snapshot
    exists: cached values' ages then cannot be trusted.
    """
    control = read_control_dict(state_dir)
    clock_state, max_wall = read_clock_state_at(state_dir)
    view: dict = {
        "server_epoch": read_server_epoch_at(state_dir),
        "max_wall_seen": max_wall,
        "clock_trusted": True,
        "dispatch": read_dispatch_at(state_dir),
        "providers": {},
    }
    for p in PROVIDERS:
        if p not in bindings:
            continue
        pv = read_provider_dicts(state_dir, p, bindings[p])
        pv["generation"] = control["providers"][p]["generation"]
        pv["settings_fingerprint"] = control["providers"][p]["settings_fingerprint"]
        view["providers"][p] = pv
    if clock_state == CLOCK_CORRUPT or (
        clock_state == CLOCK_MISSING and (
            any(pv.get("snapshot") is not None for pv in view["providers"].values())
            or any_snapshot_exists(state_dir)
        )
    ):
        view["clock_trusted"] = False
    return view


# Dataclass layer: heavy imports are lazy.


def _types():
    from . import types as t

    return t


def _fsutil():
    from . import fsutil

    return fsutil


def load_view(paths, bindings=None, env=None):
    """``StateView`` for ``bindings`` (default: from the current settings). Never raises."""
    t = _types()
    if bindings is None:
        from . import binding as binding_mod
        from . import settings as settings_mod

        s, _src = settings_mod.load_tick(paths.config_dir, paths.state_dir)
        env = os.environ if env is None else env
        bindings = {} if s is None else {p: binding_mod.binding_for(p, s, env) for p in PROVIDERS}
    raw = load_view_dicts(paths.state_dir, bindings)
    providers = {}
    for p, pv in raw["providers"].items():
        providers[p] = t.ProviderView(
            connection=_safe(t.ConnectionRecord.from_json, pv["connection"]),
            status=_safe(t.ProviderStatus.from_json, pv["status"]),
            snapshot=_safe(t.Snapshot.from_json, pv["snapshot"]),
            generation=pv["generation"],
        )
    return t.StateView(server_epoch=raw["server_epoch"], providers=providers, dispatch=raw["dispatch"])


def _safe(fn, obj):
    if obj is None:
        return None
    try:
        return fn(obj)
    except ValueError:
        return None


def read_control(paths) -> dict:
    c = read_control_dict(paths.state_dir)
    return {p: c["providers"][p]["generation"] for p in PROVIDERS}


def _control_for_write(paths, now: float | None = None) -> dict:
    """Current control for a writer (caller holds the settings lock).

    A missing or corrupt file restarts generations at ``int(now)``, not 0, so an old
    recorded generation can never match again (fail closed).
    """
    status, obj = read_json_status(control_path(paths.state_dir))
    if status == OK:
        try:
            return validate_control(obj)
        except ValueError:
            report_bad_file(paths.state_dir, control_path(paths.state_dir), "schema mismatch")
    elif status == CORRUPT:
        report_bad_file(paths.state_dir, control_path(paths.state_dir), str(obj))
    base = int(time.time() if now is None else now)
    c = default_control_dict()
    for p in PROVIDERS:
        c["providers"][p]["generation"] = max(0, base)
    return c


def write_control(paths, control: dict) -> None:
    """Caller holds the settings lock."""
    data = _types().validate_control(control)
    _fsutil().ensure_private_dir(paths.state_dir)
    _fsutil().atomic_write_json(paths.control, data)


def sync_fingerprints(paths, fingerprints: dict, *, now: float | None = None) -> dict:
    """Bump the generation of each provider whose fingerprint changed; return generations.

    Caller holds the settings lock.
    """
    c = _control_for_write(paths, now)
    changed = not os.path.exists(paths.control)
    for p, fp in fingerprints.items():
        entry = c["providers"][p]
        if entry["settings_fingerprint"] != fp:
            entry["generation"] += 1
            entry["settings_fingerprint"] = fp
            changed = True
    if changed:
        write_control(paths, c)
    return {p: c["providers"][p]["generation"] for p in PROVIDERS}


def _read_record(paths, provider: str, binding_key: str, name: str, from_json):
    try:
        path = record_path(paths.state_dir, provider, binding_key, name)
    except ValueError:
        return None
    d = _read_validated(paths.state_dir, path, lambda o: from_json(o))
    if d is None or getattr(d, "provider", provider) != provider:
        return None
    return d


def read_connection(paths, provider: str, binding_key: str):
    rec = _read_record(paths, provider, binding_key, "connection", _types().ConnectionRecord.from_json)
    if rec is not None and rec.binding_key != binding_key:
        return None
    return rec


def write_connection(paths, rec) -> None:
    data = rec.to_json()
    paths.ensure_binding_dir(rec.provider, rec.binding_key)
    _fsutil().atomic_write_json(paths.connection(rec.provider, rec.binding_key), data)


def read_status(paths, provider: str, binding_key: str):
    return _read_record(paths, provider, binding_key, "status", _types().ProviderStatus.from_json)


def write_status(paths, st, binding_key: str) -> None:
    data = st.to_json()
    paths.ensure_binding_dir(st.provider, binding_key)
    _fsutil().atomic_write_json(paths.status(st.provider, binding_key), data)


def read_snapshot(paths, provider: str, binding_key: str):
    return _read_record(paths, provider, binding_key, "snapshot", _types().Snapshot.from_json)


def write_snapshot(paths, snap, binding_key: str) -> None:
    if not snap.windows:
        raise ValueError("refusing to write a snapshot without windows")
    data = snap.to_json()
    paths.ensure_binding_dir(snap.provider, binding_key)
    _fsutil().atomic_write_json(paths.snapshot(snap.provider, binding_key), data)


def delete_snapshot(paths, provider: str, binding_key: str) -> bool:
    try:
        os.unlink(paths.snapshot(provider, binding_key))
        return True
    except FileNotFoundError:
        return False


def delete_all_snapshots(paths) -> bool:
    """Remove every snapshot (clock repair); True only when all are known to be gone.

    On False the caller must not establish a new clock baseline.
    """
    complete = True
    root = os.path.join(paths.state_dir, "providers")
    for provider in PROVIDERS:
        pdir = os.path.join(root, provider)
        try:
            keys = os.listdir(pdir)
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        for key in keys:
            if not is_binding_key(key):
                continue
            try:
                os.unlink(os.path.join(pdir, key, "snapshot.json"))
            except FileNotFoundError:
                pass
            except OSError:
                complete = False
    return complete


def read_server_epoch(paths) -> str | None:
    return read_server_epoch_at(paths.state_dir)


def rotate_server_epoch_locked(paths, now: float | None = None) -> str:
    """Write a fresh server epoch (caller holds the settings lock)."""
    now = time.time() if now is None else now
    epoch = os.urandom(16).hex()
    _fsutil().ensure_private_dir(paths.state_dir)
    _fsutil().atomic_write_json(paths.server_epoch, {"schema_version": SCHEMA_VERSION, "epoch": epoch, "started_at": float(now)})
    note_wall_clock(paths, now)
    return epoch


def rotate_server_epoch(paths, now: float | None = None) -> str:
    """Start a new server epoch, forcing re-verification; takes the settings lock so it
    never interleaves with a publish."""
    fsutil = _fsutil()
    with fsutil.file_lock(paths.settings_lock, blocking=True):
        return rotate_server_epoch_locked(paths, now)


def ensure_server_epoch_locked(paths, now: float | None = None) -> str:
    """Existing epoch, or a new one when missing (caller holds the settings lock)."""
    epoch = read_server_epoch(paths)
    return epoch if epoch is not None else rotate_server_epoch_locked(paths, now)


def read_clock(paths) -> float | None:
    return read_clock_at(paths.state_dir)


def read_clock_state(paths) -> tuple[str, float | None]:
    return read_clock_state_at(paths.state_dir)


def note_wall_clock(paths, now: float, *, reset: bool = False) -> float:
    prev = None if reset else read_clock(paths)
    value = float(now) if prev is None or now > prev else prev
    if value != prev:
        _fsutil().ensure_private_dir(paths.state_dir)
        _fsutil().atomic_write_json(paths.clock, {"max_wall_seen": value})
    return value


def read_salt(paths) -> bytes:
    """The account-key salt, created on first use."""
    path = paths.salt

    def _load() -> bytes | None:
        try:
            text = read_regular_bytes(path, 256).decode("ascii").strip()
            raw = bytes.fromhex(text)
        except (FileNotFoundError, UnicodeDecodeError, ValueError):
            return None
        return raw if len(raw) == 32 else None

    existing = _load()
    if existing is not None:
        return existing
    fsutil = _fsutil()
    fsutil.ensure_private_dir(paths.state_dir)
    # Double-checked under a lock: concurrent first users all get the same salt.
    with fsutil.file_lock(os.path.join(paths.locks_dir, "salt.lock"), blocking=True):
        if _load() is None:
            fsutil.atomic_write_text(path, os.urandom(32).hex() + "\n")
    salt = _load()
    if salt is None:
        raise OSError("could not create the account-key salt")
    return salt


__all__ = [
    "load_view_dicts",
    "read_provider_dicts",
    "read_control_dict",
    "read_server_epoch_at",
    "read_clock_at",
    "read_dispatch_at",
    "load_view",
    "read_control",
    "sync_fingerprints",
    "write_control",
    "read_connection",
    "write_connection",
    "read_status",
    "write_status",
    "read_snapshot",
    "write_snapshot",
    "delete_snapshot",
    "read_server_epoch",
    "rotate_server_epoch",
    "rotate_server_epoch_locked",
    "ensure_server_epoch_locked",
    "read_clock",
    "note_wall_clock",
    "read_salt",
]
