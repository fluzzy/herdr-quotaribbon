"""CodexCollector end to end against the fake ``codex app-server`` (no network, no real CLI)."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
import unittest

from quotaribbon import procutil
from tests.helpers.fake_herdr import link_executable
from quotaribbon.procutil import SessionLimits
from quotaribbon.providers.codex import ALLOWED_METHODS, IDENTITY_UNPROVEN, CodexCollector
from quotaribbon.types import ADMISSION_REVOKED, CollectResult

FAKE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures", "bin", "fake_codex.py"))
CANARIES = ("canary-a@example.invalid", "canary-b@example.invalid", "acct-CANARY", "sk-CANARY")


# Constant content, so one cached executable serves every test (see cached_executable).
WRAPPER = f"#!/bin/sh\nexec {shlex.quote(os.path.realpath(sys.executable))} -S {shlex.quote(FAKE)} \"$@\"\n"


class CodexCollectorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        self.profile = os.path.join(self.home, ".codex")
        os.makedirs(self.profile)
        self.work = os.path.join(self.tmp, "state", "work", "codex")
        self.log = os.path.join(self.tmp, "fake.log")
        self.exe = link_executable(os.path.join(self.tmp, "bin"), "codex", WRAPPER)
        # The wrapper is shared, so its cached version stays valid between tests.

    def tearDown(self):
        self._tmp.cleanup()

    def collector(self, scenario: str = "ok", *, timeout: float = 10.0, grace: float = 2.0, lock_fd: int | None = None,
                  **env_extra: str) -> CodexCollector:
        env = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "QR_FAKE_SCENARIO": scenario,
            "QR_FAKE_LOG": self.log,
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_PLUGIN_ID": "plugin.id",
        }
        env.update(env_extra)
        return CodexCollector(
            executable=self.exe,
            profile_dir=self.profile,
            work_dir=self.work,
            env=env,
            salt=b"\x07" * 32,
            limits=SessionLimits(timeout_s=timeout, grace_s=grace),
            lock_fd=lock_fd,
        )

    def records(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def written(self) -> list[dict]:
        return [json.loads(r["line"]) for r in self.records() if r["mode"] == "session"]

    def methods(self) -> list[str]:
        return [m["method"] for m in self.written() if "method" in m]

    def invocations(self) -> list[list[str]]:
        return [r["argv"] for r in self.records() if r["mode"] == "invoke"]

    def assert_no_canary(self, result) -> None:
        blob = repr(result) + json.dumps(result.to_json())
        for c in CANARIES:
            self.assertNotIn(c, blob)


class CodexSuccessTests(CodexCollectorCase):
    def test_codex_bucket_windows_sorted_by_duration(self):
        t0 = time.time()
        r = self.collector("ok").collect(20)
        t1 = time.time()
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(r.source, "codex-app-server-rate-limits")
        self.assertEqual(r.cli_version, "0.166.0")
        self.assertEqual([(w.id, w.duration_seconds, w.used_percent) for w in r.windows], [("primary", 18000, 54.0), ("secondary", 604800, 21.0)])
        self.assertEqual(r.windows[1].resets_at, "2036-11-03T19:21:25Z")
        self.assertEqual(r.auth_before.state, "subscription")
        self.assertEqual(r.auth_before.account_key, r.auth_after.account_key)
        self.assertEqual(r.auth_before.config_dir, os.path.realpath(self.profile))
        self.assertTrue(t0 <= r.usage_received_at <= t1)
        CollectResult.from_json(r.to_json())
        self.assert_no_canary(r)

    def test_only_the_allowed_requests_are_written_in_order(self):
        self.collector("ok").collect(20)
        msgs = self.written()
        self.assertEqual(self.methods(), ["initialize", "initialized", "account/read", "account/rateLimits/read", "account/read"])
        self.assertTrue(set(self.methods()) <= set(ALLOWED_METHODS))
        self.assertTrue(all("jsonrpc" not in m for m in msgs))
        init = msgs[0]["params"]
        self.assertEqual((init["clientInfo"]["name"], init["clientInfo"]["version"]), ("quotaribbon", "0.1.0"))
        self.assertEqual(init["capabilities"], {"experimentalApi": False, "requestAttestation": False})
        self.assertNotIn("id", msgs[1])
        self.assertEqual(msgs[2]["params"], {"refreshToken": False})
        self.assertEqual(msgs[3]["params"], {"excludeResetCreditDetails": True})
        self.assertEqual(msgs[4]["params"], {"refreshToken": False})
        argvs = [r["argv"] for r in self.records() if r["mode"] == "invoke"]
        self.assertIn(["app-server"], argvs)
        self.assertFalse(any("--startup-sign-out" in a for a in argvs))

    def test_child_env_binds_codex_home_and_drops_herdr_identity(self):
        self.collector("ok").collect(20)
        for rec in self.records():
            if rec["mode"] == "invoke":
                self.assertEqual(rec["env"]["CODEX_HOME"], self.profile)
                self.assertIsNone(rec["env"]["HERDR_ENV"])
                self.assertIsNone(rec["env"]["HERDR_PANE_ID"])
                self.assertIsNone(rec["env"]["HERDR_PLUGIN_ID"])
                self.assertEqual(os.path.realpath(rec["cwd"]), os.path.realpath(self.work))

    def test_server_request_is_refused_without_blocking(self):
        r = self.collector("server_request").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        replies = [m for m in self.written() if "method" not in m]
        self.assertEqual(replies, [{"id": "srv-1", "error": {"code": -32601, "message": "unsupported by quotaribbon"}}])
        self.assertTrue(set(self.methods()) <= set(ALLOWED_METHODS))

    def test_check_auth_reads_the_account_only(self):
        a = self.collector("ok").check_auth(10)
        self.assertEqual(a.state, "subscription")
        self.assertEqual(self.methods(), ["initialize", "initialized", "account/read"])

    def test_usage_receipt_time_is_taken_when_the_response_arrives_not_after_shutdown(self):
        t0 = time.time()
        r = self.collector("slow_exit").collect(20)
        t1 = time.time()
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertGreaterEqual(r.usage_received_at, t0)
        self.assertLessEqual(r.usage_received_at, t1 - 0.25)

    def test_usage_without_account_id_relies_on_the_two_account_reads(self):
        r = self.collector("usage_without_account_id").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)

    def test_harmless_notification_after_the_last_response_is_accepted(self):
        r = self.collector("trailing_notification_on_eof").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)

    def test_response_with_boolean_id_is_never_matched_to_a_request(self):
        r = self.collector("bool_id_response").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)

    def test_huge_json_numbers_never_raise(self):
        r = self.collector("huge_numbers").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual([w.used_percent for w in r.windows], [None, None])
        self.assertIn("invalid_percent", r.notes)
        self.assertIn("invalid_reset", r.notes)
        CollectResult.from_json(r.to_json())

    def test_unregistered_pass_fds_are_inherited_by_every_provider_process(self):
        procutil.clear_version_cache()
        lock_fd = os.open(os.path.join(self.tmp, "codex.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            c = self.collector("ok", lock_fd=lock_fd, QR_FAKE_CHECK_FD=str(lock_fd))
            self.assertEqual(c.collect(20).outcome, "ok")
            self.assertEqual(c.check_auth(10).state, "subscription")
            procutil.clear_version_cache()
            self.assertEqual(c.cli_version(), "0.166.0")
        finally:
            os.close(lock_fd)
        recs = [rec for rec in self.records() if rec["mode"] == "invoke"]
        self.assertEqual([rec["argv"] for rec in recs], [["--version"], ["app-server"], ["app-server"], ["--version"]])
        self.assertTrue(all(rec["fd_open"] is True for rec in recs))

    def test_a_registered_lock_fd_is_withheld_from_every_provider_process(self):
        procutil.clear_version_cache()
        lock_fd = os.open(os.path.join(self.tmp, "codex.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        procutil.register_lock_owner(lock_fd)
        try:
            self.assertEqual(self.collector("ok", lock_fd=lock_fd, QR_FAKE_CHECK_FD=str(lock_fd)).collect(20).outcome, "ok")
        finally:
            procutil.unregister_lock_owner(lock_fd)
            os.close(lock_fd)
        recs = [rec for rec in self.records() if rec["mode"] == "invoke"]
        self.assertTrue(recs and all(rec["fd_open"] is False for rec in recs))


class CodexFakeCliKnobTests(CodexCollectorCase):
    def test_account_selection_is_consistent_across_every_call(self):
        ra = self.collector("ok").collect(20)
        rb = self.collector("ok", QR_FAKE_ACCOUNT="b").collect(20)
        self.assertEqual((ra.outcome, rb.outcome), ("ok", "ok"), (ra.error_code, rb.error_code))
        self.assertNotEqual(ra.auth_before.account_key, rb.auth_before.account_key)
        self.assertEqual(rb.auth_before.account_key, rb.auth_after.account_key)

    def test_usage_error_is_transient(self):
        r = self.collector("usage_error").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "rpc_error"))

    def test_usage_delay_postpones_the_rate_limits_answer(self):
        procutil.clear_version_cache()
        t0 = time.monotonic()
        r = self.collector("ok", QR_FAKE_USAGE_DELAY="0.2").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        kinds = [rec["kind"] for rec in self.records() if rec["mode"] == "invoke"]
        self.assertEqual(kinds, ["version", "app_server"])


class CodexNoDataTests(CodexCollectorCase):
    def test_model_only_buckets_give_no_windows(self):
        r = self.collector("model_only").collect(20)
        self.assertEqual((r.outcome, r.windows), ("unavailable", ()))
        self.assertIn("no_common_bucket", r.notes)

    def test_rate_limited_error_without_explicit_retry_time(self):
        r = self.collector("rate_limited").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.retry_not_before), ("rate_limited", "rate_limited", None))
        self.assert_no_canary(r)

    def test_other_rpc_error_is_transient(self):
        r = self.collector("rpc_error").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "rpc_error"))
        self.assert_no_canary(r)

    def test_malformed_response_is_protocol_unsupported(self):
        r = self.collector("malformed_json").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("protocol_unsupported", "malformed_output", ()))

    def test_malformed_output_after_the_last_response_rejects_the_result(self):
        for scenario in ("trailing_malformed_same_write", "trailing_malformed_on_eof"):
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.error_code, r.windows), ("protocol_unsupported", "malformed_output", ()), scenario)
            self.assertIsNone(r.usage_received_at)

    def test_turn_activity_rejects_the_result(self):
        for scenario in ("turn_notification", "approval_request", "trailing_turn_on_eof"):
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.error_code, r.windows), ("protocol_unsupported", "unexpected_turn", ()), scenario)
            self.assertEqual(r.notes, ("unexpected_turn",), scenario)
            # A turn request is never answered.
            self.assertEqual([m for m in self.written() if "method" not in m], [], scenario)
            self.assertEqual(procutil.live_session_count(), 0)

    def test_server_request_with_an_unsafe_id_aborts_and_is_never_echoed(self):
        for scenario in ("bad_server_id_object", "bad_server_id_huge_int", "bad_server_id_long", "bad_server_id_spaces",
                         "bad_server_id_bool"):
            if os.path.exists(self.log):
                os.remove(self.log)
            r = self.collector(scenario).collect(20)
            self.assertEqual((r.outcome, r.error_code, r.windows), ("protocol_unsupported", "malformed_output", ()), scenario)
            self.assertEqual([m for m in self.written() if "method" not in m], [], scenario)
            self.assertNotIn("account/rateLimits/read", self.methods(), scenario)

    def test_check_auth_never_reports_a_subscription_from_a_failed_session(self):
        a = self.collector("turn_notification").check_auth(10)
        self.assertEqual((a.state, a.detail, a.retryable), ("error", "unexpected_turn", True))

    def test_timeout_kills_the_server(self):
        t0 = time.monotonic()
        r = self.collector("slow", grace=0.1).collect(0.4)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "timeout"))
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(procutil.live_session_count(), 0)


class CodexAuthTests(CodexCollectorCase):
    def test_logged_out_never_requests_rate_limits(self):
        r = self.collector("logged_out").collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "logged_out"))
        self.assertNotIn("account/rateLimits/read", self.methods())

    def test_api_key_account_never_requests_rate_limits(self):
        r = self.collector("api_key").collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "api_auth"))
        self.assertNotIn("account/rateLimits/read", self.methods())

    def test_env_api_key_is_reported_by_name(self):
        r = self.collector("env_api_key", OPENAI_API_KEY="sk-CANARY-1").collect(20)
        self.assertEqual(r.auth_before.state, "logged_out")
        self.assertEqual(r.auth_before.env_conflicts, ("OPENAI_API_KEY",))
        self.assert_no_canary(r)

    def test_account_change_between_reads_is_identity_changed(self):
        r = self.collector("account_changed").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("identity_changed", "account_changed", ()))
        self.assertNotEqual(r.auth_before.account_key, r.auth_after.account_key)
        self.assert_no_canary(r)

    def test_usage_for_another_account_is_identity_changed(self):
        r = self.collector("usage_account_mismatch").collect(20)
        self.assertEqual((r.outcome, r.error_code), ("identity_changed", "usage_account_mismatch"))

    def test_usage_account_id_that_cannot_be_compared_is_unproven(self):
        r = self.collector("usage_account_id_not_a_string").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))

    def test_email_without_stable_account_id_is_unproven_and_asks_no_usage(self):
        r = self.collector("no_routing_id").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))
        self.assertEqual(r.auth_before.state, "subscription")
        self.assertIsNone(r.auth_before.account_key)
        self.assertNotIn("account/rateLimits/read", self.methods())
        self.assert_no_canary(r)

    def test_stable_account_id_missing_on_the_second_read_is_unproven(self):
        r = self.collector("routing_missing_after").collect(20)
        self.assertEqual((r.outcome, r.error_code, r.windows), ("transient_error", IDENTITY_UNPROVEN, ()))
        self.assertIsNotNone(r.auth_before.account_key)
        self.assertIsNone(r.auth_after.account_key)

    def test_server_reporting_another_codex_home_is_profile_mismatch(self):
        r = self.collector("wrong_home").collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "profile_mismatch"))
        self.assertEqual(self.methods(), ["initialize"])

    def test_conflicting_inherited_codex_home_starts_no_process(self):
        c = self.collector("ok", CODEX_HOME=os.path.join(self.tmp, "other"))
        r = c.collect(20)
        self.assertEqual((r.outcome, r.auth_before.state), ("auth_error", "profile_mismatch"))
        procutil.clear_version_cache()
        self.assertIsNone(c.cli_version())
        self.assertEqual(self.records(), [])

    def test_missing_executable_is_retryable(self):
        c = self.collector("ok")
        c.executable = os.path.join(self.tmp, "missing")
        r = c.collect(5)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", "spawn_failed"))
        self.assertTrue(r.auth_before.retryable)


class CodexAdmissionTests(CodexCollectorCase):
    def test_revoked_before_start_runs_nothing(self):
        r = self.collector("ok").collect(20, admit=lambda: False)
        self.assertEqual(r.error_code, ADMISSION_REVOKED)
        self.assertEqual(self.records(), [])

    def test_admission_is_checked_before_every_process_and_every_message(self):
        # Order: --version, app-server spawn, initialize, initialized, account/read,
        # account/rateLimits/read, account/read. Revoking at check k sends nothing more.
        sent = ["initialize", "initialized", "account/read", "account/rateLimits/read", "account/read"]
        expected = [
            ([], []),
            ([["--version"]], []),
            ([["--version"], ["app-server"]], []),
        ] + [([["--version"], ["app-server"]], sent[:i]) for i in range(1, 5)]
        for k, (procs, methods) in enumerate(expected, start=1):
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
            self.assertEqual(self.invocations(), procs, k)
            self.assertEqual(self.methods(), methods, k)
            self.assertEqual(procutil.live_session_count(), 0)
        calls = []
        r = self.collector("ok").collect(20, admit=lambda: calls.append(1) or True)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(len(calls), 7)

    def test_revoked_after_first_account_read_stops_before_rate_limits(self):
        calls = []

        def admit():
            calls.append(1)
            return len(calls) <= 5  # --version, spawn, initialize, initialized, account/read

        r = self.collector("ok").collect(20, admit=admit)
        self.assertEqual((r.outcome, r.error_code), ("transient_error", ADMISSION_REVOKED))
        self.assertEqual(self.methods(), ["initialize", "initialized", "account/read"])
        self.assertEqual(r.auth_before.state, "subscription")
        self.assertEqual(procutil.live_session_count(), 0)

    def test_reply_to_a_server_request_also_needs_admission(self):
        calls = []

        def admit():
            calls.append(1)
            return len(calls) <= 5  # revoked right when the refusal would be written

        r = self.collector("server_request").collect(20, admit=admit)
        self.assertEqual(r.error_code, ADMISSION_REVOKED)
        self.assertEqual([m for m in self.written() if "method" not in m], [])

    def test_check_auth_checks_admission_before_spawn_and_each_message(self):
        for k, methods in ((1, []), (2, []), (3, ["initialize"]), (4, ["initialize", "initialized"])):
            calls = []

            def admit(k=k):
                calls.append(1)
                return len(calls) < k

            if os.path.exists(self.log):
                os.remove(self.log)
            a = self.collector("ok").check_auth(10, admit=admit)
            self.assertEqual((a.state, a.detail, a.retryable), ("error", ADMISSION_REVOKED, True), k)
            self.assertEqual(self.methods(), methods, k)
            self.assertEqual(self.invocations(), [] if k == 1 else [["app-server"]], k)


if __name__ == "__main__":
    unittest.main()
