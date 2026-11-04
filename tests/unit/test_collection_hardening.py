"""Hardening of collection, state, scheduling and rendering: as-of time, retries, labels, reads."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from quotaribbon import cache, renderer, scheduler
from quotaribbon import types as t
from quotaribbon.providers.claude import usage_as_of
from tests.helpers.core_fakes import T0
from tests.helpers.core_records import EPOCH, pview, settings, stat, state

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "claude")


def usage_payload() -> dict:
    with open(os.path.join(FIXTURES, "get-usage.subscription.stream.json"), encoding="utf-8") as fh:
        msgs = json.load(fh)
    for m in msgs:
        if m.get("type") == "control_response":
            return m["response"]["response"]
    raise AssertionError("no usage response in fixture")


class UsageAsOfTests(unittest.TestCase):
    def test_the_recorded_answer_carries_an_old_as_of_time(self):
        got = usage_as_of(usage_payload())
        self.assertEqual(got, t.parse_iso_utc("2026-01-01T12:00:00.500000+00:00"))

    def test_missing_or_bad_as_of_is_none(self):
        base = usage_payload()
        for mutate in (
            lambda b: b["rate_limits"].pop("seven_day_breakdown"),
            lambda b: b["rate_limits"]["seven_day_breakdown"].pop("as_of"),
            lambda b: b["rate_limits"]["seven_day_breakdown"].update(as_of="yesterday"),
            lambda b: b["rate_limits"]["seven_day_breakdown"].update(as_of=1730000000),
            lambda b: b["rate_limits"]["seven_day_breakdown"].update(as_of="2026-01-01T12:00:00"),  # naive
            lambda b: b.update(rate_limits=None),
        ):
            b = json.loads(json.dumps(base))
            mutate(b)
            self.assertIsNone(usage_as_of(b))
        self.assertIsNone(usage_as_of(None))
        self.assertIsNone(usage_as_of([]))


def failing_view(**status_kw) -> dict:
    st = stat(**status_kw)
    return state(claude=pview(status=st, snapshot=None)).to_json()


class FailingLabelTests(unittest.TestCase):
    def render(self, view: dict, now: float) -> str:
        s = settings(claude=True, codex=False).to_json()
        view.setdefault("max_wall_seen", None)
        return renderer.render_dicts(s, view, now)

    def test_hidden_values_after_a_failure_count_minutes_since_the_last_success(self):
        v = failing_view(data_state="transient_error", last_success_at=T0 - 1000, last_failure_at=T0 - 5,
                         consecutive_failures=3, first_failure_at=T0 - 700)
        self.assertEqual(self.render(v, T0), "Claude -- [failing 16m]")

    def test_never_succeeded_counts_from_the_first_failure_of_the_streak(self):
        v = failing_view(data_state="transient_error", last_success_at=None, last_failure_at=T0 - 5,
                         consecutive_failures=2, first_failure_at=T0 - 360)
        self.assertEqual(self.render(v, T0), "Claude -- [failing 6m]")

    def test_older_status_without_streak_start_uses_the_last_failure(self):
        v = failing_view(data_state="rate_limited", last_success_at=None, last_failure_at=T0 - 130,
                         consecutive_failures=1)
        self.assertEqual(self.render(v, T0), "Claude -- [failing 2m]")

    def test_no_label_when_the_last_attempt_succeeded_or_is_not_a_failure(self):
        ok = failing_view(data_state="ok", last_success_at=T0 - 1000, last_failure_at=T0 - 2000)
        self.assertEqual(self.render(ok, T0), "Claude --")
        unavailable = failing_view(data_state="unavailable", last_success_at=None, last_failure_at=T0 - 5)
        self.assertEqual(self.render(unavailable, T0), "Claude --")
        unsupported = failing_view(data_state="protocol_unsupported", last_success_at=None, last_failure_at=T0 - 5)
        self.assertEqual(self.render(unsupported, T0), "Claude --")

    def test_stale_label_is_unchanged_while_values_are_shown(self):
        v = state(claude=pview(status=stat(data_state="transient_error", last_failure_at=T0 - 1,
                                           consecutive_failures=1))).to_json()
        line = self.render(v, T0)
        self.assertIn("[stale", line)
        self.assertNotIn("failing", line)


class ManualRetryTests(unittest.TestCase):
    def pv(self, **kw) -> dict:
        base = dict(next_due_at=T0 + 400, consecutive_failures=3, attempt_started_at=T0 - 120,
                    attempt_finished_at=T0 - 119, data_state="transient_error")
        base.update(kw)
        return pview(status=stat(**base)).to_json()

    def test_manual_refresh_skips_the_backoff(self):
        self.assertIsNone(scheduler.provider_due(self.pv(), EPOCH, T0))
        self.assertEqual(scheduler.provider_due(self.pv(), EPOCH, T0, manual=True), scheduler.REASON_MANUAL_RETRY)

    def test_explicit_retry_not_before_still_wins(self):
        pv = self.pv(data_state="rate_limited", retry_not_before=T0 + 30)
        self.assertIsNone(scheduler.provider_due(pv, EPOCH, T0, manual=True))

    def test_manual_spacing_still_applies(self):
        pv = self.pv(attempt_started_at=T0 - 10, attempt_finished_at=T0 - 9)
        self.assertIsNone(scheduler.provider_due(pv, EPOCH, T0, manual=True))

    def test_tab_bar_never_dispatches_for_it(self):
        view = state(claude=pview(status=stat(next_due_at=T0 + 400, consecutive_failures=3,
                                              attempt_started_at=T0 - 120, attempt_finished_at=T0 - 119))).to_json()
        view["max_wall_seen"] = None
        due = scheduler.work_due_dicts(settings(codex=False).to_json(), view, T0)
        self.assertIsNone(due["claude"])


class TickErrorTests(unittest.TestCase):
    def test_an_internal_tick_error_is_reported_by_class_name(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = scheduler.work_due_dicts

        def boom(*a, **k):
            raise KeyError("secret-detail")

        scheduler.work_due_dicts = boom
        self.addCleanup(setattr, scheduler, "work_due_dicts", original)
        res = renderer.tick(os.path.join(tmp.name, "cfg"), os.path.join(tmp.name, "state"), {"HOME": tmp.name}, T0)
        self.assertEqual(res["error"], "KeyError")
        self.assertFalse(res["dispatch"])

    def test_a_clean_tick_has_no_error_key(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        res = renderer.tick(os.path.join(tmp.name, "cfg"), os.path.join(tmp.name, "state"), {"HOME": tmp.name}, T0)
        self.assertNotIn("error", res)


class StatusStreakFieldTests(unittest.TestCase):
    def test_round_trip_and_both_validators_agree(self):
        st = stat(data_state="transient_error", consecutive_failures=2, last_failure_at=T0, first_failure_at=T0 - 300)
        d = st.to_json()
        self.assertEqual(d["first_failure_at"], T0 - 300)
        self.assertEqual(t.ProviderStatus.from_json(d).first_failure_at, T0 - 300)
        self.assertEqual(cache.validate_status(d)["first_failure_at"], T0 - 300)

    def test_records_written_before_the_field_existed_still_load(self):
        d = stat().to_json()
        del d["first_failure_at"]
        self.assertIsNone(t.ProviderStatus.from_json(d).first_failure_at)
        self.assertIsNone(cache.validate_status(d)["first_failure_at"])

    def test_bad_values_are_rejected_by_both(self):
        d = stat().to_json()
        d["first_failure_at"] = "soon"
        with self.assertRaises(ValueError):
            t.ProviderStatus.from_json(d)
        with self.assertRaises(ValueError):
            cache.validate_status(d)


class SpecialFileReadTests(unittest.TestCase):
    def test_a_fifo_at_a_state_path_is_corrupt_and_never_blocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "status.json")
        os.mkfifo(path, 0o600)
        out: dict = {}
        th = threading.Thread(target=lambda: out.setdefault("r", cache.read_json_status(path)), daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "read blocked on a FIFO")
        self.assertEqual(out["r"][0], cache.CORRUPT)

    def test_regular_files_and_missing_files_are_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "x.json")
        self.assertEqual(cache.read_json_status(path), (cache.MISSING, None))
        with open(path, "w") as fh:
            fh.write('{"a": 1}')
        self.assertEqual(cache.read_json_status(path), (cache.OK, {"a": 1}))
        self.assertEqual(cache.read_regular_bytes(path, 3), b'{"a')


if __name__ == "__main__":
    unittest.main()
