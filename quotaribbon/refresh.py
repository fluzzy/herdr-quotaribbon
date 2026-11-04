"""Refresh orchestration: one run collects every enabled provider in its own thread.

Every write is published under ``locks/settings.lock`` after a final gate (enabled,
fingerprint, generation, server epoch, binding, plugin active, deadline); any mismatch
discards the result.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from dataclasses import dataclass, field

from . import cache
from . import constants as C
from . import procutil
from . import scheduler
from . import settings as settings_mod
from . import types as t
from .binding import make_binding, scope_key
from .fsutil import file_lock
from .log import get_logger

RUN_BUDGET_SECONDS = C.LEASE_TTL - 5
VERSION_STORE_NAME = "cli_versions.json"

_DENIED_STATES = {
    "logged_out": "logged_out",
    "api_auth": "api_auth",
    "unsupported_auth": "unsupported_auth",
    "profile_mismatch": "profile_mismatch",
}


@dataclass
class ProviderOutcome:
    """What one refresh run did for one provider; ``detail`` is a short code, never a secret."""

    provider: str
    action: str
    detail: str | None = None


@dataclass
class RefreshReport:
    outcomes: list[ProviderOutcome] = field(default_factory=list)
    manual: bool = False

    def outcome(self, provider: str) -> ProviderOutcome | None:
        for o in self.outcomes:
            if o.provider == provider:
                return o
        return None

    def summary(self) -> str:
        parts = [f"{o.provider}: {o.action}" + (f" ({o.detail})" if o.detail else "") for o in self.outcomes]
        return ("manual " if self.manual else "") + "refresh: " + ("; ".join(parts) or "nothing to do")


@dataclass(frozen=True)
class _Context:
    provider: str
    ps: t.ProviderSettings
    binding: object
    fingerprint: str
    generation: int
    server_epoch: str
    server: tuple = ("", None)  # (socket_path, identity)


class _Admission:
    def __init__(self, provider_run: "_ProviderRun") -> None:
        self._pr = provider_run

    def __call__(self) -> bool:
        return self._pr.admit()

    def critical(self):
        return self._pr.critical()


class _Discard(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RunState:
    def __init__(self, paths, host, settings, manual: bool, force: bool, now_fn, deadline: float, log,
                 settings_source: str = "file") -> None:
        self.paths = paths
        self.host = host
        self.settings = settings
        self.settings_source = settings_source
        self.manual = manual
        self.force = force
        self.now_fn = now_fn
        self.deadline = deadline
        self.log = log
        self.cancelled = threading.Event()

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def _new_connection(provider: str, binding, now: float) -> t.ConnectionRecord:
    return t.ConnectionRecord(
        schema_version=1,
        provider=provider,
        binding_key=binding.binding_key,
        profile_key=binding.profile_key,
        state="unverified",
        ever_connected=False,
        evidence=None,
        executable=None,
        cli_version=None,
        account_key=None,
        auth_epoch=0,
        verified_at=None,
        verified_server_epoch=None,
        verified_generation=None,
        next_verify_at=float(now),
        last_reason=None,
        env_conflicts=[],
    )


def _new_status(provider: str) -> t.ProviderStatus:
    return t.ProviderStatus(
        schema_version=1,
        provider=provider,
        data_state="none",
        attempt_started_at=None,
        attempt_finished_at=None,
        last_success_at=None,
        last_failure_at=None,
        consecutive_failures=0,
        next_due_at=0.0,
        retry_not_before=None,
        last_error_code=None,
        unsupported_cli_version=None,
        notes=[],
    )


def _short_code(code: object) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    clean = "".join(ch for ch in code if ch.isascii() and (ch.isalnum() or ch in "_:.-"))
    return clean[:64] or None


def _valid_notes(notes) -> list[str]:
    return [n for n in notes if t.is_valid_note(n)][:32]


class _ProviderRun:
    def __init__(self, run: _RunState, ctx: _Context) -> None:
        self.run = run
        self.ctx = ctx
        self.p = ctx.provider
        self.bk = ctx.binding.binding_key
        self.delete_snapshot_pending = False
        self.snapshot: t.Snapshot | None = None
        self.manual_retry = False

    def gate(self, *, slow: bool = True) -> str | None:
        """Reason the run may no longer act for this provider, or None.

        ``slow=False`` skips the socket round trip, for use under ``locks/settings.lock``.
        """
        run, ctx = self.run, self.ctx
        if run.cancelled.is_set():
            return "cancelled"
        if run.remaining() <= 0:
            return "deadline"
        try:
            loaded = settings_mod.load(run.paths, update_last_good=False)
            # Fail closed: never fall back to a last-good or default ON.
            if loaded.settings is None or not loaded.valid:
                return "settings_invalid"
            if run.settings_source == "file" and loaded.source != "file":
                return "settings_changed"
            ps = loaded.settings.providers[self.p]
            if not ps.enabled:
                return "disabled"
            if settings_mod.provider_fingerprint(ps) != ctx.fingerprint:
                return "settings_changed"
            if cache.read_control(run.paths)[self.p] != ctx.generation:
                return "generation_changed"
            if cache.read_server_epoch(run.paths) != ctx.server_epoch:
                return "server_epoch_changed"
            b2 = make_binding(self.p, run.host.resolve_profile(self.p, ps), run.host.herdr_config_path(),
                              namespace=run.host.credential_namespace(self.p, ps))
            if b2.binding_key != self.bk:
                return "binding_changed"
            if slow and not run.host.plugin_active():
                return "plugin_inactive"
        except Exception as exc:  # noqa: BLE001 - fail closed
            return f"gate_error:{exc.__class__.__name__}"
        return None

    def admit(self) -> bool:
        return self.gate() is None

    @contextlib.contextmanager
    def critical(self):
        """Admission held for the block under ``locks/settings.lock``.

        An OFF toggle takes the same lock, so once it completes no new request can start.
        """
        if self.gate() is not None:
            yield False
            return
        with file_lock(self.run.paths.settings_lock, blocking=True):
            yield self.gate(slow=False) is None

    def require_admission(self) -> None:
        reason = self.gate()
        if reason is not None:
            raise _Discard(reason)

    def publish(self, conn, status, snapshot=None, delete_snapshot: bool = False) -> None:
        paths = self.run.paths
        reason = self.gate()
        if reason is not None:
            raise _Discard(reason)
        with file_lock(paths.settings_lock, blocking=True):
            reason = self.gate(slow=False)
            if reason is not None:
                raise _Discard(reason)
            if delete_snapshot or self.delete_snapshot_pending:
                cache.delete_snapshot(paths, self.p, self.bk)
                self.delete_snapshot_pending = False
            cache.write_connection(paths, conn)
            if snapshot is not None:
                cache.write_snapshot(paths, snapshot, self.bk)
            cache.write_status(paths, status, self.bk)

    def timeout(self) -> float:
        remaining = self.run.remaining()
        if remaining <= 1:
            raise _Discard("deadline")
        return float(min(C.COLLECT_TIMEOUT, remaining))

    def execute(self) -> ProviderOutcome:
        paths = self.run.paths
        lock_path = paths.provider_lock(self.p, self.ctx.binding.profile_key)
        try:
            fd = cache.lock_fd(lock_path, blocking=False)
        except OSError as exc:
            return ProviderOutcome(self.p, "failed", f"lock:{exc.__class__.__name__}")
        if fd is None:
            return ProviderOutcome(self.p, "skipped_busy", None)
        try:
            procutil.register_lock_owner(fd)
            try:
                paths.ensure_binding_dir(self.p, self.bk)
                return self._locked(fd)
            finally:
                procutil.unregister_lock_owner(fd)
        finally:
            self._release(fd)

    @staticmethod
    def _release(fd: int) -> None:
        """Children never inherit the fd, so closing it releases the lock."""
        try:
            os.close(fd)
        except OSError:
            pass

    def _locked(self, lock_fd: int) -> ProviderOutcome:
        run, paths = self.run, self.run.paths
        now = run.now_fn()
        conn = cache.read_connection(paths, self.p, self.bk)
        if conn is not None and conn.profile_key != self.ctx.binding.profile_key:
            conn = None
        fresh = conn is None
        if fresh:
            conn = _new_connection(self.p, self.ctx.binding, now)
            # A snapshot without a usable connection record can never be trusted again.
            self.delete_snapshot_pending = cache.read_snapshot(paths, self.p, self.bk) is not None or self._snapshot_file_exists()
        status = cache.read_status(paths, self.p, self.bk)
        self.snapshot = None if fresh else cache.read_snapshot(paths, self.p, self.bk)
        pv = {
            "connection": None if fresh else conn.to_json(),
            "status": None if status is None else status.to_json(),
            "generation": self.ctx.generation,
        }
        reason = scheduler.provider_due(pv, self.ctx.server_epoch, now, server=self.ctx.server,
                                        manual=run.manual, force=run.force)
        if reason is None:
            return ProviderOutcome(self.p, "not_due", None)
        # Decide before recording this attempt's start: spacing is from the previous one.
        self.manual_retry = run.manual and scheduler.manual_retry_ok(pv["status"], now)
        if status is None:
            status = _new_status(self.p)
        status.attempt_started_at = float(now)
        try:
            cache.write_status(paths, status, self.bk)
        except (OSError, ValueError) as exc:
            return ProviderOutcome(self.p, "failed", f"status_write:{exc.__class__.__name__}")
        try:
            return self._attempt(conn, status, reason, lock_fd)
        except _Discard as d:
            run.log.info("%s: discarded:%s", self.p, d.reason)
            return ProviderOutcome(self.p, "discarded", d.reason)
        except Exception as exc:  # noqa: BLE001 - one provider never affects the other
            run.log.warning("%s: refresh failed: %s", self.p, exc.__class__.__name__)
            return self._unexpected_failure(exc)
        finally:
            self._mark_finished(status)

    def _unexpected_failure(self, exc: BaseException) -> ProviderOutcome:
        """Persist like a transient failure, from the on-disk records, never half-applied state."""
        code = _short_code(f"internal:{exc.__class__.__name__}")
        paths = self.run.paths
        try:
            conn = cache.read_connection(paths, self.p, self.bk)
            if conn is None or conn.profile_key != self.ctx.binding.profile_key:
                conn = _new_connection(self.p, self.ctx.binding, self.run.now_fn())
            status = cache.read_status(paths, self.p, self.bk) or _new_status(self.p)
            self._transient_failure(conn, status, self.run.now_fn(), code)
        except _Discard as d:
            return ProviderOutcome(self.p, "discarded", d.reason)
        except Exception:  # noqa: BLE001 - the state dir itself is failing
            pass
        return ProviderOutcome(self.p, "failed", code)

    def _snapshot_file_exists(self) -> bool:
        return os.path.exists(self.run.paths.snapshot(self.p, self.bk))

    def _mark_finished(self, fallback) -> None:
        try:
            st = cache.read_status(self.run.paths, self.p, self.bk) or fallback
            st.attempt_finished_at = float(max(self.run.now_fn(), st.attempt_started_at or 0.0))
            cache.write_status(self.run.paths, st, self.bk)
        except Exception:  # noqa: BLE001
            pass

    def _reset_status(self, status, code: str | None) -> None:
        status.data_state = "none"
        status.consecutive_failures = 0
        status.first_failure_at = None
        status.next_due_at = 0.0
        status.retry_not_before = None
        status.last_error_code = _short_code(code)
        status.unsupported_cli_version = None
        status.notes = []

    def _evidence(self, conn, status, now: float) -> ProviderOutcome | None:
        run = self.run
        ev_rec = conn.evidence
        if ev_rec is not None and not run.force and not scheduler.evidence_due(conn.to_json(), now):
            return None
        try:
            ev = run.host.evidence(self.p, self.ctx.binding.profile_dir, self.ctx.ps)
            ev.validate()
        except Exception:  # noqa: BLE001 - treat as Herdr being unreachable
            ev = None
        if ev is None or ev.error is not None:
            if ev_rec is not None:
                return None  # transport failure never changes a recorded connection
            conn.next_verify_at = float(now + C.VERIFY_RECHECK_SECONDS)
            conn.last_reason = "herdr_unreachable"
            self.publish(conn, status)
            return ProviderOutcome(self.p, "failed", "herdr_unreachable")
        if ev.linked and ev.profile_correspondence in ("match", "inferred"):
            source = ev.source or ("integration" if ev.integration_state in ("current", "outdated") else "agent")
            conn.evidence = {"source": source, "integration_state": ev.integration_state, "observed_at": float(now)}
            return None
        invalidate = None
        if ev.profile_correspondence == "mismatch":
            invalidate = "evidence_profile_mismatch"
        elif ev_rec is None:
            hinted = ev.linked or ev.agent_detected or ev.integration_state in ("current", "outdated")
            invalidate = "profile_unverified" if hinted and ev.profile_correspondence == "unknown" else "no_evidence"
        elif ev_rec["source"] == "integration" and ev.integration_state == "not_installed" and not ev.agent_detected:
            invalidate = "integration_removed"
        if invalidate is None:
            # Sticky evidence (agent exit, workspace switch).
            conn.evidence = dict(ev_rec, observed_at=float(now))
            return None
        was_connected = conn.ever_connected
        conn.state = "not_connected"
        conn.evidence = None
        conn.next_verify_at = float(now + C.VERIFY_RECHECK_SECONDS)
        conn.last_reason = invalidate
        if was_connected:
            self._reset_status(status, invalidate)
        status.notes = [t.make_note("profile_unverified")] if invalidate == "profile_unverified" else []
        self.publish(conn, status, delete_snapshot=True)
        return ProviderOutcome(self.p, "not_connected", invalidate)

    def _make_collector(self, executable: str, lock_fd: int):
        # The run's own settings snapshot decides the credential selection.
        return self.run.host.make_collector(self.p, executable, self.ctx.binding.profile_dir, lock_fd=lock_fd,
                                            ps=self.ctx.ps)

    def _apply_auth(self, conn, auth, ts: float) -> tuple[str, bool]:
        """``("verified", epoch_bumped)`` | ``("denied", _)`` | ``("transient", _)``.

        A subscription without a stable account key is not a verification.
        """
        if auth.state == "subscription" and auth.account_key is None:
            return "transient", False
        if auth.state == "subscription":
            new = auth.account_key
            bump = False
            if new is not None and new != conn.account_key:
                bump = True
            elif new is None and (conn.account_key is not None or conn.state != "connected"):
                bump = True  # identity unproven: never reuse across a transition
            if bump:
                conn.auth_epoch += 1
            conn.account_key = new
            conn.state = "connected"
            conn.ever_connected = True
            conn.verified_at = float(ts)
            conn.verified_server_epoch = self.ctx.server_epoch
            conn.verified_generation = self.ctx.generation
            servers = {} if bump else dict(conn.verified_servers)
            path, ident = self.ctx.server
            if ident:
                servers.pop(path, None)
                servers[path] = ident
                while len(servers) > t.MAX_VERIFIED_SERVERS:
                    servers.pop(next(iter(servers)))
            conn.verified_servers = servers
            conn.env_conflicts = list(auth.env_conflicts)
            conn.next_verify_at = float(ts)
            conn.last_reason = None
            return "verified", bump
        if auth.retryable or auth.state not in _DENIED_STATES:
            return "transient", False
        conn.state = _DENIED_STATES[auth.state]
        conn.verified_at = None
        conn.verified_server_epoch = None
        conn.verified_generation = None
        conn.verified_servers = {}
        conn.env_conflicts = list(auth.env_conflicts)
        conn.next_verify_at = float(ts + C.VERIFY_RECHECK_SECONDS)
        conn.last_reason = auth.state
        return "denied", False

    def _denied(self, conn, status) -> ProviderOutcome:
        self._reset_status(status, conn.state)
        self.publish(conn, status, delete_snapshot=True)
        return ProviderOutcome(self.p, "auth_state", conn.state)

    def _transient_failure(self, conn, status, now: float, code: str | None, *, data_state: str = "transient_error",
                           retry_not_before: float | None = None, notes=(), verification_pending: bool = False) -> ProviderOutcome:
        k = status.consecutive_failures + 1
        status.consecutive_failures = k
        if k == 1 or status.first_failure_at is None:
            status.first_failure_at = float(now)
        status.last_failure_at = float(now)
        status.data_state = data_state
        status.retry_not_before = None if retry_not_before is None else float(retry_not_before)
        status.next_due_at = scheduler.next_due_after_failure(self.run.settings, k, now, retry_not_before)
        status.last_error_code = _short_code(code)
        status.notes = _valid_notes(notes)
        if verification_pending:
            conn.next_verify_at = float(status.next_due_at)
        self.publish(conn, status)
        return ProviderOutcome(self.p, "failed", _short_code(code) or data_state)

    def _identity_changed(self, conn, status, code: str) -> ProviderOutcome:
        conn.auth_epoch += 1
        conn.account_key = None
        conn.verified_at = None
        conn.verified_server_epoch = None
        conn.verified_generation = None
        conn.verified_servers = {}
        conn.next_verify_at = float(self.run.now_fn())
        conn.last_reason = code
        self._reset_status(status, code)
        self.publish(conn, status, delete_snapshot=True)
        return ProviderOutcome(self.p, "identity_changed", code)

    def _snapshot_reusable(self, conn) -> bool:
        snap = self.snapshot
        return (
            snap is not None
            and conn.account_key is not None
            and snap.account_key == conn.account_key
            and snap.auth_epoch == conn.auth_epoch
            and snap.profile_key == conn.profile_key
        )

    def _attempt(self, conn, status, reason: str, lock_fd: int) -> ProviderOutcome:
        run = self.run
        now = run.now_fn()

        out = self._evidence(conn, status, now)
        if out is not None:
            return out

        try:
            exe = run.host.resolve_executable(self.p, self.ctx.ps)
        except Exception:  # noqa: BLE001
            exe = None
        if exe is None:
            was_connected = conn.ever_connected
            conn.state = "cli_missing"
            conn.executable = None
            conn.next_verify_at = float(now + C.VERIFY_RECHECK_SECONDS)
            conn.last_reason = "cli_missing"
            if was_connected:
                self._reset_status(status, "cli_missing")
            self.publish(conn, status, delete_snapshot=True)
            return ProviderOutcome(self.p, "not_connected", "cli_missing")
        conn.executable = exe

        collector = self._make_collector(exe, lock_fd)
        verify = run.force or conn.state != "connected" or scheduler.needs_reverify(
            conn.to_json(), self.ctx.server_epoch, self.ctx.generation,
            server_ok=scheduler.server_verified(conn.to_json(), self.ctx.server),
        )
        rnb = status.retry_not_before
        rnb_ok = rnb is None or now >= rnb

        if status.data_state == "protocol_unsupported":
            usage = False
            if rnb_ok and (run.force or now >= status.next_due_at or reason == scheduler.REASON_MANUAL_REPROBE):
                with self.critical() as ok:
                    if not ok:
                        raise _Discard(self.gate() or "admission_revoked")
                    version = collector.cli_version()
                if version is None or version != status.unsupported_cli_version or reason == scheduler.REASON_MANUAL_REPROBE:
                    usage = True
                else:
                    status.next_due_at = float(now + C.VERIFY_RECHECK_SECONDS)
        else:
            usage = rnb_ok and (
                run.force
                or now >= status.next_due_at
                or self.manual_retry
                or (verify and status.consecutive_failures == 0 and not self._snapshot_reusable(conn))
            )

        auth_via_collect = verify and self.p == "codex" and usage
        if verify and not auth_via_collect:
            self.require_admission()
            auth = collector.check_auth(self.timeout(), _Admission(self))
            if auth.detail == t.ADMISSION_REVOKED:
                raise _Discard("admission_revoked")
            kind, bumped = self._apply_auth(conn, auth, run.now_fn())
            if kind == "denied":
                return self._denied(conn, status)
            if kind == "transient":
                code = "identity_unproven" if auth.state == "subscription" else (auth.detail or "auth_check_failed")
                return self._transient_failure(conn, status, run.now_fn(), code, verification_pending=True)
            if bumped:
                self.delete_snapshot_pending = self.snapshot is not None
                self.snapshot = None
                usage = usage or (rnb_ok and status.data_state != "protocol_unsupported" and status.consecutive_failures == 0)
            if not usage:
                self.publish(conn, status)
                return ProviderOutcome(self.p, "verified", None)
            # Publish before the slower usage fetch so the tab bar shows progress.
            self.publish(conn, status)

        if not usage:
            self.publish(conn, status)
            return ProviderOutcome(self.p, "not_due", reason)

        self.require_admission()
        started = run.now_fn()
        res = collector.collect(self.timeout(), _Admission(self))
        fetched = run.now_fn()
        if res.error_code == t.ADMISSION_REVOKED:
            raise _Discard("admission_revoked")
        ab, aa = res.auth_before, res.auth_after
        if res.cli_version is not None:
            conn.cli_version = res.cli_version

        if auth_via_collect:
            if ab is None:
                return self._transient_failure(conn, status, fetched, res.error_code or "auth_unavailable",
                                               verification_pending=True)
            kind, bumped = self._apply_auth(conn, ab, started)
            if kind == "denied":
                return self._denied(conn, status)
            if kind == "transient":
                code = "identity_unproven" if ab.state == "subscription" else (res.error_code or ab.detail or "auth_check_failed")
                return self._transient_failure(conn, status, fetched, code, verification_pending=True)
            if bumped:
                self.delete_snapshot_pending = self.snapshot is not None

        outcome = res.outcome
        # Any confirmed account-key mismatch wins over every other result.
        known = [a.account_key for a in (ab, aa)
                 if a is not None and a.state == "subscription" and a.account_key is not None]
        if conn.account_key is not None and any(k != conn.account_key for k in known):
            return self._identity_changed(conn, status, "account_changed")
        if len(set(known)) > 1:
            return self._identity_changed(conn, status, "account_changed")

        if outcome == "auth_error":
            confirmed = None
            for a in (aa, ab):
                if a is not None and a.state in _DENIED_STATES and not a.retryable:
                    confirmed = a
                    break
            if confirmed is not None:
                self._apply_auth(conn, confirmed, fetched)
                return self._denied(conn, status)
            outcome = "transient_error"

        if outcome == "identity_changed":
            return self._identity_changed(conn, status, res.error_code or "identity_changed")

        if outcome == "ok":
            if ab is None or ab.state != "subscription":
                return self._transient_failure(conn, status, fetched, "auth_before_missing", notes=res.notes)
            if aa is None or aa.state != "subscription":
                if aa is not None and aa.state in _DENIED_STATES and not aa.retryable:
                    self._apply_auth(conn, aa, fetched)
                    return self._denied(conn, status)
                return self._transient_failure(conn, status, fetched, "auth_after_failed", notes=res.notes)
            if ab.account_key is None or aa.account_key != ab.account_key or ab.account_key != conn.account_key:
                return self._transient_failure(conn, status, fetched, "identity_unproven", notes=res.notes)
            if conn.state != "connected" or scheduler.needs_reverify(
                conn.to_json(), self.ctx.server_epoch, self.ctx.generation,
                server_ok=scheduler.server_verified(conn.to_json(), self.ctx.server),
            ):
                return self._transient_failure(conn, status, fetched, "not_verified", verification_pending=True)
            received = res.usage_received_at
            if received is None or not started - 1 <= received <= fetched:
                # Value age comes from arrival time; without a plausible one, publish nothing.
                return self._transient_failure(conn, status, fetched, "usage_time_missing", notes=res.notes)
            observed = received
            if res.source_observed_at is not None:
                # A cached CLI answer is never younger than its own measurement time.
                try:
                    observed = min(received, cache.parse_iso(res.source_observed_at))
                except ValueError:
                    return self._transient_failure(conn, status, fetched, "usage_time_missing", notes=res.notes)
            return self._success(conn, status, res, observed, received)

        if outcome == "rate_limited":
            return self._transient_failure(conn, status, fetched, res.error_code or "rate_limited", data_state="rate_limited",
                                           retry_not_before=res.retry_not_before, notes=res.notes)
        if outcome == "unavailable":
            status.data_state = "unavailable"
            status.last_failure_at = float(fetched)
            status.consecutive_failures = 0
            status.first_failure_at = None
            status.retry_not_before = None
            status.next_due_at = scheduler.next_due_after_success(run.settings, fetched)
            status.last_error_code = _short_code(res.error_code) or "unavailable"
            status.notes = _valid_notes(res.notes)
            self.publish(conn, status)
            return ProviderOutcome(self.p, "unavailable", status.last_error_code)
        if outcome == "protocol_unsupported":
            status.data_state = "protocol_unsupported"
            status.unsupported_cli_version = res.cli_version or conn.cli_version or ""
            status.next_due_at = float(fetched + C.VERIFY_RECHECK_SECONDS)
            status.retry_not_before = None
            status.last_error_code = _short_code(res.error_code) or "protocol_unsupported"
            status.notes = _valid_notes(res.notes)
            self.publish(conn, status)
            return ProviderOutcome(self.p, "unsupported", status.last_error_code)
        return self._transient_failure(conn, status, fetched, res.error_code or "transient_error",
                                       retry_not_before=res.retry_not_before, notes=res.notes)

    def _kept_snapshot_time(self, conn) -> float | None:
        """``fetched_at`` of the snapshot this attempt would replace, if it stays valid."""
        if self.delete_snapshot_pending or not self._snapshot_reusable(conn):
            return None
        try:
            return cache.parse_iso(self.snapshot.fetched_at)
        except (ValueError, AttributeError):
            return None

    def _success(self, conn, status, res, fetched: float, arrived: float) -> ProviderOutcome:
        """Publish usage measured at ``fetched``.

        Values not newer than the kept snapshot never replace it, so a cached answer can
        never renew a value's lifetime.
        """
        run = self.run
        if arrived - fetched >= run.settings.hide_values_after_seconds:
            return self._transient_failure(conn, status, arrived, "usage_too_old", notes=res.notes)
        kept = self._kept_snapshot_time(conn)
        status.data_state = "ok"
        status.last_success_at = float(max(fetched, kept or fetched))
        status.consecutive_failures = 0
        status.first_failure_at = None
        status.next_due_at = scheduler.next_due_after_success(run.settings, arrived)
        status.retry_not_before = None
        status.last_error_code = None
        status.unsupported_cli_version = None
        status.notes = _valid_notes(res.notes)
        windows = list(res.windows)
        if windows and kept is not None and fetched <= kept:
            self.publish(conn, status)
            return ProviderOutcome(self.p, "published", "not_newer")
        if not windows:
            self.publish(conn, status, delete_snapshot=True)
            return ProviderOutcome(self.p, "published", "no_windows")
        snap = t.Snapshot(
            schema_version=1,
            provider=self.p,
            scope_key=scope_key(self.p, self.ctx.binding.profile_dir),
            profile_key=self.ctx.binding.profile_key,
            account_key=conn.account_key,
            auth_epoch=conn.auth_epoch,
            source=res.source,
            fetched_at=t.iso_utc_precise(fetched),
            source_observed_at=res.source_observed_at,
            status="ok",
            windows=windows,
            cli_version=res.cli_version,
            server_epoch=self.ctx.server_epoch,
        )
        snap.validate()
        self.publish(conn, status, snapshot=snap)
        return ProviderOutcome(self.p, "published", None)


SIGNAL_GRACE_SECONDS = 3.0


def _install_signal_handlers(signals: dict):
    """SIGTERM/SIGINT only record the signal; the main thread acts on it outside signal
    context, so no lock is ever taken in a handler. Returns a restore callable."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {}

    def handler(signum, frame):  # noqa: ARG001
        if signals.get("signum") is not None:
            raise SystemExit(128 + signum)
        signals["signum"] = signum

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass

    def restore():
        for sig, h in previous.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass

    return restore


JOIN_SLICE_SECONDS = 0.1
WORKER_REAP_SECONDS = 15.0


def _reap_workers(threads: list, holder: list) -> bool:
    """Cancel and terminate all provider groups, then join workers; True when all are gone."""
    alive = [th for _p, th in threads if th.is_alive()]
    if not alive:
        return procutil.live_session_count() == 0
    for rs in holder:
        rs.cancelled.set()
    end = time.monotonic() + WORKER_REAP_SECONDS
    try:
        procutil.shutdown_all(SIGNAL_GRACE_SECONDS, WORKER_REAP_SECONDS / 2)
    except Exception:  # noqa: BLE001
        pass
    for th in alive:
        th.join(max(0.0, end - time.monotonic()))
    try:
        procutil.shutdown_all(0.5, max(0.0, end - time.monotonic()))
    except Exception:  # noqa: BLE001
        pass
    return not any(th.is_alive() for th in alive) and procutil.live_session_count() == 0


def run(paths, host, *, manual: bool, now_fn=time.time, lease_id: str | None = None) -> RefreshReport:
    """Refresh every enabled provider; never raises for provider failures."""
    report = RefreshReport(outcomes=[], manual=manual)
    procutil.reset_cancellation()
    log = get_logger(paths)
    holder: list = []
    threads: list = []
    keep_clock = False
    signals: dict = {"signum": None}
    restore = _install_signal_handlers(signals)
    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    try:
        try:
            paths.ensure_private_dirs()
        except (OSError, ValueError) as exc:
            report.outcomes = [ProviderOutcome(p, "failed", f"state_dir:{exc.__class__.__name__}") for p in t.PROVIDERS]
            return report
        procutil.configure_version_store(os.path.join(paths.state_dir, VERSION_STORE_NAME))
        loaded = settings_mod.load(paths, update_last_good=True)
        if loaded.settings is None or not loaded.valid:
            # Collect nothing: the invalid file may say OFF where the last good copy says ON.
            log.warning("refresh skipped: settings.json is invalid")
            report.outcomes = [ProviderOutcome(p, "skipped_invalid_settings", None) for p in t.PROVIDERS]
            return report
        s = loaded.settings
        now = now_fn()
        with file_lock(paths.settings_lock, blocking=True):
            clock_state, max_wall = cache.read_clock_state(paths)
            untrusted = clock_state == cache.CLOCK_CORRUPT or (
                clock_state == cache.CLOCK_MISSING and cache.any_snapshot_exists(paths.state_dir))
            force = untrusted or scheduler.clock_rolled_back(max_wall, now)
            if force:
                log.warning("wall clock record unusable or moved backwards; forcing re-verification")
                # Pre-rollback values have unknowable ages: drop them for good.
                if not cache.delete_all_snapshots(paths):
                    # Keep the clock untrusted (values stay hidden) and collect nothing.
                    log.warning("refresh skipped: cached values could not be invalidated")
                    keep_clock = True
                    report.outcomes = [ProviderOutcome(p, "failed", "invalidation_incomplete") for p in t.PROVIDERS]
                    return report
                epoch = cache.rotate_server_epoch_locked(paths, now)
                cache.note_wall_clock(paths, now, reset=True)
            else:
                epoch = cache.ensure_server_epoch_locked(paths, now)
                cache.note_wall_clock(paths, now)
            fps = settings_mod.settings_fingerprints(s)
            gens = cache.sync_fingerprints(paths, fps, now=now)
        rs = _RunState(paths, host, s, manual, force, now_fn, deadline, log, loaded.source)
        try:
            server = tuple(host.server_identity())
        except Exception:  # noqa: BLE001
            server = ("", None)
        holder.append(rs)
        slots: dict = {}
        for p in t.PROVIDERS:
            ps = s.providers[p]
            if not ps.enabled:
                slots[p] = ProviderOutcome(p, "skipped_disabled", None)
                continue
            try:
                b = make_binding(p, host.resolve_profile(p, ps), host.herdr_config_path(),
                                 namespace=host.credential_namespace(p, ps))
            except Exception as exc:  # noqa: BLE001
                slots[p] = ProviderOutcome(p, "failed", f"binding:{exc.__class__.__name__}")
                continue
            ctx = _Context(p, ps, b, fps[p], gens[p], epoch, server)
            pr = _ProviderRun(rs, ctx)

            def work(pr=pr, p=p):
                try:
                    slots[p] = pr.execute()
                except Exception as exc:  # noqa: BLE001
                    slots[p] = ProviderOutcome(p, "failed", exc.__class__.__name__)

            th = threading.Thread(target=work, name=f"quotaribbon-refresh-{p}", daemon=True)
            threads.append((p, th))
            th.start()
        for p, th in threads:
            end = deadline + 1.0
            while th.is_alive() and time.monotonic() < end and signals["signum"] is None:
                th.join(min(JOIN_SLICE_SECONDS, max(0.0, end - time.monotonic())))
        if signals["signum"] is not None:
            rs.cancelled.set()
            log.warning("refresh: stopped by signal %s", signals["signum"])
            raise SystemExit(128 + signals["signum"])
        for p, th in threads:
            if th.is_alive():
                slots[p] = ProviderOutcome(p, "timeout", None)
        _reap_workers(threads, holder)
        report.outcomes = [slots[p] for p in t.PROVIDERS if p in slots]
        log.info("%s", report.summary())
        return report
    finally:
        restore()
        # Unreaped children keep the lease held until it expires.
        reaped = _reap_workers(threads, holder)
        if not keep_clock:
            try:
                with file_lock(paths.settings_lock, blocking=True):
                    cache.note_wall_clock(paths, now_fn())
            except Exception:  # noqa: BLE001
                pass
        busy = scheduler.busy_backoff(report.outcomes)
        if reaped and not busy:
            scheduler.finish_dispatch(paths, lease_id, now_fn())
        elif not reaped:
            log.warning("refresh: provider work still running at exit; dispatch lease left to expire")
        elif lease_id:
            log.info("refresh: provider lock busy; dispatch lease left to expire as backoff")


__all__ = ["ProviderOutcome", "RefreshReport", "run", "RUN_BUDGET_SECONDS"]
