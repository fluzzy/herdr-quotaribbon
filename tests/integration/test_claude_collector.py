"""ClaudeCollector end to end against the fake ``claude`` CLI (no network, no real CLI)."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from quotaribbon import procutil
from tests.helpers.fake_herdr import link_executable
from quotaribbon.procutil import SessionLimits
from quotaribbon.providers.claude import IDENTITY_UNPROVEN, ClaudeCollector
from quotaribbon.types import ADMISSION_REVOKED, AuthResult, CollectResult

FAKE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures", "bin", "fake_claude.py"))
CANARIES = ("canary-a@example.invalid", "canary-b@example.invalid", "org-CANARY", "Canary Org", "sk-ant-CANARY")


# Constant content, so one cached executable serves every test (see cached_executable).
WRAPPER = f"#!/bin/sh\nexec {shlex.quote(os.path.realpath(sys.executable))} -S {shlex.quote(FAKE)} \"$@\"\n"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class ClaudeCollectorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        self.profile = os.path.join(self.home, ".claude")
        os.makedirs(self.profile)
        self.work = os.path.join(self.tmp, "state", "work", "claude")
        self.log = os.path.join(self.tmp, "fake.log")
        self.pidfile = os.path.join(self.tmp, "pids")
        self.exe = link_executable(os.path.join(self.tmp, "bin"), "claude", WRAPPER)
        # The wrapper is shared, so its cached version stays valid between tests.

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, scenario: str, **extra: str) -> dict[str, str]:
        e = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "QR_FAKE_SCENARIO": scenario,
            "QR_FAKE_LOG": self.log,
            "QR_FAKE_STATE": os.path.join(self.tmp, "counter"),
            "QR_FAKE_PIDFILE": self.pidfile,
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_TAB_ID": "w1:t1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_PLUGIN_ID": "plugin.id",
            "HERDR_PLUGIN_STATE_DIR": "/state",
        }
        e.update(extra)
        return e

    def collector(self, scenario: str = "ok", *, profile: str | None = None, timeout: float = 10.0, grace: float = 2.0,
                  lock_fd: int | None = None, **env_extra: str) -> ClaudeCollector:
        return ClaudeCollector(
            executable=self.exe,
            profile_dir=profile or self.profile,
            work_dir=self.work,
            env=self.env(scenario, **env_extra),
            salt=b"\x05" * 32,
            limits=SessionLimits(timeout_s=timeout, grace_s=grace),
            lock_fd=lock_fd,
            managed_settings_dir=os.path.join(self.tmp, "managed"),
        )

    def records(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def invocations(self) -> list[list[str]]:
        return [r["argv"] for r in self.records() if r["mode"] == "invoke"]

    def stdin_lines(self) -> list[dict]:
        return [json.loads(r["line"]) for r in self.records() if r["mode"] == "session"]

    def assert_no_canary(self, result: CollectResult | AuthResult) -> None:
        blob = repr(result) + json.dumps(result.to_json())
        for c in CANARIES:
            self.assertNotIn(c, blob)


class CollectSuccessTests(ClaudeCollectorCase):
    def test_both_windows_read_and_unknown_buckets_ignored(self):
        t0 = time.time()
        r = self.collector("ok").collect(20)
        t1 = time.time()
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(r.source, "claude-control-get-usage")
        self.assertEqual(r.cli_version, "2.1.316")
        self.assertEqual([(w.id, w.duration_seconds, w.used_percent) for w in r.windows], [("five_hour", 18000, 32.0), ("seven_day", 604800, 68.0)])
        self.assertEqual(r.windows[0].resets_at, "2036-11-02T08:00:00Z")
        self.assertIn("ignored_model_buckets:2", r.notes)
        self.assertEqual(r.auth_before.state, "subscription")
        self.assertEqual(r.auth_after.state, "subscription")
        self.assertEqual(r.auth_before.account_key, r.auth_after.account_key)
        self.assertIsNotNone(r.auth_before.account_key)
        self.assertIsNone(r.retry_not_before)
        self.assertIsNotNone(r.usage_received_at)
        self.assertTrue(t0 <= r.usage_received_at <= t1)
        CollectResult.from_json(r.to_json())
        self.assert_no_canary(r)

    def test_only_the_two_control_requests_are_ever_written_to_stdin(self):
        self.collector("ok").collect(20)
        lines = self.stdin_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["type"], "control_request")
        self.assertEqual(lines[0]["request"], {"subtype": "initialize"})
        self.assertEqual(lines[1]["type"], "control_request")
        self.assertEqual(lines[1]["request"], {"subtype": "get_usage", "skip_behaviors": True})
        self.assertNotEqual(lines[0]["request_id"], lines[1]["request_id"])
        session = [a for a in self.invocations() if "-p" in a]
        self.assertEqual(session, [["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--safe-mode", "--no-session-persistence"]])
        # auth before, then a fresh auth after once the session is over
        auth_runs = [i for i, a in enumerate(self.invocations()) if a[:2] == ["auth", "status"]]
        session_run = next(i for i, a in enumerate(self.invocations()) if "-p" in a)
        self.assertEqual(len(auth_runs), 2)
        self.assertLess(auth_runs[0], session_run)
        self.assertGreater(auth_runs[1], session_run)

    def test_child_env_drops_herdr_identity_and_runs_in_private_work_dir(self):
        self.collector("ok").collect(20)
        for r in self.records():
            if r["mode"] != "invoke":
                continue
            for var in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_PLUGIN_ID"):
                self.assertIsNone(r["env"][var], var)
            self.assertEqual(os.path.realpath(r["cwd"]), os.path.realpath(self.work))
            # Default profile: CLAUDE_CONFIG_DIR stays unset, as the user's own sessions have it.
            self.assertIsNone(r["env"]["CLAUDE_CONFIG_DIR"])
        self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), 0o700)

    def test_non_default_profile_is_exported_to_the_child(self):
        alt = os.path.join(self.tmp, "alt-profile")
        os.mkdir(alt)
        r = self.collector("ok", profile=alt).collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        envs = [rec["env"]["CLAUDE_CONFIG_DIR"] for rec in self.records() if rec["mode"] == "invoke"]
        self.assertTrue(envs and all(v == alt for v in envs))

    def test_inherited_matching_profile_value_is_passed_through_verbatim(self):
        alt = os.path.join(self.tmp, "alt-profile")
        os.mkdir(alt)
        r = self.collector("ok", profile=alt, CLAUDE_CONFIG_DIR=alt + "/").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        envs = {rec["env"]["CLAUDE_CONFIG_DIR"] for rec in self.records() if rec["mode"] == "invoke"}
        self.assertEqual(envs, {alt + "/"})

    def test_cli_control_requests_are_ignored_and_never_answered(self):
        r = self.collector("cli_control_request").collect(20)
        self.assertEqual(r.outcome, "ok")
        self.assertEqual(len(self.stdin_lines()), 2)

    def test_escaped_control_characters_are_harmless(self):
        r = self.collector("escaped_control_chars").collect(20)
        self.assertEqual(r.outcome, "ok")
        self.assert_no_canary(r)
        self.assertNotIn("\x1b", repr(r))

    def test_usage_receipt_time_is_taken_when_the_response_arrives_not_after_shutdown(self):
        t0 = time.time()
        r = self.collector("slow_exit").collect(20)
        t1 = time.time()
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertGreaterEqual(r.usage_received_at, t0)
        # The session lingers 0.3 s after stdin EOF; the receipt time must not include it.
        self.assertLessEqual(r.usage_received_at, t1 - 0.25)

    def test_receipt_time_precedes_a_slow_auth_after(self):
        r = self.collector("auth_after_slow").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertLessEqual(r.usage_received_at, time.time() - 0.15)

    def test_harmless_trailing_output_after_the_usage_response_is_accepted(self):
        r = self.collector("trailing_system_on_eof").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)

    def test_window_not_provided_is_omitted(self):
        r = self.collector("five_hour_null").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual([(w.id, w.used_percent) for w in r.windows], [("seven_day", 79.0)])
        self.assertIn("window_not_provided:five_hour", r.notes)

    def test_no_window_provided_gives_no_windows(self):
        r = self.collector("no_windows").collect(20)
        self.assertEqual((r.outcome, r.windows), ("ok", ()), r.error_code)
        self.assertIn("window_not_provided:five_hour", r.notes)
        self.assertIn("window_not_provided:seven_day", r.notes)

    def test_huge_json_numbers_never_raise(self):
        r = self.collector("huge_numbers").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual([w.used_percent for w in r.windows], [None, None])
        self.assertIn("invalid_percent", r.notes)
        self.assertIn("invalid_reset", r.notes)
        CollectResult.from_json(r.to_json())

    def test_unregistered_pass_fds_are_inherited_by_every_provider_process(self):
        procutil.clear_version_cache()
        lock_fd = os.open(os.path.join(self.tmp, "claude.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            c = self.collector("ok", lock_fd=lock_fd, QR_FAKE_CHECK_FD=str(lock_fd))
            r = c.collect(20)
            self.assertEqual(r.outcome, "ok", r.error_code)
            procutil.clear_version_cache()
            self.assertEqual(c.cli_version(), "2.1.316")
        finally:
            os.close(lock_fd)
        recs = [rec for rec in self.records() if rec["mode"] == "invoke"]
        kinds = [rec["argv"][:2] for rec in recs]
        self.assertEqual(kinds.count(["--version"]), 2)
        self.assertEqual(kinds.count(["auth", "status"]), 2)
        self.assertTrue(any("-p" in rec["argv"] for rec in recs))
        self.assertTrue(all(rec["fd_open"] is True for rec in recs), [(rec["argv"][:2], rec["fd_open"]) for rec in recs])

    def test_a_registered_lock_fd_is_withheld_from_every_provider_process(self):
        procutil.clear_version_cache()
        lock_fd = os.open(os.path.join(self.tmp, "claude.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        procutil.register_lock_owner(lock_fd)
        try:
            r = self.collector("ok", lock_fd=lock_fd, QR_FAKE_CHECK_FD=str(lock_fd)).collect(20)
            self.assertEqual(r.outcome, "ok", r.error_code)
        finally:
            procutil.unregister_lock_owner(lock_fd)
            os.close(lock_fd)
        recs = [rec for rec in self.records() if rec["mode"] == "invoke"]
        self.assertTrue(recs and all(rec["fd_open"] is False for rec in recs))

    def test_without_a_lock_fd_nothing_extra_is_inherited(self):
        lock_fd = os.open(os.path.join(self.tmp, "claude.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.collector("ok", QR_FAKE_CHECK_FD=str(lock_fd)).collect(20)
        finally:
            os.close(lock_fd)
        recs = [rec for rec in self.records() if rec["mode"] == "invoke"]
        self.assertTrue(recs and all(rec["fd_open"] is False for rec in recs))


class FakeCliKnobTests(ClaudeCollectorCase):
    def test_account_selection_is_consistent_across_every_invocation(self):
        ra = self.collector("ok").collect(20)
        rb = self.collector("ok", QR_FAKE_ACCOUNT="b").collect(20)
        self.assertEqual((ra.outcome, rb.outcome), ("ok", "ok"), (ra.error_code, rb.error_code))
        self.assertNotEqual(ra.auth_before.account_key, rb.auth_before.account_key)
        self.assertEqual(rb.auth_before.account_key, rb.auth_after.account_key)
        # An account switch scenario on account b switches to a different account.
        os.remove(os.path.join(self.tmp, "counter"))
        r = self.collector("switch_after_session", QR_FAKE_ACCOUNT="b").collect(20)
        self.assertEqual(r.error_code, "account_changed")
        self.assertEqual(r.auth_after.account_key, ra.auth_before.account_key)

    def test_usage_delay_postpones_only_the_usage_answer(self):
        t0 = time.monotonic()
        r = self.collector("ok", QR_FAKE_USAGE_DELAY="0.2").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)

    def test_every_process_start_is_logged_with_its_kind(self):
        procutil.clear_version_cache()
        self.collector("ok").collect(20)
        kinds = [r["kind"] for r in self.records() if r["mode"] == "invoke"]
        self.assertEqual(kinds, ["version", "auth_status", "session", "auth_status"])


class CollectFailureTests(ClaudeCollectorCase):
    def test_rate_limits_unavailable(self):
        r = self.collector("unavailable").collect(20)
        self.assertEqual((r.outcome, r.windows), ("unavailable", ()))
        self.assertIn("rate_limits_unavailable", r.notes)

    def test_malformed_json_is_never_turned_into_values(self):
        for scenario in ("malformed_json", "nan_literal", "raw_control_chars"):
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.windows), ("protocol_unsupported", ()), scenario)
            self.assertEqual(r.error_code, "malformed_output", scenario)
            self.assert_no_canary(r)

    def test_huge_line_exceeds_the_response_budget(self):
        r = self.collector("huge_line").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("protocol_unsupported", "response_too_large"))

    def test_model_turn_aborts_the_session(self):
        r = self.collector("assistant_turn").collect(20)
        self.assertEqual(r.outcome, "protocol_unsupported")
        self.assertEqual(r.notes, ("unexpected_turn",))
        self.assertEqual(r.windows, ())

    def test_malformed_output_after_the_usage_response_rejects_the_result(self):
        for scenario in ("trailing_malformed_same_write", "trailing_malformed_on_eof"):
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.error_code, r.windows), ("protocol_unsupported", "malformed_output", ()), scenario)
            self.assertIsNone(r.usage_received_at, scenario)
            self.assertEqual(procutil.live_session_count(), 0)

    def test_model_turn_after_the_usage_response_rejects_the_result(self):
        for scenario in ("trailing_turn_same_write", "trailing_turn_on_eof"):
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.windows), ("protocol_unsupported", ()), scenario)
            self.assertEqual(r.notes, ("unexpected_turn",), scenario)
            self.assertIsNone(r.usage_received_at, scenario)

    def test_usage_error_response_is_transient_and_not_leaked(self):
        r = self.collector("usage_error").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "control_error"))
        self.assert_no_canary(r)

    def test_child_closing_stdin_early_is_transient(self):
        r = self.collector("stdin_closed_early").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "session_exited"))

    def test_timeout_kills_child_and_grandchild_without_orphans(self):
        t0 = time.monotonic()
        r = self.collector("slow", grace=0.1).collect(0.5)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "timeout"))
        self.assertLess(time.monotonic() - t0, 10)
        with open(self.pidfile) as fh:
            child, grandchild = map(int, fh.read().split())
        self.assertTrue(wait_gone(child), "session child survived")
        self.assertTrue(wait_gone(grandchild), "grandchild orphaned")
        self.assertEqual(procutil.live_session_count(), 0)

    def test_timeout_escalates_to_sigkill_when_sigterm_is_ignored(self):
        r = self.collector("slow_ignore_term", grace=0.1).collect(0.5)
        self.assertEqual(r.error_code, "timeout")
        with open(self.pidfile) as fh:
            child, grandchild = map(int, fh.read().split())
        self.assertTrue(wait_gone(child))
        self.assertTrue(wait_gone(grandchild))


class AuthTests(ClaudeCollectorCase):
    def test_logged_out_starts_no_usage_session(self):
        r = self.collector("logged_out").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("auth_error", "logged_out"))
        self.assertEqual(r.auth_before.state, "logged_out")
        self.assertFalse(any("-p" in a for a in self.invocations()))
        self.assertEqual(self.stdin_lines(), [])

    def test_api_key_auth_is_reported_with_variable_name_only(self):
        r = self.collector("api_key", ANTHROPIC_API_KEY="sk-ant-CANARY-0000").collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "api_auth"))
        self.assertEqual(r.auth_before.env_conflicts, ("ANTHROPIC_API_KEY",))
        self.assertFalse(any("-p" in a for a in self.invocations()))
        self.assert_no_canary(r)
        # The variable is never stripped from the child env.
        envs = [rec["env"]["ANTHROPIC_API_KEY"] for rec in self.records() if rec["mode"] == "invoke"]
        self.assertTrue(envs and all(v == "sk-ant-CANARY-0000" for v in envs))

    def test_conflicting_inherited_profile_starts_no_process(self):
        c = self.collector("ok", CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "elsewhere"))
        a = c.check_auth(10)
        self.assertEqual((a.state, a.retryable), ("profile_mismatch", False))
        r = c.collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "profile_mismatch"))
        procutil.clear_version_cache()
        self.assertIsNone(c.cli_version())
        self.assertEqual(self.invocations(), [])  # not even --version against the other profile

    def test_account_switch_between_auth_and_session_is_identity_changed(self):
        r = self.collector("identity_changed").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("identity_changed", "identity_gate_failed"))
        self.assertEqual(r.windows, ())
        self.assert_no_canary(r)

    def test_logged_out_session_identity_fails_closed(self):
        r = self.collector("init_token_none").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("identity_changed", "identity_gate_failed"))

    def test_api_key_session_identity_fails_closed(self):
        r = self.collector("init_api_key_source").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("identity_changed", "identity_gate_failed", ()))

    def test_auth_after_is_a_real_second_check_never_a_copy(self):
        r = self.collector("ok").collect(20)
        self.assertEqual(r.outcome, "ok")
        self.assertIsNot(r.auth_after, r.auth_before)
        self.assertNotEqual(r.auth_after.detail, "identity:initialize")
        self.assertEqual(r.auth_after.state, "subscription")

    def test_account_switch_after_the_session_is_identity_changed(self):
        r = self.collector("switch_after_session").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("identity_changed", "account_changed", ()))
        self.assertNotEqual(r.auth_before.account_key, r.auth_after.account_key)
        self.assertIsNone(r.usage_received_at)
        self.assert_no_canary(r)

    def test_same_email_and_org_name_but_another_org_id_is_identity_changed(self):
        # The initialize gate (email + org name) passes; only the stable key tells them apart.
        r = self.collector("same_org_name_other_org_id").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("identity_changed", "account_changed", ()))

    def test_missing_stable_key_after_the_session_is_unproven_not_published(self):
        r = self.collector("auth_after_no_org_id").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))
        self.assertIsNotNone(r.auth_before.account_key)
        self.assertIsNone(r.auth_after.account_key)

    def test_missing_stable_key_before_the_session_is_unproven_not_published(self):
        r = self.collector("auth_before_no_org_id").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))
        self.assertIsNone(r.auth_before.account_key)

    def test_initialize_without_identity_is_unproven(self):
        r = self.collector("init_no_email").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))

    def test_logout_detected_by_auth_after_is_auth_error(self):
        r = self.collector("auth_after_logged_out").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("auth_error", "logged_out", ()))
        self.assertEqual(r.auth_after.state, "logged_out")
        self.assertFalse(r.auth_after.retryable)

    def test_initialize_unsupported_falls_back_to_auth_after(self):
        r = self.collector("init_unsupported_ok").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(sum(1 for a in self.invocations() if a[:2] == ["auth", "status"]), 2)
        self.assertEqual(r.auth_before.account_key, r.auth_after.account_key)

    def test_initialize_unsupported_and_account_switch_is_identity_changed(self):
        r = self.collector("init_unsupported_switch").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("identity_changed", "account_changed"))
        self.assertNotEqual(r.auth_before.account_key, r.auth_after.account_key)
        self.assert_no_canary(r)

    def test_auth_status_errors_are_retryable(self):
        a = self.collector("auth_garbage").check_auth(10)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", "auth_status_unparsable", True))
        self.assert_no_canary(a)
        t0 = time.monotonic()
        a = self.collector("auth_slow", grace=0.1).check_auth(0.3)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", "timeout", True))
        self.assertLess(time.monotonic() - t0, 8)
        r = self.collector("auth_slow", grace=0.1).collect(0.3)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "timeout"))

    def test_missing_executable_is_retryable(self):
        c = self.collector("ok")
        c.executable = os.path.join(self.tmp, "missing-claude")
        a = c.check_auth(5)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", "spawn_failed", True))
        self.assertIsNone(c.cli_version())


class AdmissionAndPolicyTests(ClaudeCollectorCase):
    def test_revoked_admission_before_start_runs_nothing(self):
        r = self.collector("ok").collect(20, admit=lambda: False)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", ADMISSION_REVOKED))
        self.assertEqual(self.invocations(), [])
        a = self.collector("ok").check_auth(5, admit=lambda: False)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", ADMISSION_REVOKED, True))

    def test_admission_is_checked_before_each_process_and_request(self):
        # Admission order: --version, auth before, session spawn, initialize,
        # get_usage, auth after. Revoking at check k starts / writes nothing after it.
        expected = [
            # (processes started, stdin lines written, auth status runs)
            ([], [], 0),
            ([["--version"]], [], 0),
            ([["--version"], ["auth", "status"]], [], 1),
            ([["--version"], ["auth", "status"], ["-p"]], [], 1),
            ([["--version"], ["auth", "status"], ["-p"]], ["initialize"], 1),
            ([["--version"], ["auth", "status"], ["-p"]], ["initialize", "get_usage"], 1),
        ]
        for k, (procs, lines, auth_runs) in enumerate(expected, start=1):
            calls = []

            def admit(k=k):
                calls.append(1)
                return len(calls) < k

            procutil.clear_version_cache()
            if os.path.exists(self.log):
                os.remove(self.log)
            r = self.collector("ok").collect(20, admit=admit)
            self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", ADMISSION_REVOKED, ()), k)
            self.assertEqual(len(calls), k)
            self.assertEqual([a[:2] if a[:1] != ["-p"] else ["-p"] for a in self.invocations()], procs, k)
            self.assertEqual([l["request"]["subtype"] for l in self.stdin_lines()], lines, k)
            self.assertEqual(sum(1 for a in self.invocations() if a[:2] == ["auth", "status"]), auth_runs, k)
            self.assertEqual(procutil.live_session_count(), 0)

        # Never revoked: exactly six checks.
        calls = []
        r = self.collector("ok").collect(20, admit=lambda: calls.append(1) or True)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(len(calls), 6)

    def test_check_auth_checks_admission_right_before_its_process(self):
        a = self.collector("ok").check_auth(5, admit=lambda: False)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", ADMISSION_REVOKED, True))
        self.assertEqual(self.invocations(), [])

    def test_admission_callback_that_raises_means_stop(self):
        def boom():
            raise RuntimeError("socket gone")

        r = self.collector("ok").collect(20, admit=boom)
        self.assertEqual(r.error_code, ADMISSION_REVOKED)

    def test_managed_policy_blocks_collection_without_starting_claude(self):
        managed = os.path.join(self.tmp, "managed")
        os.mkdir(managed)
        with open(os.path.join(managed, "managed-settings.json"), "w") as fh:
            fh.write('{"hooks": {"SessionStart": []}}')
        r = self.collector("ok").collect(20)
        self.assertEqual(r.outcome, "protocol_unsupported")
        self.assertEqual(r.notes, ("managed_policy_present",))
        self.assertFalse(any(a[:2] == ["auth", "status"] or "-p" in a for a in self.invocations()))


class ParentKilledTests(ClaudeCollectorCase):
    def test_killed_refresh_keeps_provider_lock_until_cli_exits_on_stdin_eof(self):
        import fcntl
        import textwrap

        lock_path = os.path.join(self.tmp, "claude.lock")
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        parent = textwrap.dedent(
            f"""
            import fcntl, json, os, sys
            sys.path.insert(0, {repo!r})
            from quotaribbon.procutil import SessionLimits
            from quotaribbon.providers.claude import IDENTITY_UNPROVEN, ClaudeCollector
            fd = os.open({lock_path!r}, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = json.loads(sys.argv[1])
            c = ClaudeCollector(executable={self.exe!r}, profile_dir={self.profile!r}, work_dir={self.work!r},
                                env=env, salt=b"x" * 32, limits=SessionLimits(timeout_s=60, grace_s=30),
                                lock_fd=fd, managed_settings_dir={os.path.join(self.tmp, "managed")!r})
            c.collect(60)
            """
        )
        p = subprocess.Popen([sys.executable, "-c", parent, json.dumps(self.env("hold_until_eof"))])
        try:
            end = time.monotonic() + 15
            while not (os.path.exists(self.pidfile) and os.path.getsize(self.pidfile)):
                self.assertLess(time.monotonic(), end, "session never started")
                time.sleep(0.02)
            with open(self.pidfile) as fh:
                child = int(fh.read().split()[0])
            p.kill()  # SIGKILL the refresh process mid-collection
            p.wait()
            fd = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a second refresh cannot start
                self.assertTrue(wait_gone(child), "CLI must exit on stdin EOF")
                end = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        self.assertLess(time.monotonic(), end, "lock never released")
                        time.sleep(0.05)
            finally:
                os.close(fd)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


if __name__ == "__main__":
    unittest.main()
