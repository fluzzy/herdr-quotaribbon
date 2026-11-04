"""Refresh orchestration with fake host/collectors, rendered through the real tab-bar tick."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from quotaribbon import cache, refresh, renderer, scheduler
from quotaribbon import constants as C
from quotaribbon import settings as settings_mod
from quotaribbon import types as t
from tests.helpers.core_fakes import (
    ACCT_A,
    ACCT_B,
    NO_EVIDENCE,
    PROCESS_ACTIVITY,
    T0,
    Clock,
    FakeHost,
    auth,
    evidence,
    result,
    temp_paths,
    window,
)

E = "…"
CLAUDE = "Claude 5h 32% | 7d 68%"
CODEX = "Codex 5h 54% | 7d 21%"
SEP = "  ·  "


class RefreshTestCase(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.root = os.path.dirname(self.paths.state_dir)
        self.host = FakeHost(self.root)
        self.clock = Clock()
        self.host.clock = self.clock

    def refresh(self, manual: bool = False, lease_id: str | None = None) -> refresh.RefreshReport:
        return refresh.run(self.paths, self.host, manual=manual, now_fn=self.clock, lease_id=lease_id)

    def action(self, report, provider):
        return report.outcome(provider).action

    def line(self) -> str:
        return renderer.tick(self.paths.config_dir, self.paths.state_dir, self.host.env(), self.clock.now)["line"]

    def due(self) -> dict:
        return renderer.tick(self.paths.config_dir, self.paths.state_dir, self.host.env(), self.clock.now)["due"]

    def bk(self, provider):
        from quotaribbon import binding

        return binding.binding_for(provider, settings_mod.DEFAULT_SETTINGS, self.host.env()).binding_key

    def conn(self, provider):
        return cache.read_connection(self.paths, provider, self.bk(provider))

    def status(self, provider):
        return cache.read_status(self.paths, provider, self.bk(provider))

    def snapshot(self, provider):
        return cache.read_snapshot(self.paths, provider, self.bk(provider))

    def process_calls(self, provider):
        return [c for c in self.host.calls_for(provider) if c in PROCESS_ACTIVITY]

    def connect_both(self):
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["published", "published"], rep.summary())
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)
        self.host.calls.clear()


class DisplayFlowTests(RefreshTestCase):
    def test_both_connected_and_on_show_both_providers(self):
        self.assertEqual(self.line(), "")
        self.connect_both()
        snap = self.snapshot("claude")
        self.assertEqual(snap.server_epoch, cache.read_server_epoch(self.paths))
        self.assertEqual(snap.account_key, ACCT_A)

    def test_first_fetch_shows_ellipsis_once_verified_then_numbers(self):
        seen = {}
        self.host.during_collect["claude"] = lambda: seen.setdefault("line", self.line())
        self.host.evidence_results["codex"] = NO_EVIDENCE
        self.assertEqual(self.line(), "")
        self.refresh()
        self.assertEqual(seen["line"], f"Claude {E}")
        self.assertEqual(self.line(), CLAUDE)

    def test_first_refresh_creates_the_server_epoch_and_clock(self):
        self.assertIsNone(cache.read_server_epoch(self.paths))
        self.refresh()
        self.assertIsNotNone(cache.read_server_epoch(self.paths))
        self.assertEqual(cache.read_clock(self.paths), T0)

    def test_claude_verifies_with_auth_check_and_codex_through_its_collector(self):
        self.refresh()
        self.assertEqual(self.process_calls("claude"), ["check_auth", "collect"])
        self.assertEqual(self.process_calls("codex"), ["collect"])

    def test_not_due_until_the_interval_passes(self):
        self.connect_both()
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["not_due", "not_due"])
        self.assertEqual(self.host.calls, [])
        self.clock.advance(120)
        rep = self.refresh()
        self.assertEqual(self.process_calls("claude"), ["collect"])
        self.assertEqual(self.action(rep, "claude"), "published")

    def test_lock_fd_is_handed_to_the_collector(self):
        self.refresh()
        self.assertIsInstance(self.host.last_lock_fd, int)


class OnOffTests(RefreshTestCase):
    def test_off_completing_during_the_slow_plugin_check_blocks_the_next_spawn(self):
        # OFF lands while the admission's socket round trip is in flight: the final
        # check runs under the settings lock afterwards, so nothing is started.
        self.host.evidence_results["codex"] = NO_EVIDENCE
        checks = {"n": 0}

        def off_during_second_check():
            checks["n"] += 1
            if checks["n"] == 2:
                settings_mod.set_enabled(self.paths, "claude", False)

        self.host.plugin_check_hook = off_during_second_check
        rep = self.refresh()
        self.assertEqual(self.process_calls("claude"), [])
        self.assertEqual(self.action(rep, "claude"), "discarded")
        self.assertEqual(self.line(), "")

    def test_off_waits_for_an_admitted_spawn_and_then_stops_further_requests(self):
        import threading

        self.host.evidence_results["codex"] = NO_EVIDENCE
        done = {}

        def toggle_while_admitted():
            th = threading.Thread(target=lambda: done.setdefault("r", settings_mod.set_enabled(self.paths, "claude", False)))
            th.start()
            th.join(0.3)
            done["blocked"] = th.is_alive()  # OFF waits while the admitted spawn is in progress
            done["thread"] = th

        self.host.inside_admission["claude"] = toggle_while_admitted
        rep = self.refresh()
        done["thread"].join(5)
        self.assertTrue(done["blocked"])
        self.assertTrue(done["r"].changed)
        self.assertEqual(self.action(rep, "claude"), "discarded")  # never published after OFF
        self.assertIsNone(self.snapshot("claude"))

    def test_collectors_get_the_runs_own_settings_snapshot(self):
        self.refresh()
        ps = self.host.last_ps
        self.assertIsNotNone(ps)
        self.assertEqual(ps, settings_mod.load(self.paths, update_last_good=False).settings.providers["codex"])

    def test_disabled_provider_gets_no_evidence_auth_or_usage_calls_across_restarts(self):
        settings_mod.set_enabled(self.paths, "claude", False)
        for _ in range(3):
            self.clock.advance(400)
            cache.rotate_server_epoch(self.paths, now=self.clock.now)
            rep = self.refresh()
            self.assertEqual(self.action(rep, "claude"), "skipped_disabled")
        self.assertEqual(self.host.calls_for("claude"), [])
        self.assertFalse(settings_mod.load(self.paths, update_last_good=False).settings.providers["claude"].enabled)
        self.assertEqual(self.line(), CODEX)

    def test_off_during_collection_discards_the_result_and_stops_requests(self):
        self.host.during_collect["claude"] = lambda: settings_mod.set_enabled(self.paths, "claude", False)
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "discarded")
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.line(), CODEX)
        self.host.calls.clear()
        self.clock.advance(1000)
        self.refresh()
        self.assertEqual(self.host.calls_for("claude"), [])
        status = self.status("claude")
        self.assertGreaterEqual(status.attempt_finished_at, status.attempt_started_at)

    def test_toggling_claude_during_codex_collection_keeps_the_codex_result(self):
        def toggle():
            settings_mod.set_enabled(self.paths, "claude", False)
            settings_mod.set_enabled(self.paths, "claude", True)

        self.host.during_collect["codex"] = toggle
        self.host.evidence_results["claude"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(self.action(rep, "codex"), "published")
        self.assertEqual(self.line(), CODEX)

    def test_off_then_on_reverifies_before_showing_numbers(self):
        self.connect_both()
        settings_mod.set_enabled(self.paths, "claude", False)
        settings_mod.set_enabled(self.paths, "claude", True)
        self.assertEqual(self.line(), f"Claude {E}" + SEP + CODEX)
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "verified")
        self.assertEqual(self.process_calls("claude"), ["check_auth"])
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_direct_settings_edit_bumps_generation_and_forces_reverification(self):
        self.connect_both()
        before = cache.read_control(self.paths)["claude"]
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "providers": {"claude": {"enabled": True, "binary": "/opt/x/claude"},
                                                           "codex": {"enabled": True}}}, fh)
        self.assertEqual(self.line(), f"Claude {E}" + SEP + CODEX)
        self.refresh()
        self.assertEqual(cache.read_control(self.paths)["claude"], before + 1)
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)


class EvidenceTests(RefreshTestCase):
    def test_no_herdr_evidence_starts_no_provider_process(self):
        self.host.evidence_results = {p: NO_EVIDENCE for p in t.PROVIDERS}
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["not_connected", "not_connected"])
        for p in t.PROVIDERS:
            self.assertEqual(self.host.calls_for(p), ["evidence"])
        self.assertEqual(self.line(), "")
        self.assertEqual(self.conn("claude").state, "not_connected")
        self.host.calls.clear()
        self.clock.advance(299)
        self.refresh()
        self.assertEqual(self.host.calls, [])
        self.clock.advance(1)
        self.refresh()
        self.assertEqual(self.host.calls_for("claude"), ["evidence"])

    def test_unverifiable_profile_is_reported_and_hidden(self):
        self.host.evidence_results["claude"] = evidence(correspondence="unknown")
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").detail, "profile_unverified")
        self.assertIn("profile_unverified", self.status("claude").notes)
        self.assertEqual(self.process_calls("claude"), [])

    def test_agent_exit_keeps_a_connection_established_from_an_agent(self):
        self.host.evidence_results["claude"] = evidence(source="agent", integration_state="not_installed", agent=True, correspondence="inferred")
        self.connect_both()
        self.host.evidence_results["claude"] = NO_EVIDENCE
        for _ in range(3):
            self.clock.advance(301)
            self.refresh()
            self.assertEqual(self.conn("claude").state, "connected")
            self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_removed_integration_disconnects_and_drops_the_cache(self):
        self.connect_both()
        self.host.evidence_results["codex"] = NO_EVIDENCE
        self.clock.advance(301)
        rep = self.refresh()
        self.assertEqual(rep.outcome("codex").detail, "integration_removed")
        self.assertIsNone(self.snapshot("codex"))
        self.assertEqual(self.line(), CLAUDE)

    def test_evidence_for_another_profile_disconnects(self):
        self.connect_both()
        self.host.evidence_results["claude"] = evidence(correspondence="mismatch")
        self.clock.advance(301)
        self.refresh()
        self.assertEqual(self.conn("claude").state, "not_connected")
        self.assertEqual(self.line(), CODEX)

    def test_unreachable_herdr_never_changes_a_recorded_connection(self):
        self.connect_both()
        self.host.evidence_results["claude"] = evidence(linked=False, error="herdr_unreachable")
        self.clock.advance(301)
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "published")
        self.assertEqual(self.conn("claude").state, "connected")

    def test_unreachable_herdr_without_evidence_starts_nothing_and_waits(self):
        self.host.evidence_results["claude"] = evidence(linked=False, error="herdr_unreachable")
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").detail, "herdr_unreachable")
        self.assertEqual(self.process_calls("claude"), [])
        self.assertIsNone(self.due().get("claude"))

    def test_missing_cli_hides_the_provider(self):
        self.connect_both()
        self.host.executables["codex"] = None
        self.clock.advance(120)
        rep = self.refresh()
        self.assertEqual(rep.outcome("codex").detail, "cli_missing")
        self.assertIsNone(self.snapshot("codex"))
        self.assertEqual(self.line(), CLAUDE)


class FailureTests(RefreshTestCase):
    def test_network_error_on_one_provider_leaves_the_other_untouched(self):
        self.connect_both()
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="network")
        self.clock.advance(240)
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "failed")
        self.assertEqual(self.line(), CLAUDE + " [stale 4m]" + SEP + CODEX)

    def test_null_limits_never_render_as_zero(self):
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="rate_limits_null",
                                                     notes=[t.make_note("rate_limits_null")])
        self.refresh()
        self.assertEqual(self.line(), "Claude -- [failing 1m]" + SEP + CODEX)
        self.assertEqual(self.status("claude").notes, ["rate_limits_null"])

    def test_explicit_retry_after_is_respected_even_by_manual_refresh(self):
        self.connect_both()
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", "rate_limited", retry_not_before=T0 + 2000)
        self.refresh()
        self.assertEqual(self.status("claude").next_due_at, T0 + 2000)
        self.host.calls.clear()
        for step in (300, 600, 900):
            self.clock.now = T0 + step
            self.refresh(manual=True)
            self.assertNotIn("collect", self.host.calls_for("claude"))
        self.clock.now = T0 + 2000
        self.host.collect_results["claude"] = result("claude")
        self.refresh()
        self.assertIn("collect", self.host.calls_for("claude"))

    def test_collector_crash_is_isolated_and_still_finishes_the_attempt(self):
        def boom():
            raise RuntimeError("bad")

        self.host.collect_results["claude"] = boom
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").action, "failed")
        self.assertEqual(self.action(rep, "codex"), "published")
        st = self.status("claude")
        self.assertGreaterEqual(st.attempt_finished_at, st.attempt_started_at)

    def test_unexpected_failure_marks_values_stale_and_backs_off(self):
        self.connect_both()
        self.clock.advance(200)

        def boom(*a, **k):
            raise RuntimeError("salt unreadable")

        from unittest import mock

        with mock.patch.object(self.host, "make_collector", side_effect=boom):
            rep = self.refresh()
        self.assertEqual((self.action(rep, "claude"), rep.outcome("claude").detail), ("failed", "internal:RuntimeError"))
        st = self.status("claude")
        self.assertEqual((st.data_state, st.consecutive_failures, st.last_failure_at), ("transient_error", 1, self.clock.now))
        self.assertGreater(st.next_due_at, self.clock.now)  # backs off instead of being due at once
        self.assertEqual(self.due()["claude"], None)
        self.assertIn("[stale", self.line().split(SEP)[0])

    def test_busy_provider_lock_is_skipped(self):
        self.host.evidence_results["codex"] = NO_EVIDENCE
        self.refresh()
        from quotaribbon.binding import make_binding

        pk = make_binding("claude", self.host.profiles["claude"], self.host.config_path).profile_key
        fd = cache.lock_fd(self.paths.provider_lock("claude", pk), blocking=False)
        try:
            self.clock.advance(1000)
            rep = self.refresh()
        finally:
            os.close(fd)
        self.assertEqual(self.action(rep, "claude"), "skipped_busy")


class AuthTests(RefreshTestCase):
    def test_confirmed_logout_hides_drops_cache_and_stops_usage(self):
        self.connect_both()
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", "auth_error", before=auth("logged_out"))
        rep = self.refresh()
        self.assertEqual((rep.outcome("claude").action, rep.outcome("claude").detail), ("auth_state", "logged_out"))
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.line(), CODEX)
        self.assertTrue(settings_mod.load(self.paths, update_last_good=False).settings.providers["claude"].enabled)
        self.host.calls.clear()
        self.host.auth["claude"] = auth("logged_out")
        for _ in range(3):
            self.clock.advance(300)
            self.refresh()
        self.assertEqual(self.process_calls("claude"), ["check_auth"] * 3)

    def test_api_key_login_is_hidden(self):
        self.host.auth["claude"] = auth("api_auth")
        self.refresh()
        self.assertEqual(self.conn("claude").state, "api_auth")
        self.assertEqual(self.process_calls("claude"), ["check_auth"])
        self.assertEqual(self.line(), CODEX)

    def test_retryable_auth_failure_keeps_the_connection_state(self):
        self.connect_both()
        cache.rotate_server_epoch(self.paths, now=self.clock.now)
        self.host.auth["claude"] = auth("error", retryable=True, detail="timeout")
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "failed")
        c = self.conn("claude")
        self.assertEqual(c.state, "connected")
        self.assertEqual(c.next_verify_at, T0 + 120)
        self.assertTrue(self.line().startswith(f"Claude {E}"))
        self.host.collect_results["claude"] = result("claude", "auth_error", before=auth("error", retryable=True))
        self.host.auth["claude"] = auth()
        self.clock.advance(120)
        self.refresh()
        self.assertEqual(self.conn("claude").state, "connected")

    def test_logged_out_user_logging_back_in_reappears_but_off_stays_off(self):
        self.host.auth["claude"] = auth("logged_out")
        self.refresh()
        self.assertEqual(self.line(), CODEX)
        self.host.auth["claude"] = auth()
        self.clock.advance(299)
        self.refresh()
        self.assertEqual(self.line(), CODEX)
        self.clock.advance(1)
        self.refresh()
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)
        settings_mod.set_enabled(self.paths, "claude", False)
        self.host.calls.clear()
        self.clock.advance(1000)
        self.refresh()
        self.assertEqual(self.host.calls_for("claude"), [])
        self.assertEqual(self.line(), CODEX)

    def test_proven_account_change_wins_over_an_unproven_identity_result(self):
        self.connect_both()
        self.clock.advance(200)
        # auth-before proves account B; auth-after lacks its stable id.
        self.host.collect_results["claude"] = result(
            "claude", "transient_error", before=auth(key=ACCT_B), after=auth(key=None), error_code="identity_unproven")
        rep = self.refresh()
        self.assertEqual((self.action(rep, "claude"), rep.outcome("claude").detail), ("identity_changed", "account_changed"))
        self.assertIsNone(self.snapshot("claude"))
        self.assertNotIn(CLAUDE, self.line())

    def test_account_switch_on_the_same_profile_never_shows_old_numbers(self):
        self.connect_both()
        epoch_before = self.conn("claude").auth_epoch
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", before=auth(key=ACCT_B))
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "identity_changed")
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.conn("claude").auth_epoch, epoch_before + 1)
        self.assertEqual(self.line(), f"Claude {E}" + SEP + CODEX)
        self.host.auth["claude"] = auth(key=ACCT_B)
        self.host.collect_results["claude"] = result("claude", windows=[window("five_hour", 18000, 5.0)], before=auth(key=ACCT_B))
        self.refresh()
        self.assertEqual(self.line(), "Claude 5h 5%" + SEP + CODEX)
        self.assertEqual(self.snapshot("claude").account_key, ACCT_B)

    def test_auth_after_from_another_account_is_discarded(self):
        self.host.collect_results["codex"] = result("codex", after=auth(key=ACCT_B))
        self.host.evidence_results["claude"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(self.action(rep, "codex"), "identity_changed")
        self.assertIsNone(self.snapshot("codex"))

    def test_subscription_without_a_stable_account_id_is_never_shown(self):
        self.host.auth["claude"] = auth(key=None)
        self.host.collect_results["claude"] = result("claude", before=auth(key=None))
        self.host.evidence_results["codex"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual((self.action(rep, "claude"), rep.outcome("claude").detail), ("failed", "identity_unproven"))
        self.assertEqual(self.process_calls("claude"), ["check_auth"])  # no usage request
        self.assertEqual(self.line(), "")
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.status("claude").consecutive_failures, 1)  # backs off, no hammering

    def test_usage_with_mismatched_or_missing_auth_after_key_is_not_published(self):
        self.connect_both()
        self.clock.advance(200)
        self.host.collect_results["claude"] = result("claude", after=auth(key=None))
        rep = self.refresh()
        self.assertNotEqual(self.action(rep, "claude"), "published")
        self.assertEqual(self.line(), CLAUDE + " [stale 3m]" + SEP + CODEX)


class RestartAndClockTests(RefreshTestCase):
    def test_server_restart_shows_ellipsis_until_reverified(self):
        self.connect_both()
        cache.rotate_server_epoch(self.paths, now=self.clock.now)
        self.assertEqual(self.line(), f"Claude {E}{SEP}Codex {E}")
        self.assertEqual(self.due(), {"claude": "reverify", "codex": "reverify"})
        self.clock.advance(5)
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["verified", "verified"])
        self.assertEqual(self.process_calls("claude"), ["check_auth"])
        self.assertEqual(self.process_calls("codex"), ["check_auth"])
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_restart_detected_from_the_socket_even_without_the_startup_hook(self):
        self.connect_both()
        self.host.restart_server()  # new Herdr server, startup hook never ran: same epoch
        self.assertEqual(self.line(), f"Claude {E}{SEP}Codex {E}")
        self.assertEqual(self.due(), {"claude": "reverify", "codex": "reverify"})
        self.clock.advance(5)
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["verified", "verified"])
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_sessions_sharing_a_binding_do_not_force_each_other_to_reverify(self):
        self.connect_both()
        first = self.host.socket_path
        second = os.path.join(self.root, "herdr-second.sock")
        with open(second, "w"):
            pass
        self.host.socket_path = second
        self.clock.advance(5)
        self.assertEqual(self.refresh().outcome("claude").action, "verified")
        self.host.socket_path = first
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)
        self.assertEqual(self.due(), {"claude": None, "codex": None})
        self.assertEqual(set(self.conn("claude").verified_servers), {first, second})

    def test_rollback_repair_never_revives_values_fetched_before_it(self):
        self.connect_both()  # fetched at T0
        self.clock.advance(800)
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="timeout")
        self.refresh()  # the clock record now reaches T0 + 800
        self.clock.now = T0 + 200  # the wall clock jumps back
        rep = self.refresh()  # repairs the rollback; usage still failing
        self.assertNotEqual(self.action(rep, "claude"), "published")
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.line().split(SEP)[0], "Claude -- [failing 3m]")
        self.host.collect_results["claude"] = result("claude")
        self.clock.advance(900)
        self.refresh()
        self.assertTrue(self.line().startswith(CLAUDE))

    def test_wall_clock_rollback_hides_numbers_then_forces_reverification(self):
        self.connect_both()
        epoch = cache.read_server_epoch(self.paths)
        self.clock.now = T0 - 3600
        self.assertEqual(self.line(), "Claude --" + SEP + "Codex --")
        self.assertEqual(self.due(), {"claude": "clock_rollback", "codex": "clock_rollback"})
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["published", "published"])
        self.assertNotEqual(cache.read_server_epoch(self.paths), epoch)
        self.assertEqual(cache.read_clock(self.paths), T0 - 3600)
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)


class OutcomeTableTests(RefreshTestCase):
    """What each collection outcome does to snapshot, status and connection."""

    def run_outcome(self, res):
        self.connect_both()
        self.before_conn = self.conn("claude")
        self.clock.advance(120)
        self.host.collect_results["claude"] = res
        return self.refresh().outcome("claude")

    def test_ok_replaces_the_snapshot_and_schedules_one_interval(self):
        out = self.run_outcome(result("claude", windows=[window("five_hour", 18000, 40.0)]))
        self.assertEqual(out.action, "published")
        st = self.status("claude")
        self.assertEqual((st.data_state, st.consecutive_failures, st.next_due_at), ("ok", 0, T0 + 240))
        self.assertEqual(self.snapshot("claude").windows[0].used_percent, 40.0)
        self.assertEqual(self.conn("claude").verified_generation, cache.read_control(self.paths)["claude"])

    def test_ok_without_windows_removes_the_snapshot(self):
        out = self.run_outcome(result("claude", windows=[], notes=[t.make_note("no_common_bucket")]))
        self.assertEqual((out.action, out.detail), ("published", "no_windows"))
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.line(), "Claude --" + SEP + CODEX)

    def test_transient_error_keeps_the_snapshot_and_backs_off(self):
        self.run_outcome(result("claude", "transient_error", error_code="timeout"))
        self.assertIsNotNone(self.snapshot("claude"))
        st = self.status("claude")
        self.assertEqual((st.consecutive_failures, st.next_due_at), (1, T0 + 240))
        self.assertEqual(self.conn("claude"), self.before_conn)
        self.clock.advance(120)
        self.refresh()
        self.assertEqual(self.status("claude").next_due_at, T0 + 240 + 240)

    def test_rate_limited_keeps_the_snapshot_and_records_retry_after(self):
        self.run_outcome(result("claude", "rate_limited", retry_not_before=T0 + 5000))
        st = self.status("claude")
        self.assertEqual((st.data_state, st.retry_not_before, st.next_due_at), ("rate_limited", T0 + 5000, T0 + 5000))
        self.assertIsNotNone(self.snapshot("claude"))

    def test_unavailable_keeps_the_snapshot_until_it_expires(self):
        self.run_outcome(result("claude", "unavailable", notes=[t.make_note("rate_limits_unavailable")]))
        st = self.status("claude")
        self.assertEqual((st.data_state, st.next_due_at), ("unavailable", T0 + 240))
        self.assertTrue(self.line().startswith(CLAUDE + " [stale 2m]"))
        self.clock.now = T0 + 901
        self.assertTrue(self.line().startswith("Claude --"))

    def test_unsupported_protocol_shows_dashes_and_reprobes_only_on_version_change(self):
        out = self.run_outcome(result("claude", "protocol_unsupported", cli_version="1.0.0", notes=[t.make_note("unexpected_turn")]))
        self.assertEqual(out.action, "unsupported")
        self.assertIsNotNone(self.snapshot("claude"))
        self.assertEqual(self.status("claude").unsupported_cli_version, "1.0.0")
        self.assertEqual(self.line(), "Claude --" + SEP + CODEX)
        self.host.calls.clear()
        self.clock.advance(150)
        self.refresh()
        self.assertEqual(self.process_calls("claude"), [])
        self.clock.advance(150)
        self.refresh()
        self.assertEqual(self.process_calls("claude"), ["cli_version"])
        self.host.versions["claude"] = "1.1.0"
        self.host.collect_results["claude"] = result("claude", cli_version="1.1.0")
        self.clock.advance(300)
        self.refresh()
        self.assertEqual(self.process_calls("claude"), ["cli_version", "cli_version", "collect"])
        self.assertTrue(self.line().startswith(CLAUDE))

    def test_manual_refresh_may_reprobe_an_unsupported_protocol(self):
        self.run_outcome(result("claude", "protocol_unsupported", cli_version="1.0.0"))
        self.host.calls.clear()
        self.clock.advance(60)
        self.refresh(manual=True)
        self.assertEqual(self.process_calls("claude"), ["cli_version", "collect"])

    def test_confirmed_auth_error_deletes_the_snapshot_and_resets_status(self):
        self.run_outcome(result("claude", "auth_error", before=auth("unsupported_auth")))
        self.assertIsNone(self.snapshot("claude"))
        st = self.status("claude")
        self.assertEqual((st.data_state, st.consecutive_failures, st.next_due_at), ("none", 0, 0.0))
        self.assertEqual(self.conn("claude").state, "unsupported_auth")

    def test_identity_changed_deletes_the_snapshot_and_bumps_auth_epoch(self):
        self.run_outcome(result("claude", "identity_changed", before=auth(), error_code="identity_mismatch"))
        self.assertIsNone(self.snapshot("claude"))
        c = self.conn("claude")
        self.assertEqual(c.auth_epoch, self.before_conn.auth_epoch + 1)
        self.assertIsNone(c.verified_server_epoch)
        self.assertEqual(self.status("claude").data_state, "none")


class GateTests(RefreshTestCase):
    def test_inactive_plugin_admits_no_provider_work(self):
        self.host.active = False
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["discarded", "discarded"])
        self.assertEqual(rep.outcome("claude").detail, "plugin_inactive")
        for p in t.PROVIDERS:
            self.assertEqual(self.process_calls(p), [])
        self.assertIsNone(self.conn("claude"))

    def test_plugin_disabled_mid_collection_discards(self):
        self.host.during_collect["codex"] = lambda: setattr(self.host, "active", False)
        self.host.evidence_results["claude"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual((rep.outcome("codex").action, rep.outcome("codex").detail), ("discarded", "admission_revoked"))
        self.assertIsNone(self.snapshot("codex"))

    def test_server_epoch_change_during_collection_discards(self):
        self.host.during_collect["claude"] = lambda: cache.rotate_server_epoch(self.paths, now=self.clock.now)
        self.host.evidence_results["codex"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").detail, "admission_revoked")

    def test_publish_waits_for_the_settings_lock(self):
        self.host.evidence_results["codex"] = NO_EVIDENCE
        fd = cache.lock_fd(self.paths.settings_lock, blocking=True)
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("rep", self.refresh()))
        th.start()
        time.sleep(0.3)
        self.assertTrue(th.is_alive())
        os.close(fd)
        th.join(10)
        self.assertEqual(box["rep"].outcome("claude").action, "published")

    def test_signal_handler_only_records_and_takes_no_locks(self):
        import signal
        from unittest import mock

        signals = {"signum": None}
        restore = refresh._install_signal_handlers(signals)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            boom = AssertionError("cleanup must not run in signal context")
            with mock.patch.object(refresh.procutil, "shutdown_all", side_effect=boom), \
                    mock.patch.object(refresh.procutil, "live_session_count", side_effect=boom):
                with refresh.procutil._LIVE_LOCK:  # the interrupted main thread holds the registry lock
                    handler(signal.SIGTERM, None)
            self.assertEqual(signals["signum"], signal.SIGTERM)
            with self.assertRaises(SystemExit):
                handler(signal.SIGTERM, None)  # a second signal exits at once
        finally:
            restore()

    def test_sigterm_during_collection_exits_without_publishing(self):
        import signal
        from unittest import mock

        self.host.during_collect["claude"] = lambda: os.kill(os.getpid(), signal.SIGTERM)
        self.host.collect_sleep = 0.5  # still collecting when the main thread acts on the signal
        self.host.evidence_results["codex"] = NO_EVIDENCE
        previous = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(refresh.procutil, "shutdown_all") as terminate:
            with self.assertRaises(SystemExit) as cm:
                self.refresh()
            # The provider thread may still be winding down; it must not publish.
            time.sleep(0.3)
        self.assertEqual(cm.exception.code, 128 + signal.SIGTERM)
        terminate.assert_called()
        self.assertIsNone(self.snapshot("claude"))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_run_deadline_stops_new_provider_work(self):
        from unittest import mock

        with mock.patch.object(refresh, "RUN_BUDGET_SECONDS", 0.5):
            rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["discarded", "discarded"])
        for p in t.PROVIDERS:
            self.assertEqual(self.process_calls(p), [])

    def test_binding_change_during_collection_discards(self):
        new_cfg = os.path.join(self.root, "herdr", "other.toml")
        self.host.during_collect["claude"] = lambda: setattr(self.host, "config_path", new_cfg)
        self.host.evidence_results["codex"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").action, "discarded")


class SettingsAndLeaseTests(RefreshTestCase):
    def test_refresh_adopts_valid_settings_as_last_good(self):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "providers": {"claude": {"enabled": False}, "codex": {"enabled": True}}}, fh)
        self.refresh()
        with open(self.paths.settings_last_good) as fh:
            self.assertFalse(json.load(fh)["providers"]["claude"]["enabled"])

    def test_invalid_settings_without_last_good_collect_nothing(self):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.settings, "w") as fh:
            fh.write('{"schema_version": 1}')
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["skipped_invalid_settings"] * 2)
        self.assertEqual(self.host.calls, [])

    def test_invalid_edit_collects_nothing_even_with_a_last_good_copy(self):
        settings_mod.set_enabled(self.paths, "claude", False)
        with open(self.paths.settings, "w") as fh:
            fh.write("{oops")
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["skipped_invalid_settings"] * 2)
        self.assertEqual(self.host.calls, [])

    def test_settings_broken_mid_run_stop_new_requests(self):
        settings_mod.set_enabled(self.paths, "claude", True)

        def break_file():
            with open(self.paths.settings, "w") as fh:
                fh.write("{oops")

        self.host.during_collect["claude"] = break_file
        self.host.evidence_results["codex"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "discarded")
        self.assertIsNone(self.snapshot("claude"))

    def test_settings_deleted_mid_run_never_fall_back_to_defaults(self):
        settings_mod.set_enabled(self.paths, "claude", True)
        self.host.during_collect["claude"] = lambda: os.unlink(self.paths.settings)
        self.host.evidence_results["codex"] = NO_EVIDENCE
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "discarded")

    def test_stale_read_never_overwrites_a_newer_off_in_last_good(self):
        from unittest import mock

        settings_mod.set_enabled(self.paths, "claude", True)
        real = cache.read_json_status
        state = {"raced": False}

        def read_then_toggle_off(path, *args, **kwargs):
            out = real(path, *args, **kwargs)
            if path == self.paths.settings and not state["raced"]:
                state["raced"] = True  # an OFF toggle lands right after the loader's read
                settings_mod.set_enabled(self.paths, "claude", False)
            return out

        with mock.patch.object(settings_mod._cache, "read_json_status", side_effect=read_then_toggle_off):
            loaded = settings_mod.load(self.paths, update_last_good=True)
        self.assertTrue(loaded.settings.providers["claude"].enabled)  # the loader's stale view
        with open(self.paths.settings_last_good) as fh:
            self.assertFalse(json.load(fh)["providers"]["claude"]["enabled"])

    def test_lease_is_finished_only_by_its_owner(self):
        lease = scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        self.refresh(manual=True)
        self.assertIsNone(cache.read_dispatch_at(self.paths.state_dir)["finished_at"])
        self.refresh(lease_id="not-the-lease")
        self.assertIsNone(cache.read_dispatch_at(self.paths.state_dir)["finished_at"])
        self.refresh(lease_id=lease)
        self.assertIsNotNone(cache.read_dispatch_at(self.paths.state_dir)["finished_at"])

    def test_run_budget_is_below_the_lease_ttl(self):
        self.assertLessEqual(refresh.RUN_BUDGET_SECONDS, C.LEASE_TTL - 5)


if __name__ == "__main__":
    unittest.main()
