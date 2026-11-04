"""Tests for the shared data contract (quotaribbon.types)."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
import unittest

from quotaribbon import types as t

HOME = os.path.join(tempfile.gettempdir(), "qr-home")


def window(**kw):
    base = dict(
        id="five_hour",
        scope="all",
        duration_seconds=18000,
        used_percent=32.0,
        raw_used_percent=32,
        resets_at="2026-11-02T08:00:00Z",
    )
    base.update(kw)
    return t.Window(**base)


def auth(**kw):
    base = dict(
        state="subscription",
        account_key="acct:000000000000000000000000",
        config_dir=os.path.join(HOME, ".claude"),
        detail="auth_method:claude.ai",
        retryable=False,
        env_conflicts=("ANTHROPIC_API_KEY",),
    )
    base.update(kw)
    return t.AuthResult(**base)


def snapshot(**kw):
    base = dict(
        schema_version=1,
        provider="claude",
        scope_key="scope",
        profile_key="0123456789abcdef",
        account_key="acct:000000000000000000000000",
        auth_epoch=1,
        source="claude-control-get-usage",
        fetched_at="2026-11-02T05:00:00.123456Z",
        source_observed_at=None,
        status="ok",
        windows=[window(), window(id="seven_day", duration_seconds=604800, used_percent=0, raw_used_percent=0)],
        cli_version="2.1.316",
        server_epoch="abcd",
    )
    base.update(kw)
    return t.Snapshot(**base)


def connection(**kw):
    base = dict(
        schema_version=1,
        provider="codex",
        binding_key="b" * 24,
        profile_key="p" * 16,
        state="connected",
        ever_connected=True,
        evidence={"source": "integration", "integration_state": "current", "observed_at": 1700000000.0},
        executable="/opt/homebrew/bin/codex",
        cli_version="0.166.0",
        account_key=None,
        auth_epoch=3,
        verified_at=1700000000.5,
        verified_server_epoch="abcd",
        verified_generation=2,
        next_verify_at=1700000300,
        last_reason=None,
        env_conflicts=["OPENAI_API_KEY"],
    )
    base.update(kw)
    return t.ConnectionRecord(**base)


def status(**kw):
    base = dict(
        schema_version=1,
        provider="claude",
        data_state="ok",
        attempt_started_at=1700000000.0,
        attempt_finished_at=1700000001.0,
        last_success_at=1700000001.0,
        last_failure_at=None,
        consecutive_failures=0,
        next_due_at=1700000121.0,
        retry_not_before=None,
        last_error_code=None,
        unsupported_cli_version=None,
        notes=["window_not_provided:seven_day"],
    )
    base.update(kw)
    return t.ProviderStatus(**base)


def collect(**kw):
    base = dict(
        outcome="ok",
        windows=(window(),),
        auth_before=auth(),
        auth_after=auth(),
        source="claude-control-get-usage",
        source_observed_at=None,
        error_code=None,
        retry_not_before=None,
        cli_version="2.1.316",
        notes=("ignored_model_buckets:3", "window_not_provided:seven_day"),
    )
    base.update(kw)
    return t.CollectResult(**base)


def evidence(**kw):
    base = dict(
        linked=True,
        source="agent",
        integration_state=None,
        agent_detected=True,
        profile_correspondence="unknown",
        error=None,
    )
    base.update(kw)
    return t.Evidence(**base)


def roundtrip(cls, obj):
    """to_json → real JSON text → from_json must reproduce an equal object."""
    text = json.dumps(obj.to_json(), allow_nan=False)
    return cls.from_json(json.loads(text))


def minimal_base() -> dict:
    return {"schema_version": 1, "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}}


class RoundTripTests(unittest.TestCase):
    def test_window(self):
        self.assertEqual(roundtrip(t.Window, window()), window())

    def test_auth_tuple_serialises_as_list(self):
        d = auth().to_json()
        self.assertEqual(d["env_conflicts"], ["ANTHROPIC_API_KEY"])
        self.assertEqual(roundtrip(t.AuthResult, auth()), auth())

    def test_collect_result(self):
        r = collect()
        d = r.to_json()
        self.assertIsInstance(d["windows"], list)
        self.assertIsInstance(d["notes"], list)
        self.assertEqual(roundtrip(t.CollectResult, r), r)
        self.assertEqual(roundtrip(t.CollectResult, collect(auth_before=None, auth_after=None, windows=())),
                         collect(auth_before=None, auth_after=None, windows=()))

    def test_evidence(self):
        self.assertEqual(roundtrip(t.Evidence, evidence()), evidence())

    def test_settings_default_and_overrides(self):
        s = t.Settings.default()
        self.assertEqual(roundtrip(t.Settings, s), s)
        s2 = t.Settings(
            schema_version=1,
            providers={
                "claude": t.ProviderSettings(enabled=False, binary="/usr/local/bin/claude"),
                "codex": t.ProviderSettings(enabled=True, profile_dir=os.path.join(HOME, ".codex")),
            },
            display_mode="used",
            interval_seconds=60,
            stale_after_seconds=120,
            hide_values_after_seconds=900,
        )
        self.assertEqual(roundtrip(t.Settings, s2), s2)
        self.assertEqual(s2.display_format, "full")  # optional field: older constructors keep working
        s3 = dataclasses.replace(s2, display_format="compact")
        self.assertEqual(roundtrip(t.Settings, s3), s3)
        self.assertEqual(t.Settings.from_json(s3.to_file_json()), s3)
        self.assertNotIn("format", s2.to_file_json()["display"])
        with self.assertRaises(ValueError):
            dataclasses.replace(s2, display_format="short").validate()
        self.assertTrue(s2.display_show_reset)  # optional field: on by default
        self.assertNotIn("show_reset", s2.to_file_json()["display"])
        s4 = dataclasses.replace(s3, display_show_reset=False)
        self.assertEqual(roundtrip(t.Settings, s4), s4)
        self.assertEqual(t.Settings.from_json(s4.to_file_json()), s4)
        self.assertIs(s4.to_file_json()["display"]["show_reset"], False)
        for bad in (1, 0, "true", None):
            with self.assertRaises(ValueError):
                dataclasses.replace(s2, display_show_reset=bad).validate()
        self.assertTrue(s2.display_window_labels)
        self.assertNotIn("window_labels", s2.to_file_json()["display"])
        s5 = dataclasses.replace(s4, display_window_labels=False)
        self.assertEqual(roundtrip(t.Settings, s5), s5)
        self.assertEqual(t.Settings.from_json(s5.to_file_json()), s5)
        self.assertIs(s5.to_file_json()["display"]["window_labels"], False)
        for bad in (1, 0, "false", None):
            with self.assertRaises(ValueError):
                dataclasses.replace(s2, display_window_labels=bad).validate()

    def test_full_and_partial_settings_files_fill_in_defaults(self):
        full = {
            "schema_version": 1,
            "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}},
            "display": {"mode": "used"},
            "refresh": {"interval_seconds": 120, "stale_after_seconds": 300, "hide_values_after_seconds": 900},
        }
        self.assertEqual(t.Settings.from_json(full), t.Settings.default())
        self.assertEqual(t.Settings.default().to_file_json()["refresh"], {"interval_seconds": 120})
        long = t.Settings.from_json({**minimal_base(), "refresh": {"interval_seconds": 600}})
        self.assertEqual((long.stale_after_seconds, long.hide_values_after_seconds), (1200, 1800))
        self.assertEqual(long.to_json()["refresh"],
                         {"interval_seconds": 600, "stale_after_seconds": 1200, "hide_values_after_seconds": 1800})
        self.assertEqual(t.Settings.from_json(long.to_file_json()), long)
        minimal = {"schema_version": 1, "providers": {"claude": {"enabled": True}, "codex": {"enabled": False}}}
        s = t.Settings.from_json(minimal)
        self.assertEqual(s.interval_seconds, 120)
        self.assertFalse(s.providers["codex"].enabled)

    def test_snapshot(self):
        s = snapshot()
        self.assertEqual(roundtrip(t.Snapshot, s), s)
        self.assertEqual(list(s.to_json()), list(t.Snapshot._FIELDS))

    def test_connection(self):
        c = connection()
        self.assertEqual(roundtrip(t.ConnectionRecord, c), c)
        c2 = connection(evidence=None, verified_at=None, verified_server_epoch=None, verified_generation=None,
                        executable=None, state="unverified", ever_connected=False, env_conflicts=[])
        self.assertEqual(roundtrip(t.ConnectionRecord, c2), c2)

    def test_status(self):
        self.assertEqual(roundtrip(t.ProviderStatus, status()), status())

    def test_views(self):
        pv = t.ProviderView(connection=connection(), status=status(provider="codex"), snapshot=None, generation=2)
        self.assertEqual(roundtrip(t.ProviderView, pv), pv)
        sv = t.StateView(
            server_epoch="abcd",
            providers={"codex": pv, "claude": t.ProviderView(None, None, None, 0)},
            dispatch={"id": "lease-1", "dispatched_at": 1700000000.0, "finished_at": None},
        )
        self.assertEqual(roundtrip(t.StateView, sv), sv)
        self.assertEqual(list(sv.to_json()["providers"]), ["claude", "codex"])


class StrictRejectionTests(unittest.TestCase):
    def assertRejects(self, cls, d):
        with self.assertRaises(ValueError):
            cls.from_json(d)

    def test_non_object(self):
        for cls in (t.Window, t.AuthResult, t.Snapshot, t.Settings, t.ConnectionRecord, t.ProviderStatus):
            self.assertRejects(cls, [])
            self.assertRejects(cls, None)

    def test_missing_and_unknown_keys(self):
        d = window().to_json()
        del d["resets_at"]
        self.assertRejects(t.Window, d)
        d = window().to_json()
        d["extra"] = 1
        self.assertRejects(t.Window, d)
        d = snapshot().to_json()
        del d["server_epoch"]
        self.assertRejects(t.Snapshot, d)

    def test_nan_and_inf(self):
        for bad in (math.nan, math.inf, -math.inf):
            d = window().to_json()
            d["used_percent"] = bad
            self.assertRejects(t.Window, d)
            d = status().to_json()
            d["next_due_at"] = bad
            self.assertRejects(t.ProviderStatus, d)
            d = window().to_json()
            d["raw_used_percent"] = bad
            self.assertRejects(t.Window, d)
            with self.assertRaises(ValueError):
                window(raw_used_percent=bad).to_json()

    def test_bool_is_not_int_or_float(self):
        d = window().to_json()
        d["duration_seconds"] = True
        self.assertRejects(t.Window, d)
        d = window().to_json()
        d["used_percent"] = True
        self.assertRejects(t.Window, d)
        d = snapshot().to_json()
        d["auth_epoch"] = False
        self.assertRejects(t.Snapshot, d)
        d = snapshot().to_json()
        d["schema_version"] = True
        self.assertRejects(t.Snapshot, d)
        d = status().to_json()
        d["consecutive_failures"] = True
        self.assertRejects(t.ProviderStatus, d)
        d = t.Settings.default().to_json()
        d["refresh"]["interval_seconds"] = True
        self.assertRejects(t.Settings, d)

    def test_wrong_types(self):
        d = auth().to_json()
        d["retryable"] = 0
        self.assertRejects(t.AuthResult, d)
        d = connection().to_json()
        d["ever_connected"] = "yes"
        self.assertRejects(t.ConnectionRecord, d)
        d = snapshot().to_json()
        d["auth_epoch"] = 1.0
        self.assertRejects(t.Snapshot, d)
        d = window().to_json()
        d["used_percent"] = "32"
        self.assertRejects(t.Window, d)
        d = connection().to_json()
        d["env_conflicts"] = ["not a name!"]
        self.assertRejects(t.ConnectionRecord, d)

    def test_unknown_enums(self):
        cases = [
            (t.Window, window().to_json(), "id", "opus"),
            (t.Window, window().to_json(), "scope", "model"),
            (t.AuthResult, auth().to_json(), "state", "maybe"),
            (t.CollectResult, collect().to_json(), "outcome", "fine"),
            (t.CollectResult, collect().to_json(), "source", "web-scrape"),
            (t.Evidence, evidence().to_json(), "profile_correspondence", "probably"),
            (t.Evidence, evidence().to_json(), "source", "guess"),
            (t.Snapshot, snapshot().to_json(), "status", "stale"),
            (t.Snapshot, snapshot().to_json(), "provider", "gemini"),
            (t.ConnectionRecord, connection().to_json(), "state", "online"),
            (t.ProviderStatus, status().to_json(), "data_state", "great"),
        ]
        for cls, d, key, value in cases:
            with self.subTest(cls=cls.__name__, key=key):
                d[key] = value
                self.assertRejects(cls, d)

    def test_snapshot_source_must_match_provider(self):
        d = snapshot().to_json()
        d["source"] = "codex-app-server-rate-limits"
        self.assertRejects(t.Snapshot, d)

    def test_schema_version(self):
        for cls, d in ((t.Snapshot, snapshot().to_json()), (t.ConnectionRecord, connection().to_json()),
                       (t.ProviderStatus, status().to_json()), (t.Settings, t.Settings.default().to_json())):
            d["schema_version"] = 2
            self.assertRejects(cls, d)

    def test_percent_out_of_range_rejected_not_clamped(self):
        for bad in (-0.1, 100.01, 150):
            d = window().to_json()
            d["used_percent"] = bad
            self.assertRejects(t.Window, d)

    def test_bad_timestamps(self):
        for bad in ("2026-11-02 05:00:00Z", "2026-11-02T05:00:00", "yesterday", "2026-13-02T05:00:00Z"):
            d = snapshot().to_json()
            d["fetched_at"] = bad
            self.assertRejects(t.Snapshot, d)

    def test_settings_relations(self):
        def with_refresh(**kw):
            d = t.Settings.default().to_json()
            d["refresh"].update(kw)
            return d

        self.assertRejects(t.Settings, with_refresh(interval_seconds=59))
        self.assertRejects(t.Settings, with_refresh(interval_seconds=901))
        self.assertRejects(t.Settings, with_refresh(stale_after_seconds=200))  # < 2 * 120
        self.assertRejects(t.Settings, with_refresh(hide_values_after_seconds=300))  # not > stale
        self.assertRejects(t.Settings, with_refresh(hide_values_after_seconds=86401))
        self.assertRejects(t.Settings, with_refresh(hide_values_after_seconds=3601))
        widest = t.Settings.from_json(with_refresh(interval_seconds=900, stale_after_seconds=1800,
                                                   hide_values_after_seconds=3600))
        self.assertEqual(widest.hide_values_after_seconds, 3600)
        self.assertRejects(t.Settings, with_refresh(interval_seconds=10 ** 400))
        d = t.Settings.default().to_json()
        d["display"]["mode"] = "remaining"
        self.assertEqual(t.Settings.from_json(d).display_mode, "remaining")
        d["display"]["mode"] = "left"
        self.assertRejects(t.Settings, d)
        d["display"]["mode"] = ["used"]
        self.assertRejects(t.Settings, d)
        d = t.Settings.default().to_json()
        self.assertEqual(d["display"], {"mode": "used", "format": "full", "show_reset": True, "window_labels": True})
        d["display"]["format"] = "compact"
        self.assertEqual(t.Settings.from_json(d).display_format, "compact")
        for bad in ("short", 1, None, ["compact"]):
            d["display"]["format"] = bad
            self.assertRejects(t.Settings, d)
        d = t.Settings.default().to_json()
        d["display"]["show_reset"] = False
        self.assertFalse(t.Settings.from_json(d).display_show_reset)
        for bad in (1, 0, "true", None, [True]):
            d["display"]["show_reset"] = bad
            self.assertRejects(t.Settings, d)
        d = t.Settings.default().to_json()
        d["display"]["window_labels"] = False
        self.assertFalse(t.Settings.from_json(d).display_window_labels)
        for bad in (1, 0, "false", None, [False]):
            d["display"]["window_labels"] = bad
            self.assertRejects(t.Settings, d)
        d = t.Settings.default().to_json()
        del d["providers"]["codex"]
        self.assertRejects(t.Settings, d)
        d = t.Settings.default().to_json()
        d["providers"]["claude"]["binary"] = "claude"  # relative override
        self.assertRejects(t.Settings, d)

    def test_evidence_record_shape(self):
        d = connection().to_json()
        d["evidence"] = {"source": "integration"}
        self.assertRejects(t.ConnectionRecord, d)

    def test_dispatch_shape(self):
        with self.assertRaises(ValueError):
            t.validate_dispatch({"id": "x", "dispatched_at": 1.0})
        with self.assertRaises(ValueError):
            t.validate_dispatch({"id": "x", "dispatched_at": True, "finished_at": None})

    def test_to_json_refuses_invalid_objects(self):
        with self.assertRaises(ValueError):
            window(used_percent=math.nan).to_json()
        with self.assertRaises(ValueError):
            snapshot(status="partial").to_json()


class HelperTests(unittest.TestCase):
    def test_duration_labels(self):
        table = {18000: "5h", 604800: "7d", 900: "15m", 3600: "1h", 90: "90s", 86400: "1d", 60: "1m", 7200: "2h"}
        for seconds, label in table.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(t.duration_label(seconds), label)
        for bad in (0, -60, True, 1.5, None):
            with self.assertRaises(ValueError):
                t.duration_label(bad)

    def test_is_valid_percent(self):
        for good in (0, 0.0, 32, 99.9, 100, 100.0):
            self.assertTrue(t.is_valid_percent(good), good)
        for bad in (-0.001, 100.0001, math.nan, math.inf, True, False, None, "50", [50]):
            self.assertFalse(t.is_valid_percent(bad), bad)

    def test_iso_roundtrip(self):
        ts = 1793595600.0
        self.assertEqual(t.iso_utc(ts), "2026-11-02T05:00:00Z")
        self.assertEqual(t.parse_iso_utc("2026-11-02T05:00:00Z"), ts)
        self.assertEqual(t.iso_utc(ts + 0.999), "2026-11-02T05:00:00Z")

    def test_parse_iso_variants(self):
        ts = 1793595600.0
        self.assertEqual(t.parse_iso_utc("2026-11-02T05:00:00+00:00"), ts)
        self.assertEqual(t.parse_iso_utc("2026-11-02T14:00:00+09:00"), ts)
        self.assertEqual(t.parse_iso_utc("2026-11-02T00:00:00-05:00"), ts)
        self.assertAlmostEqual(t.parse_iso_utc("2026-11-02T05:00:00.250Z"), ts + 0.25)
        for bad in ("2026-11-02T05:00:00", "2026-11-02", "2026-11-02T05:00Z", "2026-11-02T05:00:00+0900", 5, None):
            with self.assertRaises(ValueError):
                t.parse_iso_utc(bad)

    def test_iso_utc_rejects_bad_input(self):
        for bad in (math.nan, math.inf, True, "1"):
            with self.assertRaises(ValueError):
                t.iso_utc(bad)

    def test_raw_percent_or_none(self):
        self.assertEqual(t.raw_percent_or_none(32), 32)
        self.assertEqual(t.raw_percent_or_none(150.5), 150.5)  # kept; used_percent is None
        for bad in (math.nan, math.inf, -math.inf, True, "32", [32], {"v": 1}, None):
            self.assertIsNone(t.raw_percent_or_none(bad), bad)
        w = window(used_percent=None, raw_used_percent=t.raw_percent_or_none(math.nan))
        self.assertEqual(roundtrip(t.Window, w), w)
        out_of_range = window(used_percent=None, raw_used_percent=150)
        self.assertEqual(roundtrip(t.Window, out_of_range), out_of_range)

    def test_iso_utc_precise(self):
        ts = 1793595600.123456
        s = t.iso_utc_precise(ts)
        self.assertEqual(s, "2026-11-02T05:00:00.123456Z")
        self.assertEqual(t.parse_iso_utc(s), ts)
        self.assertEqual(t.iso_utc_precise(1793595600), "2026-11-02T05:00:00.000000Z")
        self.assertEqual(t.iso_utc_precise(1793595600.9999996), "2026-11-02T05:00:01.000000Z")
        # Full-precision ordering: sub-second differences survive the round trip.
        a, b = t.iso_utc_precise(ts), t.iso_utc_precise(ts + 0.000001)
        self.assertLess(t.parse_iso_utc(a), t.parse_iso_utc(b))
        for bad in (math.nan, True, "1"):
            with self.assertRaises(ValueError):
                t.iso_utc_precise(bad)

    def test_precise_string_roundtrip_many(self):
        import random

        rng = random.Random(1234)
        for _ in range(2000):
            ts = rng.uniform(1.6e9, 2.2e9)
            s = t.iso_utc_precise(ts)
            self.assertEqual(t.iso_utc_precise(t.parse_iso_utc(s)), s)
            self.assertLessEqual(abs(t.parse_iso_utc(s) - ts), 0.000001)  # 1 µs rounding + float ulp
        self.assertEqual(t.parse_iso_utc("2026-11-02T05:00:00.000000001Z"), 1793595600.000000001)

    def test_snapshot_fetched_at_full_precision(self):
        s = snapshot(fetched_at=t.iso_utc_precise(1793595600.654321))
        back = roundtrip(t.Snapshot, s)
        self.assertEqual(back.fetched_at, "2026-11-02T05:00:00.654321Z")
        self.assertEqual(t.parse_iso_utc(back.fetched_at), 1793595600.654321)
        self.assertEqual(roundtrip(t.Snapshot, snapshot(fetched_at="2026-11-02T05:00:00Z")).fetched_at,
                         "2026-11-02T05:00:00Z")

    def test_make_note(self):
        self.assertEqual(t.make_note("unexpected_turn"), "unexpected_turn")
        self.assertEqual(t.make_note("ignored_model_buckets", 3), "ignored_model_buckets:3")
        self.assertEqual(t.make_note("window_not_provided:seven_day"), "window_not_provided:seven_day")
        required = {
            "window_not_provided:five_hour", "window_not_provided:seven_day", "ignored_model_buckets",
            "no_common_bucket", "invalid_percent", "percent_out_of_range", "unknown_duration",
            "unexpected_turn", "managed_policy_present", "rate_limits_null", "rate_limits_unavailable",
            "legacy_bucket_used",
        }
        self.assertLessEqual(required, t.NOTE_CODES)
        for code in t.NOTE_CODES:
            self.assertTrue(t.is_valid_note(t.make_note(code)))
            self.assertTrue(t.is_valid_note(t.make_note(code, 0)))
        for bad_code in ("ignored_bucket:opus", "window_not_provided:opus", "", None):
            with self.assertRaises(ValueError):
                t.make_note(bad_code)
        for bad_count in (-1, True, 1.0, "3"):
            with self.assertRaises(ValueError):
                t.make_note("ignored_model_buckets", bad_count)
        for bad in ("ignored_model_buckets:opus", "ignored_model_buckets:-1", "ignored_model_buckets:03",
                    "ignored_bucket:claude-opus", "unexpected_turn:", "x", 3):
            self.assertFalse(t.is_valid_note(bad), bad)

    def test_notes_must_use_vocabulary(self):
        d = collect().to_json()
        d["notes"] = ["ignored_bucket:claude-opus-4"]
        with self.assertRaises(ValueError):
            t.CollectResult.from_json(d)
        d = status().to_json()
        d["notes"] = ["free text"]
        with self.assertRaises(ValueError):
            t.ProviderStatus.from_json(d)
        with self.assertRaises(ValueError):
            status(notes=["ignored_bucket:x"]).to_json()

    def test_control_record(self):
        c = t.default_control()
        self.assertEqual(t.validate_control(c), c)
        good = {
            "schema_version": 1,
            "providers": {
                "claude": {"generation": 4, "settings_fingerprint": "ab" * 32},
                "codex": {"generation": 0, "settings_fingerprint": None},
            },
        }
        self.assertEqual(t.validate_control(good), good)

        def variant(fn):
            d = json.loads(json.dumps(good))
            fn(d)
            return d

        bads = [
            variant(lambda d: d.update(schema_version=2)),
            variant(lambda d: d["providers"].pop("codex")),
            variant(lambda d: d["providers"].update(gemini={"generation": 0, "settings_fingerprint": None})),
            variant(lambda d: d["providers"]["claude"].update(generation=-1)),
            variant(lambda d: d["providers"]["claude"].update(generation=True)),
            variant(lambda d: d["providers"]["claude"].update(generation=1.0)),
            variant(lambda d: d["providers"]["claude"].update(settings_fingerprint=5)),
            variant(lambda d: d["providers"]["claude"].pop("settings_fingerprint")),
            variant(lambda d: d["providers"]["claude"].update(extra=1)),
            variant(lambda d: d.update(extra=1)),
            [],
            None,
        ]
        for bad in bads:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    t.validate_control(bad)

    def test_protocols_are_runtime_checkable(self):
        class FakeCollector:
            provider = "claude"

            def cli_version(self):
                return None

            def check_auth(self, timeout_s, admit):
                return auth()

            def collect(self, timeout_s, admit):
                if not admit():
                    return collect(outcome="transient_error", windows=(), error_code=t.ADMISSION_REVOKED,
                                   notes=())
                return collect()

        class FakeHost:
            def herdr_config_path(self): ...
            def evidence(self, provider, profile_dir): ...
            def resolve_profile(self, provider, ps): ...
            def resolve_executable(self, provider, ps): ...
            def make_collector(self, provider, executable, profile_dir, *, lock_fd=None): ...
            def server_identity(self): ...
            def credential_namespace(self, provider, ps): ...

        class FakeHostWithActive(FakeHost):
            def plugin_active(self):
                return False

        c = FakeCollector()
        self.assertIsInstance(c, t.Collector)
        self.assertEqual(c.collect(1.0, lambda: False).error_code, "admission_revoked")
        self.assertEqual(c.collect(1.0, lambda: True).outcome, "ok")
        self.assertNotIsInstance(object(), t.HostAdapter)
        self.assertNotIsInstance(FakeHost(), t.HostAdapter)  # plugin_active is required
        self.assertIsInstance(FakeHostWithActive(), t.HostAdapter)

if __name__ == "__main__":
    unittest.main()
