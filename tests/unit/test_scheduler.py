"""Scheduler: due-ness, backoff, explicit retry-after floors, manual refresh limits, dispatch lease."""

from __future__ import annotations

import json
import os
import unittest

from quotaribbon import constants as C
from quotaribbon import scheduler
from tests.helpers.core_fakes import T0, temp_paths
from tests.helpers.core_records import EPOCH, GEN, conn, pview, settings, stat, state


def pv_dict(**kw) -> dict:
    return pview(**kw).to_json()


def due(pv: dict, now: float = T0, **kw):
    return scheduler.provider_due(pv, EPOCH, now, **kw)


class BackoffTests(unittest.TestCase):
    def test_success_schedules_one_interval_later(self):
        self.assertEqual(scheduler.next_due_after_success(settings(), T0), T0 + 120)
        self.assertEqual(scheduler.next_due_after_success(settings(interval_seconds=600, stale_after_seconds=1200, hide_values_after_seconds=1300), T0), T0 + 600)

    def test_transient_failures_back_off_exponentially_up_to_the_cap(self):
        got = [scheduler.next_due_after_failure(settings(), k, T0, None) - T0 for k in range(1, 7)]
        self.assertEqual(got, [120, 240, 480, 900, 900, 900])

    def test_explicit_retry_not_before_is_a_floor(self):
        self.assertEqual(scheduler.next_due_after_failure(settings(), 1, T0, T0 + 3600), T0 + 3600)
        self.assertEqual(scheduler.next_due_after_failure(settings(), 1, T0, T0 + 5), T0 + 120)

    def test_dict_settings_are_accepted(self):
        self.assertEqual(scheduler.next_due_after_success(settings().to_json(), T0), T0 + 120)


class DueTests(unittest.TestCase):
    def test_never_verified_provider_is_due(self):
        self.assertEqual(due({"connection": None, "status": None, "generation": 0}), "never_verified")

    def test_up_to_date_provider_is_not_due_until_next_due_at(self):
        self.assertIsNone(due(pv_dict()))
        self.assertEqual(due(pv_dict(), now=T0 + 60), "usage_due")

    def test_unconnected_provider_rechecks_on_its_verify_schedule_only(self):
        pv = pv_dict(connection=conn(state="logged_out", next_verify_at=T0 + 300))
        self.assertIsNone(due(pv))
        self.assertEqual(due(pv, now=T0 + 300), "verify_recheck")

    def test_manual_refresh_may_pull_forward_reverification_but_spaced(self):
        pv = pv_dict(connection=conn(state="not_connected", next_verify_at=T0 + 300),
                     status=stat(attempt_started_at=T0 - 30, attempt_finished_at=T0 - 29))
        self.assertIsNone(due(pv, manual=True))
        pv["status"]["attempt_started_at"] = T0 - C.MANUAL_MIN_SPACING
        self.assertEqual(due(pv, manual=True), "manual_verify")

    def test_manual_refresh_never_shortens_the_usage_interval(self):
        pv = pv_dict(status=stat(next_due_at=T0 + 100, consecutive_failures=0, attempt_started_at=T0 - 600, attempt_finished_at=T0 - 599))
        self.assertIsNone(due(pv, manual=True))

    def test_manual_refresh_may_skip_the_transient_backoff_but_spaced(self):
        pv = pv_dict(status=stat(next_due_at=T0 + 100, consecutive_failures=2, attempt_started_at=T0 - 600, attempt_finished_at=T0 - 599))
        self.assertIsNone(due(pv))
        self.assertEqual(due(pv, manual=True), "manual_retry")
        pv["status"]["attempt_started_at"] = T0 - 30
        pv["status"]["attempt_finished_at"] = T0 - 29
        self.assertIsNone(due(pv, manual=True))

    def test_retry_not_before_blocks_everything_including_manual(self):
        pv = pv_dict(status=stat(data_state="rate_limited", next_due_at=T0 - 1, retry_not_before=T0 + 500,
                                 attempt_started_at=T0 - 600, attempt_finished_at=T0 - 599))
        self.assertIsNone(due(pv, manual=True))
        self.assertIsNone(due(pv, manual=True, force=True))
        pv["connection"]["evidence"]["observed_at"] = T0 + 400
        self.assertEqual(due(pv, now=T0 + 500), "usage_due")

    def test_epoch_or_generation_change_needs_immediate_reverification(self):
        self.assertEqual(scheduler.provider_due(pv_dict(), "epoch-2", T0), "reverify")
        self.assertEqual(due(pv_dict(generation=GEN + 1)), "reverify")
        self.assertEqual(due(pv_dict(), fingerprint_ok=False), "reverify")
        self.assertEqual(due(pv_dict(connection=conn(verified_server_epoch=None))), "reverify")

    def test_failed_reverification_waits_for_its_backoff(self):
        pv = pv_dict(connection=conn(next_verify_at=T0 + 240), generation=GEN + 1)
        self.assertIsNone(due(pv))
        self.assertEqual(due(pv, now=T0 + 240), "reverify")

    def test_evidence_is_rechecked_every_five_minutes(self):
        pv = pv_dict(connection=conn(evidence={"source": "agent", "integration_state": None, "observed_at": T0 - 300}))
        self.assertEqual(due(pv), "evidence_recheck")

    def test_unsupported_protocol_only_rechecks_the_version_periodically(self):
        pv = pv_dict(status=stat(data_state="protocol_unsupported", next_due_at=T0 + 300, unsupported_cli_version="1.0.0",
                                 attempt_started_at=T0 - 120, attempt_finished_at=T0 - 119))
        self.assertIsNone(due(pv))
        self.assertEqual(due(pv, manual=True), "manual_reprobe")
        pv["connection"]["evidence"]["observed_at"] = T0 + 200
        self.assertEqual(due(pv, now=T0 + 300), "version_recheck")

    def test_attempt_in_progress_is_not_due_again(self):
        pv = pv_dict(status=stat(attempt_started_at=T0 - 10, attempt_finished_at=T0 - 200, next_due_at=T0 - 5))
        self.assertIsNone(due(pv))
        self.assertEqual(due(pv, now=T0 - 10 + C.LEASE_TTL), "usage_due")

    def test_clock_rollback_makes_every_enabled_provider_due(self):
        view = state(pview("claude"), pview("codex")).to_json()
        view["max_wall_seen"] = T0 + 3600
        got = scheduler.work_due_dicts(settings(codex=False).to_json(), view, T0)
        self.assertEqual(got, {"claude": "clock_rollback", "codex": None})

    def test_disabled_provider_is_never_due_and_invalid_settings_mean_nothing(self):
        view = state(pview("claude", connection=None)).to_json()
        self.assertEqual(scheduler.work_due_dicts(settings(claude=False).to_json(), view, T0), {"claude": None})
        self.assertEqual(scheduler.work_due_dicts(None, view, T0), {})


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.paths.ensure_private_dirs()

    def test_lease_freedom_rules(self):
        free = scheduler.lease_free_dict
        self.assertTrue(free(None, T0))
        self.assertFalse(free({"id": "a", "dispatched_at": T0 - 5, "finished_at": None}, T0))
        self.assertTrue(free({"id": "a", "dispatched_at": T0 - 5, "finished_at": T0 - 1}, T0))
        self.assertTrue(free({"id": "a", "dispatched_at": T0 - C.LEASE_TTL - 1, "finished_at": None}, T0))
        self.assertTrue(free({"id": "a", "dispatched_at": T0 + 3600, "finished_at": None}, T0))

    def test_only_one_dispatch_until_finished_or_expired(self):
        first = scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        self.assertIsNotNone(first)
        self.assertIsNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + 1))
        self.assertIsNotNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + C.LEASE_TTL + 1))

    def test_finish_is_compare_and_set(self):
        lease = scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        self.assertFalse(scheduler.finish_dispatch_at(self.paths.state_dir, "someone-else", T0 + 1))
        self.assertFalse(scheduler.finish_dispatch_at(self.paths.state_dir, None, T0 + 1))
        self.assertIsNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + 2))
        self.assertTrue(scheduler.finish_dispatch_at(self.paths.state_dir, lease, T0 + 3))
        with open(self.paths.dispatch) as fh:
            self.assertEqual(json.load(fh)["finished_at"], T0 + 3)
        self.assertIsNotNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + 4))

    def test_finishing_an_old_lease_does_not_release_a_newer_one(self):
        old = scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        new = scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + C.LEASE_TTL + 1)
        self.assertFalse(scheduler.finish_dispatch_at(self.paths.state_dir, old, T0 + C.LEASE_TTL + 2))
        self.assertIsNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0 + C.LEASE_TTL + 3))
        self.assertTrue(scheduler.finish_dispatch_at(self.paths.state_dir, new, T0 + C.LEASE_TTL + 4))

    def test_busy_dispatch_lock_means_no_dispatch(self):
        from quotaribbon import cache

        fd = cache.lock_fd(self.paths.dispatch_lock, blocking=False)
        try:
            self.assertIsNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0))
        finally:
            cache.unlock_fd(fd)

    def test_dispatch_file_is_private(self):
        scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        self.assertEqual(os.stat(self.paths.dispatch).st_mode & 0o777, 0o600)


class ManualInvocationTests(unittest.TestCase):
    def view(self, finished=None, dispatched=T0 - 1):
        return state(dispatch={"id": "lease-1", "dispatched_at": dispatched, "finished_at": finished})

    def ctx(self, source="quotaribbon-tabbar", corr="lease-1"):
        return json.dumps({"invocation_source": source, "correlation_id": corr})

    def test_tabbar_dispatch_with_the_held_lease_is_automatic(self):
        self.assertFalse(scheduler.is_manual_invocation(self.ctx(), self.view(), T0))

    def test_everything_else_is_manual(self):
        cases = {
            "no_context": (None, self.view()),
            "cli": (self.ctx(source="cli", corr="cli:plugin"), self.view()),
            "wrong_id": (self.ctx(corr="other"), self.view()),
            "finished_lease": (self.ctx(), self.view(finished=T0)),
            "expired_lease": (self.ctx(), self.view(dispatched=T0 - C.LEASE_TTL - 1)),
            "no_lease": (self.ctx(), state()),
            "garbage": ("{not json", self.view()),
            "not_object": ("[1]", self.view()),
        }
        for name, (ctx, view) in cases.items():
            with self.subTest(name):
                self.assertTrue(scheduler.is_manual_invocation(ctx, view, T0))


if __name__ == "__main__":
    unittest.main()


class DispatchAlignmentTests(unittest.TestCase):
    """One refresh action serves providers whose routine work comes due close together."""

    @staticmethod
    def pv(next_due: float, evidence_at: float = T0 - 10, state: str = "connected") -> dict:
        return {"connection": {"state": state, "evidence": {"observed_at": evidence_at}},
                "status": {"data_state": "ok", "next_due_at": next_due, "retry_not_before": None}}

    def test_routine_work_waits_for_the_other_provider_due_soon(self):
        view = {"providers": {"claude": self.pv(T0), "codex": self.pv(T0 + 20)}}
        self.assertFalse(scheduler.should_dispatch({"claude": "usage_due", "codex": None}, view, T0))
        self.assertTrue(scheduler.should_dispatch({"claude": "usage_due", "codex": "usage_due"}, view, T0 + 20))

    def test_waiting_is_bounded_by_the_window(self):
        view = {"providers": {"claude": self.pv(T0 - 31), "codex": self.pv(T0 + 20)}}
        self.assertTrue(scheduler.should_dispatch({"claude": "usage_due", "codex": None}, view, T0))

    def test_nothing_due_soon_dispatches_at_once(self):
        view = {"providers": {"claude": self.pv(T0), "codex": self.pv(T0 + 300)}}
        self.assertTrue(scheduler.should_dispatch({"claude": "usage_due", "codex": None}, view, T0))

    def test_evidence_recheck_waits_for_the_same_providers_usage(self):
        view = {"providers": {"claude": self.pv(T0 + 15, evidence_at=T0 - C.VERIFY_RECHECK_SECONDS)}}
        self.assertFalse(scheduler.should_dispatch({"claude": "evidence_recheck"}, view, T0))

    def test_verification_work_is_never_delayed(self):
        view = {"providers": {"claude": self.pv(T0 + 10), "codex": self.pv(T0 + 20)}}
        for reason in ("never_verified", "reverify", "verify_recheck", "clock_rollback"):
            self.assertTrue(scheduler.should_dispatch({"claude": reason, "codex": None}, view, T0), reason)
        self.assertFalse(scheduler.should_dispatch({"claude": None, "codex": None}, view, T0))
