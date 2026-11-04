"""Claude Code usage collector (protocol verified on Claude Code 2.1.316).

``auth status --json`` before and after one stream-json session that sends
``initialize`` then ``get_usage``; usage is published only when both auth runs prove the
same account key.
"""

from __future__ import annotations

import itertools
import os
import pwd
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .. import constants
from ..fsutil import ensure_private_dir, loads_strict, sha256_hex
from ..procutil import (
    JsonlSession,
    ProfileConflict,
    SessionBudgetExceeded,
    SessionExited,
    SessionLimits,
    SessionProtocolError,
    SessionStderrExceeded,
    SEND_UNDER_ADMISSION_SECONDS,
    SessionAdmissionRevoked,
    SessionSpawnError,
    SessionTimeout,
    child_env,
    admission_section,
    cli_version as _query_cli_version,
    present_vars,
    profile_env,
    run_capture,
)
from ..types import (
    ADMISSION_REVOKED,
    PROVIDER_SOURCES,
    AuthResult,
    CollectResult,
    Window,
    is_finite_number,
    is_valid_percent,
    iso_utc,
    iso_utc_precise,
    make_note,
    parse_iso_utc,
    raw_percent_or_none,
)

PROVIDER = "claude"
SOURCE = PROVIDER_SOURCES[PROVIDER]

# Reported by name only in ``env_conflicts``; never removed from the child env.
CLAUDE_AUTH_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

PROFILE_VAR = "CLAUDE_CONFIG_DIR"

SUBSCRIPTION_AUTH_METHODS = frozenset({"claude.ai"})
API_AUTH_METHODS = frozenset({"api_key", "third_party"})

SESSION_FLAGS: tuple[str, ...] = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--safe-mode",
    "--no-session-persistence",
)

# A model turn started: abort the session.
TURN_MESSAGE_TYPES = frozenset({"assistant", "user", "result", "stream_event"})

# (id, duration in seconds), in display order.
CANONICAL_WINDOWS: tuple[tuple[str, int], ...] = (("five_hour", 18000), ("seven_day", 604800))


def default_managed_settings_dir() -> str:
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    return "/etc/claude-code"


MANAGED_PREFERENCES_DIR = "/Library/Managed Preferences"
MANAGED_PREFERENCES_DOMAIN = "com.anthropic.claudecode"


def default_managed_preferences() -> tuple[str, ...]:
    """MDM-delivered preference plists the CLI reads on macOS: per-user, then device-level.

    The per-user path uses the account name of the current uid, as the CLI does; without
    one only the device-level path applies.
    """
    if sys.platform != "darwin":
        return ()
    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        name = ""
    out = [f"{MANAGED_PREFERENCES_DIR}/{name}/{MANAGED_PREFERENCES_DOMAIN}.plist"] if name else []
    out.append(f"{MANAGED_PREFERENCES_DIR}/{MANAGED_PREFERENCES_DOMAIN}.plist")
    return tuple(out)


_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")


def safe_token(value: object) -> str:
    """Short identifier safe for ``detail``, else ``other``."""
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value
    return "other"


def account_key(salt: bytes, provider: str, *identifiers: str) -> str:
    return "acct:" + sha256_hex(salt, provider, *identifiers)[:24]


def _nonempty_str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


@dataclass(frozen=True)
class _AuthParse:
    result: AuthResult
    gate: str | None  # salted digest of (email, orgName)


def _gate_digest(salt: bytes, email: str | None, org_name: str | None) -> str | None:
    if email is None or org_name is None:
        return None
    return sha256_hex(salt, PROVIDER, "identity-gate", email, org_name)


def _parse_auth_full(
    obj: object,
    *,
    profile_dir: str,
    salt: bytes,
    env: Mapping[str, str],
    cwd: str | None = None,
) -> _AuthParse:
    try:
        return _parse_auth_impl(obj, profile_dir=profile_dir, salt=salt, env=env, cwd=cwd)
    except Exception:  # noqa: BLE001
        detail = "auth_status_unparsable"
        res = AuthResult("error", None, None, detail, True, present_vars(env, CLAUDE_AUTH_VARS))
        return _AuthParse(res, None)


def _parse_auth_impl(
    obj: object,
    *,
    profile_dir: str,
    salt: bytes,
    env: Mapping[str, str],
    cwd: str | None,
) -> _AuthParse:
    conflicts = present_vars(env, CLAUDE_AUTH_VARS)

    def res(state: str, detail: str | None, *, key: str | None = None, cfg: str | None = None, retry: bool = False) -> AuthResult:
        return AuthResult(state=state, account_key=key, config_dir=cfg, detail=detail, retryable=retry, env_conflicts=conflicts)

    if not isinstance(obj, dict):
        return _AuthParse(res("error", "auth_status_unparsable", retry=True), None)
    logged_in = obj.get("loggedIn")
    method = obj.get("authMethod")
    if not isinstance(logged_in, bool) or not isinstance(method, str):
        return _AuthParse(res("error", "auth_status_unparsable", retry=True), None)

    cfg_raw = obj.get("configDirectory")
    cfg_real: str | None = None
    if isinstance(cfg_raw, str) and cfg_raw:
        base = cwd or os.getcwd()
        cfg_real = os.path.realpath(cfg_raw if os.path.isabs(cfg_raw) else os.path.join(base, cfg_raw))
        if cfg_real != os.path.realpath(profile_dir):
            return _AuthParse(res("profile_mismatch", "config_directory_mismatch", cfg=cfg_real), None)
    elif cfg_raw is not None:
        return _AuthParse(res("error", "auth_status_unparsable", retry=True), None)

    if not logged_in:
        return _AuthParse(res("logged_out", f"auth_method:{safe_token(method)}", cfg=cfg_real), None)
    if obj.get("apiKeySource") is not None:
        return _AuthParse(res("api_auth", "api_key_source", cfg=cfg_real), None)
    if method in API_AUTH_METHODS:
        return _AuthParse(res("api_auth", f"auth_method:{method}", cfg=cfg_real), None)
    if method not in SUBSCRIPTION_AUTH_METHODS:
        return _AuthParse(res("unsupported_auth", f"auth_method:{safe_token(method)}", cfg=cfg_real), None)
    provider_kind = obj.get("apiProvider")
    if provider_kind is not None and provider_kind != "firstParty":
        return _AuthParse(res("unsupported_auth", f"api_provider:{safe_token(provider_kind)}", cfg=cfg_real), None)
    if cfg_real is None:
        # Subscription is only accepted for a profile the CLI confirms.
        return _AuthParse(res("profile_mismatch", "config_directory_missing"), None)

    email = _nonempty_str(obj.get("email"))
    org_id = _nonempty_str(obj.get("orgId"))
    org_name = _nonempty_str(obj.get("orgName"))
    key = account_key(salt, PROVIDER, org_id, email) if email and org_id else None
    return _AuthParse(
        res("subscription", "auth_method:claude.ai", key=key, cfg=cfg_real),
        _gate_digest(salt, email, org_name),
    )


def parse_auth_status(
    obj: object,
    *,
    profile_dir: str,
    salt: bytes,
    env: Mapping[str, str],
    cwd: str | None = None,
) -> AuthResult:
    """Classify a parsed ``claude auth status --json`` object.

    Emails and org ids are only hashed into ``account_key``; they never appear in the result.
    """
    return _parse_auth_full(obj, profile_dir=profile_dir, salt=salt, env=env, cwd=cwd).result


def _init_identity(body: object, salt: bytes) -> tuple[str, str | None]:
    """``("ok", digest)``, ``("rejected", None)`` (API key / no token) or ``("unproven", None)``."""
    if not isinstance(body, dict):
        return "unproven", None
    acct = body.get("account")
    if not isinstance(acct, dict):
        return "unproven", None
    if acct.get("apiKeySource") is not None:
        return "rejected", None
    if acct.get("tokenSource") == "none":
        return "rejected", None
    digest = _gate_digest(salt, _nonempty_str(acct.get("email")), _nonempty_str(acct.get("organization")))
    if digest is None:
        return "unproven", None
    return "ok", digest


class _ShapeError(Exception):
    pass


def _parse_reset(value: object) -> tuple[bool, str | None]:
    """``(valid, iso)``; None is valid (no reset known)."""
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    try:
        ts = parse_iso_utc(value)
    except ValueError:
        return False, None
    if not is_finite_number(ts) or ts <= 0:
        return False, None
    return True, iso_utc(ts)  # floored: hides a value sooner, never later


def _spelling_for(spelling: str | None, profile_dir: str) -> str:
    if isinstance(spelling, str) and os.path.isabs(spelling) and os.path.realpath(spelling) == os.path.realpath(profile_dir):
        return spelling
    return profile_dir


def _claude_window(wid: str, duration: int, value: object, notes: list[str]) -> Window | None:
    placeholder = Window(id=wid, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=None, resets_at=None)
    if value is None:
        notes.append(make_note(f"window_not_provided:{wid}"))
        return None
    if not isinstance(value, dict):
        raise _ShapeError
    util = value.get("utilization")
    if util is None:
        notes.append(make_note(f"window_not_provided:{wid}"))
        return None
    raw = raw_percent_or_none(util)
    reset_ok, reset_iso = _parse_reset(value.get("resets_at"))
    if raw is None:
        notes.append(make_note("invalid_percent"))
        return placeholder
    if not is_valid_percent(raw):
        notes.append(make_note("percent_out_of_range"))
        return Window(id=wid, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=raw, resets_at=reset_iso)
    if not reset_ok:
        # Without a reset time the value cannot be expired: never render it.
        notes.append(make_note("invalid_reset"))
        return Window(id=wid, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=raw, resets_at=None)
    return Window(id=wid, scope="all", duration_seconds=duration, used_percent=float(raw), raw_used_percent=raw, resets_at=reset_iso)


def _dedupe(notes: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for n in notes:
        seen.setdefault(n, None)
    return tuple(seen)


def parse_usage_response(obj: object) -> tuple[str, tuple[Window, ...], tuple[str, ...]]:
    """``(outcome, windows, notes)`` for a ``get_usage`` payload. Never raises.

    ``rate_limits_available is False`` is ``unavailable``, never "unlimited".
    """
    try:
        return _parse_usage_impl(obj)
    except Exception:  # noqa: BLE001
        return "protocol_unsupported", (), ()


# The CLI may answer from its local cache, so an answer without a usable ``as_of`` is
# never published: a cached answer must not renew a value's lifetime.
CACHED_ONLY = "cached_only"
AS_OF_SKEW_TOLERANCE = 60.0


def usage_as_of(obj: object) -> float | None:
    """``rate_limits.seven_day_breakdown.as_of`` as Unix seconds, or None. Never raises."""
    try:
        value = obj["rate_limits"]["seven_day_breakdown"]["as_of"]  # type: ignore[index]
        ts = parse_iso_utc(value)
    except Exception:  # noqa: BLE001
        return None
    return ts if is_finite_number(ts) else None


def _parse_usage_impl(obj: object) -> tuple[str, tuple[Window, ...], tuple[str, ...]]:
    if not isinstance(obj, dict):
        return "protocol_unsupported", (), ()
    available = obj.get("rate_limits_available")
    sub_type = obj.get("subscription_type")
    if not isinstance(available, bool) or not (sub_type is None or isinstance(sub_type, str)):
        return "protocol_unsupported", (), ()
    rl = obj.get("rate_limits")
    if available is False:
        return "unavailable", (), (make_note("rate_limits_unavailable"),)
    if rl is None:
        return "transient_error", (), (make_note("rate_limits_null"),)
    if not isinstance(rl, dict):
        return "protocol_unsupported", (), ()
    if not any(wid in rl for wid, _ in CANONICAL_WINDOWS):
        return "protocol_unsupported", (), ()
    notes: list[str] = []
    try:
        parsed = [_claude_window(wid, dur, rl.get(wid), notes) for wid, dur in CANONICAL_WINDOWS]
        windows = tuple(w for w in parsed if w is not None)
    except _ShapeError:
        return "protocol_unsupported", (), ()
    canonical = {wid for wid, _ in CANONICAL_WINDOWS}
    ignored = sum(
        1
        for k, v in rl.items()
        if k not in canonical and isinstance(v, dict) and "utilization" in v and "resets_at" in v
    )
    if ignored:
        notes.append(make_note("ignored_model_buckets", ignored))
    return "ok", windows, _dedupe(notes)


def _path_present(path: str) -> bool:
    """Anything at ``path``, a dangling link included; an unreadable parent fails closed."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _drop_in_dir_present(path: str) -> bool:
    try:
        entries = os.listdir(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # not a directory, or unreadable: fail closed
    return any(not name.startswith(".") for name in entries)


def managed_policy_sources(root: str | None = None, preferences: Iterable[str] | None = None) -> list[str]:
    """Paths of the admin-managed settings sources that exist (fail closed).

    ``root`` holds ``managed-settings.json`` and ``managed-settings.d``; ``preferences``
    are the managed-preferences plists. None selects the platform defaults.
    """
    root = default_managed_settings_dir() if root is None else root
    prefs = default_managed_preferences() if preferences is None else tuple(preferences)
    found = []
    settings_file = os.path.join(root, "managed-settings.json")
    if _path_present(settings_file):
        found.append(settings_file)
    drop_in = os.path.join(root, "managed-settings.d")
    if _drop_in_dir_present(drop_in):
        found.append(drop_in)
    found.extend(p for p in prefs if _path_present(p))
    return found


def managed_policy_present(root: str, preferences: Iterable[str] = ()) -> bool:
    """True if admin-managed settings exist under ``root`` or as a managed-preferences plist.

    ``--safe-mode`` does not disable managed settings, whose hooks could run at session
    start, so any such source blocks collection.
    """
    return bool(managed_policy_sources(root, preferences))


class _Abort(Exception):
    def __init__(self, code: str, note: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.note = note


IDENTITY_UNPROVEN = "identity_unproven"


@dataclass(frozen=True)
class _SessionData:
    init_supported: bool
    init_body: object
    usage_ok: bool
    usage_body: object
    received_at: float | None


def _check_message(msg: dict) -> None:
    if msg.get("type") in TURN_MESSAGE_TYPES:
        raise _Abort("unexpected_turn", "unexpected_turn")


class ClaudeCollector:
    provider = PROVIDER

    def __init__(
        self,
        *,
        executable: str,
        profile_dir: str,
        work_dir: str,
        env: Mapping[str, str],
        salt: bytes,
        limits: SessionLimits | None = None,
        lock_fd: int | None = None,
        managed_settings_dir: str | None = None,
        profile_spelling: str | None = None,
        managed_preferences: Iterable[str] | None = None,
    ) -> None:
        self.executable = executable
        self.profile_dir = profile_dir
        # Claude keys its keychain entry on the literal CLAUDE_CONFIG_DIR string, so keep
        # the user's spelling; comparisons use the realpath.
        self.profile_spelling = _spelling_for(profile_spelling, profile_dir)
        self.work_dir = work_dir
        self._env_in = dict(env)
        self._salt = bytes(salt)
        self.limits = limits or SessionLimits(timeout_s=constants.COLLECT_TIMEOUT, grace_s=constants.TERM_GRACE)
        # A lock fd registered by the refresh is withheld from children and only marks the spawn.
        self._pass_fds = () if lock_fd is None else (lock_fd,)
        self.managed_settings_dir = managed_settings_dir or default_managed_settings_dir()
        self.managed_preferences = (default_managed_preferences() if managed_preferences is None
                                    else tuple(managed_preferences))
        self._req = itertools.count(1)

    def _default_profile(self) -> str:
        home = self._env_in.get("HOME") or os.path.expanduser("~")
        return os.path.realpath(os.path.join(home, ".claude"))

    def _child_env(self) -> dict[str, str]:
        """Raises ProfileConflict when the inherited env pins another profile."""
        # Claude derives its config file and keychain entry from whether the variable is
        # set at all, so it is exported only for a non-default profile.
        is_default = os.path.realpath(self.profile_dir) == self._default_profile()
        extra = profile_env(self._env_in, PROFILE_VAR, self.profile_spelling, cwd=self.work_dir, set_when_unset=not is_default)
        return child_env(self._env_in, extra=extra)

    def _conflicts(self) -> tuple[str, ...]:
        return present_vars(self._env_in, CLAUDE_AUTH_VARS)

    def _prepare_cwd(self) -> bool:
        try:
            ensure_private_dir(self.work_dir)
            return True
        except OSError:
            return False

    def cli_version(self, admit: Callable[[], bool] | None = None) -> str | None:
        try:
            env = self._child_env()
        except ProfileConflict:
            return None
        if not self._prepare_cwd():
            return None
        return _query_cli_version(self.executable, cwd=self.work_dir, env=env, pass_fds=self._pass_fds, admit=admit)

    def check_auth(self, timeout_s: float, admit: Callable[[], bool] | None = None) -> AuthResult:
        return self._auth(timeout_s, admit).result

    def _err(self, detail: str, *, retry: bool = True, state: str = "error") -> AuthResult:
        return AuthResult(state=state, account_key=None, config_dir=None, detail=detail, retryable=retry, env_conflicts=self._conflicts())

    def _auth(self, timeout_s: float, admit: Callable[[], bool] | None) -> _AuthParse:
        try:
            env = self._child_env()
        except ProfileConflict:
            return _AuthParse(self._err("env_profile_conflict", retry=False, state="profile_mismatch"), None)
        if not self._prepare_cwd():
            return _AuthParse(self._err("work_dir_unavailable"), None)
        res = run_capture(
            [self.executable, "auth", "status", "--json"],
            cwd=self.work_dir,
            env=env,
            timeout_s=max(0.0, timeout_s),
            stdout_budget=64 << 10,
            grace_s=self.limits.grace_s,
            pass_fds=self._pass_fds,
            admit=admit,
        )
        if res.spawn_error == ADMISSION_REVOKED:
            return _AuthParse(self._err(ADMISSION_REVOKED), None)
        if res.spawn_error is not None:
            return _AuthParse(self._err("spawn_failed"), None)
        if res.timed_out:
            return _AuthParse(self._err("timeout"), None)
        if res.overflow:
            return _AuthParse(self._err("response_too_large"), None)
        try:
            obj = loads_strict(res.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return _AuthParse(self._err("auth_status_unparsable"), None)
        return _parse_auth_full(obj, profile_dir=self.profile_dir, salt=self._salt, env=self._env_in, cwd=self.work_dir)

    def _result(
        self,
        outcome: str,
        *,
        before: AuthResult | None = None,
        after: AuthResult | None = None,
        windows: tuple[Window, ...] = (),
        error_code: str | None = None,
        notes: tuple[str, ...] = (),
        version: str | None = None,
        received_at: float | None = None,
        observed_at: float | None = None,
    ) -> CollectResult:
        return CollectResult(
            outcome=outcome,
            windows=windows if outcome == "ok" else (),
            auth_before=before,
            auth_after=after,
            source=SOURCE,
            source_observed_at=iso_utc_precise(observed_at) if outcome == "ok" and observed_at is not None else None,
            error_code=error_code,
            retry_not_before=None,
            cli_version=version,
            notes=notes,
            usage_received_at=received_at if outcome == "ok" else None,
        )

    @staticmethod
    def _auth_outcome(a: AuthResult) -> tuple[str, str | None]:
        if a.state == "error":
            return "transient_error", a.detail
        return "auth_error", a.state

    def collect(self, timeout_s: float, admit: Callable[[], bool] | None = None) -> CollectResult:
        return self._collect(time.monotonic() + max(0.0, timeout_s), admit)

    def _collect(self, deadline: float, admit: Callable[[], bool] | None) -> CollectResult:
        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def revoked(**kw) -> CollectResult:
            return self._result("transient_error", error_code=ADMISSION_REVOKED, **kw)

        with admission_section(admit) as ok:
            if not ok:
                return revoked()
            version = self.cli_version()
        if managed_policy_present(self.managed_settings_dir, self.managed_preferences):
            return self._result(
                "protocol_unsupported", error_code="managed_policy", notes=(make_note("managed_policy_present"),), version=version
            )

        before_p = self._auth(remaining(), admit)
        before = before_p.result
        if before.detail == ADMISSION_REVOKED:
            return revoked(before=before, version=version)
        if before.state != "subscription":
            outcome, code = self._auth_outcome(before)
            return self._result(outcome, before=before, error_code=code, version=version)

        try:
            data = self._session(deadline, admit)
        except _Abort as ab:
            if ab.code == ADMISSION_REVOKED:
                return revoked(before=before, version=version)
            notes = (make_note(ab.note),) if ab.note else ()
            return self._result("protocol_unsupported", before=before, error_code=ab.code, notes=notes, version=version)
        except SessionSpawnError:
            return self._result("transient_error", before=before, error_code="spawn_failed", version=version)
        except SessionTimeout:
            return self._result("transient_error", before=before, error_code="timeout", version=version)
        except SessionExited:
            return self._result("transient_error", before=before, error_code="session_exited", version=version)
        except SessionBudgetExceeded:
            return self._result("protocol_unsupported", before=before, error_code="response_too_large", version=version)
        except SessionStderrExceeded:
            return self._result("transient_error", before=before, error_code="stderr_too_large", version=version)
        except SessionProtocolError:
            return self._result("protocol_unsupported", before=before, error_code="malformed_output", version=version)

        if not data.usage_ok:
            return self._result("transient_error", before=before, error_code="control_error", version=version)

        # Identity proof: a fresh auth run after the session must yield the same key.
        after = self._auth(remaining(), admit).result
        if after.detail == ADMISSION_REVOKED:
            return revoked(before=before, version=version)
        if after.state != "subscription":
            outcome, code = self._auth_outcome(after)
            return self._result(outcome, before=before, after=after, error_code=code, version=version)
        if before.account_key is None or after.account_key is None:
            return self._result("transient_error", before=before, after=after, error_code=IDENTITY_UNPROVEN, version=version)
        if after.account_key != before.account_key:
            return self._result("identity_changed", before=before, after=after, error_code="account_changed", version=version)

        # The session's own account must also match auth before.
        if data.init_supported:
            kind, digest = _init_identity(data.init_body, self._salt)
            if kind == "rejected" or (kind == "ok" and before_p.gate is not None and digest != before_p.gate):
                return self._result("identity_changed", before=before, after=after, error_code="identity_gate_failed", version=version)
            if kind != "ok" or before_p.gate is None:
                return self._result("transient_error", before=before, after=after, error_code=IDENTITY_UNPROVEN, version=version)

        outcome, windows, notes = parse_usage_response(data.usage_body)
        code = None if outcome == "ok" else ("unexpected_shape" if outcome == "protocol_unsupported" else outcome)
        if outcome == "ok" and data.received_at is None:  # pragma: no cover - set by every successful read
            return self._result("transient_error", before=before, after=after, error_code="internal", version=version)
        observed = None
        if outcome == "ok":
            as_of = usage_as_of(data.usage_body)
            if as_of is None or as_of > data.received_at + AS_OF_SKEW_TOLERANCE:
                return self._result("transient_error", before=before, after=after, error_code=CACHED_ONLY,
                                    notes=notes, version=version)
            observed = min(as_of, data.received_at)
        return self._result(
            outcome,
            before=before,
            after=after,
            windows=windows,
            error_code=code,
            notes=notes,
            version=version,
            received_at=data.received_at,
            observed_at=observed,
        )

    def _session(self, deadline: float, admit: Callable[[], bool] | None) -> _SessionData:
        """Run initialize + get_usage and validate everything the session printed."""
        env = self._child_env()
        if not self._prepare_cwd():
            raise SessionSpawnError("work dir unavailable")
        n = next(self._req)
        init_id = f"quotaribbon-init-{n}"
        usage_id = f"quotaribbon-usage-{n}"
        try:
            session = JsonlSession(
                [self.executable, *SESSION_FLAGS], cwd=self.work_dir, env=env, limits=self.limits, pass_fds=self._pass_fds,
                admit=admit,
            )
        except SessionAdmissionRevoked:
            raise _Abort(ADMISSION_REVOKED) from None
        stopped = False
        data: _SessionData | None = None

        def send_admitted(msg: dict) -> bool:
            with admission_section(admit) as ok:
                if ok:
                    session.send(msg, deadline=min(deadline, time.monotonic() + SEND_UNDER_ADMISSION_SECONDS))
                return ok

        with session:
            if not send_admitted({"type": "control_request", "request_id": init_id, "request": {"subtype": "initialize"}}):
                stopped = True
            else:
                init_resp = session.read_until(self._matcher(init_id), deadline)
                init_supported, init_body = self._unwrap(init_resp)
                if not send_admitted({
                    "type": "control_request",
                    "request_id": usage_id,
                    # Without skip_behaviors the CLI scans seven days of local transcripts.
                    "request": {"subtype": "get_usage", "skip_behaviors": True},
                }):
                    stopped = True
                else:
                    usage_resp = session.read_until(self._matcher(usage_id), deadline)
                    received_at = session.last_received_at
                    usage_ok, usage_body = self._unwrap(usage_resp)
                    # Trailing output is validated too: a late model turn rejects the result.
                    session.finish(_check_message)
                    data = _SessionData(init_supported, init_body, usage_ok, usage_body if usage_ok else None, received_at)
        if stopped or data is None:
            raise _Abort(ADMISSION_REVOKED)
        return data

    @staticmethod
    def _matcher(request_id: str) -> Callable[[dict], bool]:
        def match(msg: dict) -> bool:
            _check_message(msg)
            if msg.get("type") != "control_response":
                return False
            resp = msg.get("response")
            return isinstance(resp, dict) and resp.get("request_id") == request_id
        return match

    @staticmethod
    def _unwrap(msg: dict) -> tuple[bool, object]:
        resp = msg["response"]
        sub = resp.get("subtype")
        if sub == "success":
            return True, resp.get("response")
        if sub == "error":
            return False, None
        raise _Abort("unexpected_shape")


__all__ = [
    "PROVIDER",
    "SOURCE",
    "CLAUDE_AUTH_VARS",
    "IDENTITY_UNPROVEN",
    "CACHED_ONLY",
    "usage_as_of",
    "TURN_MESSAGE_TYPES",
    "SESSION_FLAGS",
    "ClaudeCollector",
    "parse_auth_status",
    "parse_usage_response",
    "managed_policy_present",
    "managed_policy_sources",
    "default_managed_settings_dir",
    "default_managed_preferences",
    "account_key",
    "safe_token",
]
