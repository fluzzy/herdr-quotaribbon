"""Codex usage collector (protocol verified on codex-cli 0.166.0).

One ``codex app-server`` JSON-RPC session (no ``"jsonrpc"`` field): initialize,
initialized, ``account/read``, ``account/rateLimits/read``, ``account/read``. Usage is
published only when both reads prove the same stable ``chatgptAccountId``; an email
alone never identifies an account.
"""

from __future__ import annotations

import itertools
import os
import re
import time
from typing import Callable, Mapping

from .. import __version__, constants
from ..fsutil import ensure_private_dir, sha256_hex
from ..procutil import (
    JsonlSession,
    ProfileConflict,
    SessionBudgetExceeded,
    SessionStderrExceeded,
    SessionExited,
    SessionLimits,
    SessionProtocolError,
    SEND_UNDER_ADMISSION_SECONDS,
    SessionAdmissionRevoked,
    SessionSpawnError,
    SessionTimeout,
    SessionError,
    child_env,
    admission_section,
    cli_version as _query_cli_version,
    present_vars,
    profile_env,
)
from ..types import (
    ADMISSION_REVOKED,
    PROVIDER_SOURCES,
    AuthResult,
    MAX_JSON_INT,
    CollectResult,
    Window,
    is_finite_number,
    is_valid_percent,
    iso_utc,
    make_note,
    raw_percent_or_none,
)
from .claude import _dedupe, _nonempty_str, _spelling_for, account_key, safe_token

PROVIDER = "codex"
SOURCE = PROVIDER_SOURCES[PROVIDER]
PROFILE_VAR = "CODEX_HOME"

# Reported by name only in ``env_conflicts``; never removed from the child env.
CODEX_AUTH_VARS: tuple[str, ...] = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")

CLIENT_INFO = {"name": "quotaribbon", "title": "QuotaRibbon", "version": __version__}
COMMON_BUCKET = "codex"

SUBSCRIPTION_ACCOUNT_TYPES = frozenset({"chatgpt"})
API_ACCOUNT_TYPES = frozenset({"apiKey", "amazonBedrock", "gcp"})

# Every JSON-RPC method this collector may ever send.
ALLOWED_METHODS: tuple[str, ...] = ("initialize", "initialized", "account/read", "account/rateLimits/read")

IDENTITY_UNPROVEN = "identity_unproven"

# Any of these means work is running in this app-server: reject conservatively.
TURN_METHOD_PREFIXES: tuple[str, ...] = (
    "turn/",
    "item/",
    "thread/",
    "rawResponse/",
    "rawResponseItem/",
    "model/",
    "hook/",
    "command/",
    "process/",
)
TURN_METHODS = frozenset({"applyPatchApproval", "execCommandApproval"})

_SERVER_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")

_RATE_LIMIT_ERROR_INFOS = frozenset({"rateLimitExceeded"})
# Rejects millisecond resetsAt values.
_MAX_UNIX_SECONDS = 10_000_000_000


def parse_account_read(obj: object, *, salt: bytes, env: Mapping[str, str]) -> AuthResult:
    """Classify an ``account/read`` result. Never raises.

    ``account_key`` is None without ``workspaceRouting.chatgptAccountId``.
    """
    try:
        return _parse_account_impl(obj, salt=salt, env=env)
    except Exception:  # noqa: BLE001
        conflicts = present_vars(env, CODEX_AUTH_VARS)
        return AuthResult("error", None, None, "account_read_unparsable", True, conflicts)


def _parse_account_impl(obj: object, *, salt: bytes, env: Mapping[str, str]) -> AuthResult:
    conflicts = present_vars(env, CODEX_AUTH_VARS)

    def res(state: str, detail: str | None, key: str | None = None, retry: bool = False) -> AuthResult:
        return AuthResult(state=state, account_key=key, config_dir=None, detail=detail, retryable=retry, env_conflicts=conflicts)

    if not isinstance(obj, dict) or "account" not in obj:
        return res("error", "account_read_unparsable", retry=True)
    acct = obj["account"]
    if acct is None:
        return res("logged_out", "account:none")
    if not isinstance(acct, dict) or not isinstance(acct.get("type"), str):
        return res("error", "account_read_unparsable", retry=True)
    kind = acct["type"]
    if kind in API_ACCOUNT_TYPES:
        return res("api_auth", f"auth_method:{kind}")
    if kind not in SUBSCRIPTION_ACCOUNT_TYPES:
        return res("unsupported_auth", f"auth_method:{safe_token(kind)}")
    email = _nonempty_str(acct.get("email"))
    routing = obj.get("workspaceRouting")
    acct_id = _nonempty_str(routing.get("chatgptAccountId")) if isinstance(routing, dict) else None
    key = account_key(salt, PROVIDER, email or "", acct_id) if acct_id else None
    return res("subscription", "auth_method:chatgpt", key)


class _ShapeError(Exception):
    pass


_MAX_MINUTES = MAX_JSON_INT // 60


def _int_minutes(v: object) -> int | None:
    if not is_finite_number(v):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    m = int(v)
    return m if 0 < m <= _MAX_MINUTES else None


def _codex_window(slot: str, value: object, notes: list[str]) -> Window | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _ShapeError
    minutes = _int_minutes(value.get("windowDurationMins"))
    if minutes is None:
        notes.append(make_note("unknown_duration"))
        return None
    duration = minutes * 60
    raw = raw_percent_or_none(value.get("usedPercent"))
    reset_v = value.get("resetsAt")
    reset_iso: str | None = None
    reset_ok = True
    if reset_v is not None:
        if not is_finite_number(reset_v):
            reset_ok = False
        elif not 0 < reset_v < _MAX_UNIX_SECONDS:
            reset_ok = False
        else:
            reset_iso = iso_utc(reset_v)
    if raw is None:
        notes.append(make_note("invalid_percent"))
        return Window(id=slot, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=None, resets_at=reset_iso)
    if not is_valid_percent(raw):
        notes.append(make_note("percent_out_of_range"))
        return Window(id=slot, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=raw, resets_at=reset_iso)
    if not reset_ok:
        notes.append(make_note("invalid_reset"))
        return Window(id=slot, scope="all", duration_seconds=duration, used_percent=None, raw_used_percent=raw, resets_at=None)
    return Window(id=slot, scope="all", duration_seconds=duration, used_percent=float(raw), raw_used_percent=raw, resets_at=reset_iso)


def parse_rate_limits(obj: object) -> tuple[str, tuple[Window, ...], tuple[str, ...]]:
    """``(outcome, windows, notes)`` from the ``codex`` bucket. Never raises.

    ``primary`` may be the 7-day window, so windows are sorted by duration.
    """
    try:
        return _parse_rate_limits_impl(obj)
    except Exception:  # noqa: BLE001
        return "protocol_unsupported", (), ()


def _parse_rate_limits_impl(obj: object) -> tuple[str, tuple[Window, ...], tuple[str, ...]]:
    if not isinstance(obj, dict):
        return "protocol_unsupported", (), ()
    notes: list[str] = []
    by_id = obj.get("rateLimitsByLimitId")
    bucket: object = None
    if by_id is not None:
        if not isinstance(by_id, dict):
            return "protocol_unsupported", (), ()
        others = sum(1 for k in by_id if k != COMMON_BUCKET)
        if others:
            notes.append(make_note("ignored_model_buckets", others))
        bucket = by_id.get(COMMON_BUCKET)
    else:
        legacy = obj.get("rateLimits")
        if isinstance(legacy, dict) and legacy.get("limitId") == COMMON_BUCKET:
            bucket = legacy
            notes.append(make_note("legacy_bucket_used"))
        elif legacy is not None and not isinstance(legacy, dict):
            return "protocol_unsupported", (), ()
    if bucket is None:
        notes.append(make_note("no_common_bucket"))
        return "unavailable", (), _dedupe(notes)
    if not isinstance(bucket, dict):
        return "protocol_unsupported", (), ()
    try:
        windows = [w for w in (_codex_window(s, bucket.get(s), notes) for s in ("primary", "secondary")) if w is not None]
    except _ShapeError:
        return "protocol_unsupported", (), ()
    if not windows:
        return "unavailable", (), _dedupe(notes)
    windows.sort(key=lambda w: (w.duration_seconds or 0, w.id))
    return "ok", tuple(windows), _dedupe(notes)


def _id_digest(salt: bytes, value: object) -> str | None:
    """For equality checks only; never persisted."""
    v = _nonempty_str(value)
    return None if v is None else sha256_hex(salt, PROVIDER, "account-id", v)


def _is_rate_limit_error(err: object) -> bool:
    """True only for an explicit ``codexErrorInfo`` rate-limit marker; messages are never read."""
    if not isinstance(err, dict):
        return False
    data = err.get("data")
    if not isinstance(data, dict):
        return False
    info = data.get("codexErrorInfo")
    if isinstance(info, str):
        return info in _RATE_LIMIT_ERROR_INFOS
    if isinstance(info, dict):
        for v in info.values():
            if isinstance(v, dict) and v.get("httpStatusCode") == 429:
                return True
    return False


def _is_turn_method(method: str) -> bool:
    return method in TURN_METHODS or method.startswith(TURN_METHOD_PREFIXES)


def _valid_server_id(v: object) -> bool:
    """Only plain ids are echoed back; any other aborts the session."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -MAX_JSON_INT <= v <= MAX_JSON_INT
    return isinstance(v, str) and _SERVER_ID_RE.match(v) is not None


class _Stop(Exception):
    """Deliberate stop: the session closes gracefully (stdin EOF, never signalled)."""

    def __init__(self, outcome: str, code: str | None) -> None:
        super().__init__(code)
        self.outcome = outcome
        self.code = code


class _Violation(Exception):

    def __init__(self, code: str, note: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.note = note


def _check_message(msg: dict) -> None:
    method = msg.get("method")
    if "method" in msg and not isinstance(method, str):
        raise _Violation("malformed_output")
    if isinstance(method, str) and _is_turn_method(method):
        raise _Violation("unexpected_turn", "unexpected_turn")
    if method is not None and "id" in msg and not _valid_server_id(msg["id"]):
        raise _Violation("malformed_output")


class CodexCollector:
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
        profile_spelling: str | None = None,
    ) -> None:
        self.executable = executable
        self.profile_dir = profile_dir
        self.profile_spelling = _spelling_for(profile_spelling, profile_dir)
        self.work_dir = work_dir
        self._env_in = dict(env)
        self._salt = bytes(salt)
        self.limits = limits or SessionLimits(timeout_s=constants.COLLECT_TIMEOUT, grace_s=constants.TERM_GRACE)
        self._pass_fds = () if lock_fd is None else (lock_fd,)
        self._ids = itertools.count(1)

    def _child_env(self) -> dict[str, str]:
        """Always binds ``CODEX_HOME``; raises ProfileConflict on a conflicting value."""
        extra = profile_env(self._env_in, PROFILE_VAR, self.profile_spelling, cwd=self.work_dir, set_when_unset=True)
        return child_env(self._env_in, extra=extra)

    def _conflicts(self) -> tuple[str, ...]:
        return present_vars(self._env_in, CODEX_AUTH_VARS)

    def _err(self, detail: str, *, retry: bool = True, state: str = "error") -> AuthResult:
        return AuthResult(state=state, account_key=None, config_dir=None, detail=detail, retryable=retry, env_conflicts=self._conflicts())

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
        return self._run(timeout_s, admit, usage=False).auth_before or self._err("internal")

    def collect(self, timeout_s: float, admit: Callable[[], bool] | None = None) -> CollectResult:
        return self._run(timeout_s, admit, usage=True)

    def _result(self, outcome: str, *, before=None, after=None, windows=(), code=None, notes=(), version=None,
                received_at: float | None = None) -> CollectResult:
        return CollectResult(
            outcome=outcome,
            windows=tuple(windows) if outcome == "ok" else (),
            auth_before=before,
            auth_after=after,
            source=SOURCE,
            source_observed_at=None,
            error_code=code,
            retry_not_before=None,
            cli_version=version,
            notes=tuple(notes),
            usage_received_at=received_at if outcome == "ok" else None,
        )

    def _run(self, timeout_s: float, admit: Callable[[], bool] | None, *, usage: bool) -> CollectResult:
        deadline = time.monotonic() + max(0.0, timeout_s)
        state: dict[str, object] = {"before": None, "after": None}
        return self._run_inner(deadline, admit, usage, state)

    def _run_inner(self, deadline: float, admit: Callable[[], bool] | None, usage: bool, state: dict) -> CollectResult:
        def revoked(version: str | None = None) -> CollectResult:
            return self._result("transient_error", before=self._err(ADMISSION_REVOKED), code=ADMISSION_REVOKED, version=version)

        try:
            env = self._child_env()
        except ProfileConflict:
            a = self._err("env_profile_conflict", retry=False, state="profile_mismatch")
            return self._result("auth_error", before=a, code="profile_mismatch")
        version = None
        if usage:
            with admission_section(admit) as ok:
                if not ok:
                    return revoked()
                version = self.cli_version()
        if not self._prepare_cwd():
            return self._result("transient_error", before=self._err("work_dir_unavailable"), code="work_dir_unavailable", version=version)
        try:
            session = JsonlSession(
                [self.executable, "app-server"], cwd=self.work_dir, env=env, limits=self.limits, pass_fds=self._pass_fds,
                admit=admit,
            )
        except SessionAdmissionRevoked:
            return revoked(version)
        except SessionSpawnError:
            return self._result("transient_error", before=self._err("spawn_failed"), code="spawn_failed", version=version)
        stop: _Stop | None = None
        result: CollectResult | None = None
        try:
            with session:
                try:
                    result = self._converse(session, deadline, admit, usage, state, version)
                    session.finish(_check_message)
                except _Stop as st:
                    # Leave the with-block normally so the child is reaped, never signalled.
                    stop = st
        except _Violation as v:
            notes = (make_note(v.note),) if v.note else ()
            return self._failure(state, v.code, version, usage, outcome="protocol_unsupported", notes=notes)
        except SessionTimeout:
            return self._failure(state, "timeout", version, usage)
        except SessionExited:
            return self._failure(state, "session_exited", version, usage)
        except SessionBudgetExceeded:
            return self._failure(state, "response_too_large", version, usage, outcome="protocol_unsupported")
        except SessionStderrExceeded:
            return self._failure(state, "stderr_too_large", version, usage)
        except SessionProtocolError:
            return self._failure(state, "malformed_output", version, usage, outcome="protocol_unsupported")
        except SessionError:
            return self._failure(state, "session_error", version, usage)
        if stop is not None:
            before = state["before"] or self._err(stop.code or "error")
            return self._result(stop.outcome, before=before, after=state["after"], code=stop.code, version=version)
        assert result is not None
        return result

    def _failure(self, state: dict, code: str, version: str | None, usage: bool, *, outcome: str = "transient_error",
                 notes: tuple[str, ...] = ()) -> CollectResult:
        # check_auth must never report a subscription from a session that failed.
        before = (state["before"] if usage else None) or self._err(code)
        return self._result(outcome, before=before, after=state["after"] if usage else None, code=code, version=version, notes=notes)

    def _send(self, session: JsonlSession, msg: dict, deadline: float, admit: Callable[[], bool] | None) -> None:
        with admission_section(admit) as ok:
            if not ok:
                raise _Stop("transient_error", ADMISSION_REVOKED)
            session.send(msg, deadline=min(deadline, time.monotonic() + SEND_UNDER_ADMISSION_SECONDS))

    def _request(self, session: JsonlSession, method: str, params: dict | None, deadline: float,
                 admit: Callable[[], bool] | None) -> dict:
        rid = next(self._ids)
        msg: dict = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(session, msg, deadline, admit)

        def match(m: dict) -> bool:
            _check_message(m)
            if "method" in m:
                if "id" in m:
                    # Refuse server→client requests so they never block the session.
                    self._send(session, {"id": m["id"], "error": {"code": -32601, "message": "unsupported by quotaribbon"}},
                               deadline, admit)
                return False
            mid = m.get("id")
            return type(mid) is int and mid == rid and ("result" in m or "error" in m)

        return session.read_until(match, deadline)

    def _converse(self, session, deadline, admit, usage, state, version) -> CollectResult:
        init = self._request(
            session,
            "initialize",
            {"clientInfo": dict(CLIENT_INFO), "capabilities": {"experimentalApi": False, "requestAttestation": False}},
            deadline,
            admit,
        )
        if "error" in init or not isinstance(init.get("result"), dict):
            raise _Stop("protocol_unsupported", "initialize_failed")
        home = init["result"].get("codexHome")
        home_real = os.path.realpath(home) if isinstance(home, str) and os.path.isabs(home) else None
        if home_real is not None and home_real != os.path.realpath(self.profile_dir):
            state["before"] = self._err("codex_home_mismatch", retry=False, state="profile_mismatch")
            raise _Stop("auth_error", "profile_mismatch")
        self._send(session, {"method": "initialized"}, deadline, admit)

        def read_account() -> tuple[AuthResult, str | None]:
            # refreshToken=True would force a token refresh.
            resp = self._request(session, "account/read", {"refreshToken": False}, deadline, admit)
            if "error" in resp:
                return self._err("account_read_error"), None
            res_obj = resp.get("result")
            a = parse_account_read(res_obj, salt=self._salt, env=self._env_in)
            routing_obj = res_obj.get("workspaceRouting") if isinstance(res_obj, dict) else None
            routing = _id_digest(self._salt, routing_obj.get("chatgptAccountId") if isinstance(routing_obj, dict) else None)
            if home_real is not None and a.state != "error":
                a = AuthResult(a.state, a.account_key, home_real, a.detail, a.retryable, a.env_conflicts)
            return a, routing

        before, routing_before = read_account()
        state["before"] = before
        if not usage:
            return self._result("ok" if before.state == "subscription" else "auth_error", before=before)
        if before.state != "subscription":
            outcome = "transient_error" if before.state == "error" else "auth_error"
            code = before.detail if before.state == "error" else before.state
            return self._result(outcome, before=before, code=code, version=version)
        if before.account_key is None:
            return self._result("transient_error", before=before, code=IDENTITY_UNPROVEN, version=version)

        rl = self._request(session, "account/rateLimits/read", {"excludeResetCreditDetails": True}, deadline, admit)
        received_at = session.last_received_at
        if "error" in rl:
            if _is_rate_limit_error(rl.get("error")):
                return self._result("rate_limited", before=before, code="rate_limited", version=version)
            return self._result("transient_error", before=before, code="rpc_error", version=version)

        after, routing_after = read_account()
        state["after"] = after
        if after.state != "subscription":
            outcome = "transient_error" if after.state == "error" else "auth_error"
            code = after.detail if after.state == "error" else after.state
            return self._result(outcome, before=before, after=after, code=code, version=version)
        if after.account_key is None:
            return self._result("transient_error", before=before, after=after, code=IDENTITY_UNPROVEN, version=version)
        if after.account_key != before.account_key:
            return self._result("identity_changed", before=before, after=after, code="account_changed", version=version)
        # A usage result naming an account must match the routing id of both reads.
        result_obj = rl.get("result")
        if isinstance(result_obj, dict) and result_obj.get("accountId") is not None:
            usage_digest = _id_digest(self._salt, result_obj.get("accountId"))
            if usage_digest is None or routing_before is None or routing_after is None:
                return self._result("transient_error", before=before, after=after, code=IDENTITY_UNPROVEN, version=version)
            if usage_digest != routing_before or usage_digest != routing_after:
                return self._result("identity_changed", before=before, after=after, code="usage_account_mismatch", version=version)

        outcome, windows, notes = parse_rate_limits(result_obj)
        code = None if outcome == "ok" else ("unexpected_shape" if outcome == "protocol_unsupported" else outcome)
        if outcome == "ok" and received_at is None:  # pragma: no cover - set by every successful read
            return self._result("transient_error", before=before, after=after, code="internal", version=version)
        return self._result(outcome, before=before, after=after, windows=windows, code=code, notes=notes, version=version,
                            received_at=received_at)


__all__ = [
    "PROVIDER",
    "SOURCE",
    "CODEX_AUTH_VARS",
    "ALLOWED_METHODS",
    "CLIENT_INFO",
    "IDENTITY_UNPROVEN",
    "TURN_METHOD_PREFIXES",
    "TURN_METHODS",
    "CodexCollector",
    "parse_account_read",
    "parse_rate_limits",
]
