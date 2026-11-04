"""End to end: Claude as-of ages, collector error backoff, dispatch lease, stderr budget, locks."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import socket
import sys
import tempfile
import threading
import time
import unittest

from quotaribbon import cache, procutil, scheduler, tabbar_entry
from quotaribbon import types as t
from quotaribbon.procutil import JsonlSession, SessionLimits, SessionStderrExceeded, run_capture
from quotaribbon.providers.claude import CACHED_ONLY
from tests.helpers.core_fakes import T0, result
from tests.integration.test_claude_collector import ClaudeCollectorCase
from tests.integration.test_refresh import CLAUDE, CODEX, SEP, RefreshTestCase


def observed(res: t.CollectResult, ts: float) -> t.CollectResult:
    return dataclasses.replace(res, source_observed_at=t.iso_utc_precise(ts))


# Claude as-of time (collector against the fake CLI)


class ClaudeAsOfCollectorTests(ClaudeCollectorCase):
    def test_current_as_of_is_published_with_its_time(self):
        r = self.collector("ok").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        seen = t.parse_iso_utc(r.source_observed_at)
        self.assertLessEqual(seen, r.usage_received_at)
        self.assertLess(r.usage_received_at - seen, 10)

    def test_old_cached_answer_keeps_its_own_age(self):
        r = self.collector("ok", QR_FAKE_AS_OF_AGE="600").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        age = r.usage_received_at - t.parse_iso_utc(r.source_observed_at)
        self.assertGreaterEqual(age, 599)
        self.assertLess(age, 620)

    def test_missing_unparsable_or_future_as_of_is_cached_only(self):
        for value in ("none", "garbage", "-3600"):
            with self.subTest(as_of=value):
                r = self.collector("ok", QR_FAKE_AS_OF_AGE=value).collect(20)
                self.assertEqual((r.outcome, r.error_code), ("transient_error", CACHED_ONLY))
                self.assertEqual(r.windows, ())
                self.assertIsNone(r.source_observed_at)

    def test_small_clock_skew_is_clamped_to_the_arrival(self):
        r = self.collector("ok", QR_FAKE_AS_OF_AGE="-5").collect(20)
        self.assertEqual(r.outcome, "ok", r.error_code)
        self.assertEqual(t.parse_iso_utc(r.source_observed_at), round(r.usage_received_at, 6))


# Refresh: snapshot age, internal errors, manual retry, failing label


class SnapshotAgeTests(RefreshTestCase):
    def test_snapshot_ages_from_the_measurement_time(self):
        self.host.collect_results["claude"] = observed(result("claude"), T0 - 400)
        rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "published")
        self.assertEqual(t.parse_iso_utc(self.snapshot("claude").fetched_at), T0 - 400)
        self.assertEqual(self.line(), CLAUDE + " [stale 6m]" + SEP + CODEX)
        # Rescheduled from the arrival, not from the old measurement time.
        self.assertEqual(self.status("claude").next_due_at, T0 + 120)

    def test_values_past_the_hide_cutoff_are_a_failure(self):
        self.host.collect_results["claude"] = observed(result("claude"), T0 - 900)
        rep = self.refresh()
        self.assertEqual(rep.outcome("claude").detail, "usage_too_old")
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(self.status("claude").consecutive_failures, 1)
        self.assertEqual(self.line(), "Claude -- [failing 1m]" + SEP + CODEX)

    def test_an_answer_not_newer_than_the_snapshot_never_renews_it(self):
        self.connect_both()  # fetched at T0
        self.clock.advance(120)
        self.host.collect_results["claude"] = observed(result("claude", windows=[]), T0 - 30)
        self.host.collect_results["claude"] = observed(result("claude"), T0 - 30)
        rep = self.refresh()
        self.assertEqual((rep.outcome("claude").action, rep.outcome("claude").detail), ("published", "not_newer"))
        self.assertEqual(t.parse_iso_utc(self.snapshot("claude").fetched_at), T0)
        self.clock.advance(800)  # T0 + 920: the kept value expires on its own schedule
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code=CACHED_ONLY)
        self.refresh()
        self.assertTrue(self.line().startswith("Claude -- [failing"), self.line())

    def test_cached_only_keeps_the_old_snapshot_and_its_deadline(self):
        self.connect_both()
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code=CACHED_ONLY)
        rep = self.refresh()
        self.assertEqual((rep.outcome("claude").action, rep.outcome("claude").detail), ("failed", CACHED_ONLY))
        self.assertEqual(t.parse_iso_utc(self.snapshot("claude").fetched_at), T0)
        self.assertEqual(self.line(), CLAUDE + " [stale 2m]" + SEP + CODEX)


class InternalErrorTests(RefreshTestCase):
    def test_a_raising_collector_backs_off_instead_of_being_parked(self):
        def boom():
            raise RuntimeError("bug")

        self.host.collect_results["claude"] = boom
        rep = self.refresh()
        self.assertEqual((rep.outcome("claude").action, rep.outcome("claude").detail), ("failed", "internal:RuntimeError"))
        st = self.status("claude")
        self.assertEqual(st.data_state, "transient_error")
        self.assertIsNone(st.unsupported_cli_version)
        self.assertEqual(st.consecutive_failures, 1)
        self.assertEqual(st.next_due_at, T0 + 120)
        self.host.collect_results["claude"] = result("claude")
        self.clock.advance(120)
        self.assertEqual(self.action(self.refresh(), "claude"), "published")


class ManualRetryTests(RefreshTestCase):
    def test_manual_refresh_retries_during_backoff_but_respects_spacing(self):
        self.connect_both()
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="network")
        self.refresh()
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="network")
        self.clock.advance(120)
        self.refresh()  # second failure: backoff 240 s
        self.host.collect_results["claude"] = result("claude")
        self.clock.advance(30)
        self.assertEqual(self.action(self.refresh(), "claude"), "not_due")  # scheduled: still backing off
        self.assertEqual(self.action(self.refresh(manual=True), "claude"), "not_due")  # spaced 60 s
        self.clock.advance(31)
        self.assertEqual(self.action(self.refresh(manual=True), "claude"), "published")
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_manual_refresh_never_bypasses_an_explicit_retry_after(self):
        self.connect_both()
        self.clock.advance(120)
        self.host.collect_results["claude"] = result("claude", "rate_limited", error_code="rate_limited",
                                                     retry_not_before=T0 + 2000)
        self.refresh()
        self.host.collect_results["claude"] = result("claude")
        self.clock.advance(120)
        self.assertEqual(self.action(self.refresh(manual=True), "claude"), "not_due")


class FailingLabelFlowTests(RefreshTestCase):
    def test_hidden_values_say_how_long_collection_has_been_failing(self):
        self.connect_both()
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="network")
        self.clock.advance(1000)
        self.refresh()
        self.assertEqual(self.line(), "Claude -- [failing 16m]" + SEP + CODEX)
        self.host.collect_results["claude"] = result("claude")
        self.clock.advance(120)
        self.refresh()
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)
        self.assertIsNone(self.status("claude").first_failure_at)


# Tab bar: dispatch lease after an ambiguous invoke, tick error log


class _SilentServer:
    """Accepts, reads the request and never answers (Herdr busy after receiving it)."""

    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.received: list[bytes] = []
        self.conns: list = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            try:
                self.received.append(c.recv(65536))
            except OSError:
                pass

    def close(self) -> None:
        for c in self.conns:
            c.close()
        self.sock.close()


class DispatchLeaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        os.makedirs(os.path.join(self.state, "locks"), 0o700)

    def lease(self) -> dict:
        return cache.read_dispatch_at(self.state)

    def test_timeout_after_delivery_keeps_the_lease(self):
        path = os.path.join(self._tmp.name, "h.sock")
        server = _SilentServer(path)
        self.addCleanup(server.close)
        self.assertIsNone(tabbar_entry._dispatch(self.state, time.time(), path, "plugin.id"))
        self.assertTrue(server.received and b"plugin.action.invoke" in server.received[0])
        lease = self.lease()
        self.assertIsNone(lease["finished_at"])
        self.assertFalse(scheduler.lease_free_dict(lease, time.time()))
        ctx = json.dumps({"invocation_source": scheduler.TABBAR_INVOCATION_SOURCE, "correlation_id": lease["id"]})
        self.assertFalse(scheduler.is_manual_invocation(ctx, {"dispatch": lease}, time.time()))

    def test_no_socket_releases_the_lease(self):
        path = os.path.join(self._tmp.name, "absent.sock")
        self.assertIsNone(tabbar_entry._dispatch(self.state, time.time(), path, "plugin.id"))
        self.assertIsNotNone(self.lease()["finished_at"])

    def test_tick_error_is_logged_once_with_the_class_name_only(self):
        cfg = os.path.join(self._tmp.name, "cfg")
        os.makedirs(cfg)
        original = scheduler.work_due_dicts

        def boom(*a, **k):
            raise KeyError("secret-detail")

        scheduler.work_due_dicts = boom
        self.addCleanup(setattr, scheduler, "work_due_dicts", original)
        boot = {"config_dir": cfg, "state_dir": self.state, "plugin_id": "plugin.id"}
        for _ in range(3):
            tabbar_entry.run(boot, os.path.join(self._tmp.name, "absent.sock"), {"enabled": True},
                             env={"HOME": self._tmp.name})
        with open(os.path.join(self.state, "logs", "quotaribbon.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("tabbar tick_error (KeyError)"), 1)
        self.assertNotIn("secret-detail", text)


# procutil: stderr budget, persistent version cache


def script(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


FLOOD = "import sys\nwhile True:\n    sys.stderr.write('E' * 65536)\n"


class StderrBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = {"PATH": "/usr/bin:/bin"}

    def test_a_session_flooding_stderr_is_cut_off_quickly(self):
        start = time.monotonic()
        with self.assertRaises(SessionStderrExceeded):
            with JsonlSession(script(FLOOD), cwd=self._tmp.name, env=self.env,
                              limits=SessionLimits(timeout_s=30, grace_s=2, stderr_budget=256 << 10)) as s:
                s.read_until(lambda m: True, time.monotonic() + 30)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(procutil.live_session_count(), 0)

    def test_graceful_close_escalates_when_stderr_floods(self):
        code = "import sys\nsys.stdin.read()\n" + FLOOD  # floods only after stdin EOF
        start = time.monotonic()
        s = JsonlSession(script(code), cwd=self._tmp.name, env=self.env,
                         limits=SessionLimits(timeout_s=30, grace_s=20, stderr_budget=256 << 10))
        with self.assertRaises(SessionStderrExceeded):
            s.finish()
        self.assertLess(time.monotonic() - start, 15)

    def test_a_capture_flooding_stderr_is_an_overflow(self):
        start = time.monotonic()
        res = run_capture(script(FLOOD), cwd=self._tmp.name, env=self.env, timeout_s=30, grace_s=2,
                          stderr_budget=256 << 10)
        self.assertTrue(res.overflow)
        self.assertEqual(res.stdout, b"")
        self.assertLess(time.monotonic() - start, 10)


class VersionStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.log = os.path.join(self.root, "calls.log")
        self.exe = os.path.join(self.root, "tool")
        self.write_tool("9.8.7")
        self.store = os.path.join(self.root, "cli_versions.json")
        procutil.clear_version_cache()
        procutil.configure_version_store(self.store)
        self.addCleanup(procutil.configure_version_store, None)
        self.addCleanup(procutil.clear_version_cache)

    def write_tool(self, version: str) -> None:
        body = f"#!/bin/sh\necho x >> {shlex.quote(self.log)}\necho 'tool {version}'\n"
        with open(self.exe, "w") as fh:
            fh.write(body)
        os.chmod(self.exe, 0o755)

    def probes(self) -> int:
        try:
            with open(self.log) as fh:
                return len(fh.readlines())
        except FileNotFoundError:
            return 0

    def version(self):
        return procutil.cli_version(self.exe, cwd=self.root, env={"PATH": "/usr/bin:/bin"})

    def test_a_new_process_reuses_the_stored_version(self):
        self.assertEqual(self.version(), "9.8.7")
        self.assertEqual(self.probes(), 1)
        procutil.clear_version_cache()  # as in the next refresh process
        self.assertEqual(self.version(), "9.8.7")
        self.assertEqual(self.probes(), 1)

    def test_a_changed_binary_is_probed_again(self):
        self.assertEqual(self.version(), "9.8.7")
        procutil.clear_version_cache()
        self.write_tool("10.0.0")
        st = os.stat(self.exe)
        os.utime(self.exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.version(), "10.0.0")
        self.assertEqual(self.probes(), 2)

    def test_a_tampered_store_is_ignored(self):
        self.assertEqual(self.version(), "9.8.7")
        with open(self.store) as fh:
            data = json.load(fh)
        for e in data["entries"].values():
            e["version"] = "1.2.3; rm -rf"
        with open(self.store, "w") as fh:
            json.dump(data, fh)
        procutil.clear_version_cache()
        self.assertEqual(self.version(), "9.8.7")
        self.assertEqual(self.probes(), 2)


if __name__ == "__main__":
    unittest.main()
