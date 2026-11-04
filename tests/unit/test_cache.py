"""State stores: strict lean validators, per-binding isolation, epoch/clock/salt/control files."""

from __future__ import annotations

import copy
import json
import os
import random
import stat
import threading
import unittest

from quotaribbon import binding, cache
from quotaribbon import types as t
from tests.helpers.core_fakes import ACCT_A, T0, temp_paths, window


def make_binding(provider="claude", profile="/profiles/x"):
    return binding.make_binding(provider, profile, "/cfg/config.toml", host="h", uid=501)


def connection(b, provider="claude", **kw) -> t.ConnectionRecord:
    base = dict(
        schema_version=1, provider=provider, binding_key=b.binding_key, profile_key=b.profile_key, state="connected",
        ever_connected=True, evidence={"source": "integration", "integration_state": "current", "observed_at": T0},
        executable="/usr/bin/claude", cli_version="2.1.0", account_key=ACCT_A, auth_epoch=1, verified_at=T0,
        verified_server_epoch="e1", verified_generation=3, next_verify_at=T0, last_reason=None, env_conflicts=["ANTHROPIC_API_KEY"],
    )
    base.update(kw)
    return t.ConnectionRecord(**base)


def status(provider="claude", **kw) -> t.ProviderStatus:
    base = dict(
        schema_version=1, provider=provider, data_state="ok", attempt_started_at=T0, attempt_finished_at=T0 + 1,
        last_success_at=T0, last_failure_at=None, consecutive_failures=0, next_due_at=T0 + 120, retry_not_before=None,
        last_error_code=None, unsupported_cli_version=None, notes=["ignored_model_buckets:3"],
    )
    base.update(kw)
    return t.ProviderStatus(**base)


def snapshot(b, provider="claude", **kw) -> t.Snapshot:
    base = dict(
        schema_version=1, provider=provider, scope_key="s", profile_key=b.profile_key, account_key=ACCT_A, auth_epoch=1,
        source=t.PROVIDER_SOURCES[provider], fetched_at=t.iso_utc_precise(T0), source_observed_at=None, status="ok",
        windows=[window("five_hour", 18000, 32.0, "2027-01-15T10:00:00Z"), window("seven_day", 604800, None)],
        cli_version="2.1.0", server_epoch="e1",
    )
    base.update(kw)
    return t.Snapshot(**base)


def _mutations(d: dict):
    """Yield (name, mutated copy) pairs: drop / add / retype each key."""
    yield "extra_key", {**d, "zzz": 1}
    for k, v in d.items():
        m = dict(d)
        del m[k]
        yield f"drop_{k}", m
        for name, bad in (("null", None), ("bool", True), ("str", "x"), ("nan", float("nan")), ("neg", -1), ("list", [1]), ("float", 1.5)):
            m = dict(d)
            m[k] = bad
            yield f"{k}_{name}", m


class LeanValidatorParityTests(unittest.TestCase):
    """The tick reads records without the dataclass layer; acceptance must be identical."""

    def check(self, valid: dict, lean, strict):
        for name, obj in [("valid", valid), *_mutations(valid)]:
            with self.subTest(name):
                try:
                    strict(copy.deepcopy(obj))
                    expected = True
                except ValueError:
                    expected = False
                try:
                    lean(copy.deepcopy(obj))
                    got = True
                except ValueError:
                    got = False
                self.assertEqual(got, expected)

    def test_connection_validator_parity(self):
        b = make_binding()
        self.check(connection(b).to_json(), cache.validate_connection, t.ConnectionRecord.from_json)
        self.check(connection(b, evidence=None, executable=None, verified_at=None).to_json(), cache.validate_connection, t.ConnectionRecord.from_json)
        ev = connection(b).to_json()["evidence"]
        for name, m in _mutations(ev):
            obj = connection(b).to_json()
            obj["evidence"] = m
            with self.subTest("evidence_" + name):
                self.check(obj, cache.validate_connection, t.ConnectionRecord.from_json)

    def test_status_validator_parity(self):
        self.check(status().to_json(), cache.validate_status, t.ProviderStatus.from_json)
        bad_notes = status().to_json()
        bad_notes["notes"] = ["bucket:secret-codename"]
        self.check(bad_notes, cache.validate_status, t.ProviderStatus.from_json)

    def test_snapshot_and_window_validator_parity(self):
        b = make_binding()
        snap = snapshot(b).to_json()
        self.check(snap, cache.validate_snapshot, t.Snapshot.from_json)
        for name, m in _mutations(snap["windows"][0]):
            obj = copy.deepcopy(snap)
            obj["windows"][0] = m
            with self.subTest("window_" + name):
                self.check(obj, cache.validate_snapshot, t.Snapshot.from_json)
        for pct in (0, 99.9, 100, 100.0001, -0.1, float("inf")):
            obj = copy.deepcopy(snap)
            obj["windows"][0]["used_percent"] = pct
            self.check(obj, cache.validate_snapshot, t.Snapshot.from_json)

    def test_control_and_dispatch_validator_parity(self):
        self.check(t.default_control(), cache.validate_control, t.validate_control)
        self.check({"id": "x", "dispatched_at": T0, "finished_at": None}, cache.validate_dispatch, t.validate_dispatch)

    def test_vocabularies_match_the_contract(self):
        self.assertEqual(cache.PROVIDERS, t.PROVIDERS)
        self.assertEqual(cache.WINDOW_IDS, t.WINDOW_IDS)
        self.assertEqual(cache.CONNECTION_STATES, t.CONNECTION_STATES)
        self.assertEqual(cache.DATA_STATES, t.DATA_STATES)
        self.assertEqual(cache.NOTE_CODES, t.NOTE_CODES)
        self.assertEqual(cache.EVIDENCE_SOURCES, t.EVIDENCE_SOURCES)
        self.assertEqual(cache.INTEGRATION_STATES, t.INTEGRATION_STATES)
        self.assertEqual(dict(cache.PROVIDER_SOURCES), dict(t.PROVIDER_SOURCES))
        self.assertEqual(cache.CONNECTION_FIELDS, t.ConnectionRecord._FIELDS)
        self.assertEqual(cache.STATUS_FIELDS, t.ProviderStatus._FIELDS)
        self.assertEqual(cache.SNAPSHOT_FIELDS, t.Snapshot._FIELDS)

    def test_iso_parser_matches_the_contract(self):
        samples = [
            "2026-11-02T05:00:00Z", "2026-11-02T05:00:00.5Z", "2026-11-02T05:00:00.123456789Z",
            "2024-02-29T23:59:59+09:00", "2023-02-29T00:00:00Z", "2026-13-01T00:00:00Z", "2026-11-02T24:00:00Z",
            "2026-11-02T05:00:60Z", "2026-11-02 05:00:00Z", "2026-11-02T05:00:00", "2026-11-02T05:00:00+24:00",
            "0001-01-01T00:00:00-23:59", "9999-12-31T23:59:59Z", "2026-11-02T05:00:00.Z", "2026-11-02T05:00:00.1234567890Z",
            "", "2026-1-02T05:00:00Z", "1970-01-01T00:00:00Z", "1969-12-31T23:59:59.999Z",
        ]
        rng = random.Random(7)
        for _ in range(300):
            ts = rng.uniform(-1e9, 4e9)
            samples.append(t.iso_utc_precise(ts))
            samples.append(t.iso_utc(ts)[:-1] + rng.choice(["+05:30", "-11:00", "Z"]))
        for s in samples:
            with self.subTest(s):
                try:
                    expected = t.parse_iso_utc(s)
                except ValueError:
                    expected = None
                try:
                    got = cache.parse_iso(s)
                except ValueError:
                    got = None
                self.assertEqual(got, expected)

    def test_both_iso_parsers_reject_non_ascii_digits(self):
        for bad in ("\uff12\uff10\uff12\uff16-11-02T05:00:00Z", "2026-11-02T05:00:00.\u0661Z",
                    "2026-11-02T05:00:00+0\u0665:00"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    cache.parse_iso(bad)
                with self.assertRaises(ValueError):
                    t.parse_iso_utc(bad)

    def test_path_layout_matches_paths_module(self):
        paths = temp_paths(self)
        b = make_binding()
        self.assertEqual(cache.control_path(paths.state_dir), paths.control)
        self.assertEqual(cache.server_epoch_path(paths.state_dir), paths.server_epoch)
        self.assertEqual(cache.clock_path(paths.state_dir), paths.clock)
        self.assertEqual(cache.dispatch_path(paths.state_dir), paths.dispatch)
        self.assertEqual(cache.dispatch_lock_path(paths.state_dir), paths.dispatch_lock)
        self.assertEqual(cache.record_path(paths.state_dir, "codex", b.binding_key, "snapshot"), paths.snapshot("codex", b.binding_key))
        with self.assertRaises(ValueError):
            cache.record_path(paths.state_dir, "claude", "../../etc", "snapshot")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.paths.ensure_private_dirs()
        self.b = make_binding()

    def write_all(self, b=None, provider="claude"):
        b = b or self.b
        cache.write_connection(self.paths, connection(b, provider=provider))
        cache.write_status(self.paths, status(provider=provider), b.binding_key)
        cache.write_snapshot(self.paths, snapshot(b, provider=provider), b.binding_key)

    def test_round_trip_through_load_view(self):
        self.write_all()
        view = cache.load_view(self.paths, {"claude": self.b})
        pv = view.providers["claude"]
        self.assertEqual(pv.connection, connection(self.b))
        self.assertEqual(pv.status, status())
        self.assertEqual(pv.snapshot, snapshot(self.b))
        self.assertEqual(pv.generation, 0)
        self.assertNotIn("codex", view.providers)

    def test_load_view_without_bindings_derives_them_from_settings_and_env(self):
        from quotaribbon import settings as settings_mod

        env = {"HOME": os.path.dirname(self.paths.state_dir), "PATH": "/usr/bin:/bin"}
        b = binding.binding_for("claude", settings_mod.DEFAULT_SETTINGS, env)
        cache.write_connection(self.paths, connection(b))
        view = cache.load_view(self.paths, None, env=env)
        self.assertEqual(sorted(view.providers), sorted(t.PROVIDERS))
        self.assertEqual(view.providers["claude"].connection, connection(b))
        self.assertIsNone(view.providers["codex"].connection)

    def test_records_are_private_files(self):
        self.write_all()
        mode = stat.S_IMODE(os.stat(self.paths.snapshot("claude", self.b.binding_key)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_files_read_as_absent(self):
        d = cache.load_view_dicts(self.paths.state_dir, {"claude": self.b})
        self.assertEqual(d["providers"]["claude"]["connection"], None)
        self.assertIsNone(d["server_epoch"])
        self.assertIsNone(d["dispatch"])

    def test_invalid_snapshot_files_are_ignored(self):
        self.write_all()
        path = self.paths.snapshot("claude", self.b.binding_key)
        good = snapshot(self.b).to_json()
        bad_variants = {
            "malformed": "{\"schema_version\": 1,",
            "nan_percent": json.dumps(good).replace("32.0", "NaN"),
            "out_of_range": json.dumps({**good, "windows": [{**good["windows"][0], "used_percent": 120.0}]}),
            "string_percent": json.dumps({**good, "windows": [{**good["windows"][0], "used_percent": "32"}]}),
            "wrong_schema": json.dumps({**good, "schema_version": 2}),
            "wrong_source": json.dumps({**good, "source": "statusline"}),
            "other_provider": json.dumps(snapshot(self.b, provider="codex").to_json()),
            "binary_garbage": "\x00\xff",
        }
        for name, text in bad_variants.items():
            with self.subTest(name):
                with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
                    fh.write(text)
                self.assertIsNone(cache.load_view(self.paths, {"claude": self.b}).providers["claude"].snapshot)
                self.assertIsNone(cache.read_snapshot(self.paths, "claude", self.b.binding_key) if name != "other_provider" else None)

    def test_connection_of_another_binding_is_ignored(self):
        other = make_binding(profile="/profiles/other")
        rec = connection(other)
        os.makedirs(self.paths.binding_dir("claude", self.b.binding_key), exist_ok=True)
        with open(self.paths.connection("claude", self.b.binding_key), "w") as fh:
            json.dump(rec.to_json(), fh)
        self.assertIsNone(cache.load_view(self.paths, {"claude": self.b}).providers["claude"].connection)

    def test_bindings_never_see_each_others_data(self):
        self.write_all()
        other = binding.make_binding("claude", "/profiles/x", "/cfg/OTHER.toml", host="h", uid=501)
        view = cache.load_view(self.paths, {"claude": other})
        self.assertIsNone(view.providers["claude"].connection)
        self.assertIsNone(view.providers["claude"].snapshot)

    def test_unusable_files_are_logged_once_per_version(self):
        path = self.paths.server_epoch
        with open(path, "w") as fh:
            fh.write("garbage")
        for _ in range(3):
            self.assertIsNone(cache.read_server_epoch(self.paths))
        with open(self.paths.log_file) as fh:
            self.assertEqual(sum("server_epoch.json" in line for line in fh), 1)

    def test_snapshot_without_windows_is_never_written(self):
        with self.assertRaises(ValueError):
            cache.write_snapshot(self.paths, snapshot(self.b, windows=[]), self.b.binding_key)

    def test_delete_snapshot(self):
        self.write_all()
        self.assertTrue(cache.delete_snapshot(self.paths, "claude", self.b.binding_key))
        self.assertFalse(cache.delete_snapshot(self.paths, "claude", self.b.binding_key))


class GlobalFileTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.paths.ensure_private_dirs()

    def test_rotate_server_epoch_creates_a_new_epoch_and_raises_the_clock(self):
        e1 = cache.rotate_server_epoch(self.paths, now=T0)
        e2 = cache.rotate_server_epoch(self.paths, now=T0 + 5)
        self.assertNotEqual(e1, e2)
        self.assertEqual(cache.read_server_epoch(self.paths), e2)
        self.assertEqual(cache.read_clock(self.paths), T0 + 5)

    def test_ensure_server_epoch_keeps_an_existing_epoch(self):
        e1 = cache.ensure_server_epoch_locked(self.paths, T0)
        self.assertEqual(cache.ensure_server_epoch_locked(self.paths, T0 + 1), e1)

    def test_clock_only_moves_forward_unless_reset(self):
        cache.note_wall_clock(self.paths, T0)
        cache.note_wall_clock(self.paths, T0 - 100)
        self.assertEqual(cache.read_clock(self.paths), T0)
        cache.note_wall_clock(self.paths, T0 - 100, reset=True)
        self.assertEqual(cache.read_clock(self.paths), T0 - 100)

    def test_fingerprint_sync_bumps_generations(self):
        cache.write_control(self.paths, t.default_control())
        gens = cache.sync_fingerprints(self.paths, {"claude": "fa", "codex": "fb"})
        self.assertEqual(gens, {"claude": 1, "codex": 1})
        self.assertEqual(cache.sync_fingerprints(self.paths, {"claude": "fa", "codex": "fb"}), gens)
        self.assertEqual(cache.sync_fingerprints(self.paths, {"claude": "fa2"})["claude"], 2)
        self.assertEqual(cache.read_control_dict(self.paths.state_dir)["providers"]["claude"]["settings_fingerprint"], "fa2")

    def test_lost_control_file_restarts_generations_above_any_old_value(self):
        cache.write_control(self.paths, t.default_control())
        cache.sync_fingerprints(self.paths, {"claude": "f0"})
        with open(self.paths.control, "w") as fh:
            fh.write("{broken")
        self.assertEqual(cache.read_control(self.paths), {"claude": 0, "codex": 0})  # readers fail closed
        gens = cache.sync_fingerprints(self.paths, {"claude": "f", "codex": "g"}, now=T0)
        self.assertGreater(gens["claude"], int(T0))

    def test_salt_is_created_once_privately_and_stays_stable(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.read_salt(self.paths))) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(results[0]), 32)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.salt).st_mode), 0o600)
        self.assertEqual(cache.read_salt(self.paths), results[0])

    def test_invalid_salt_is_replaced(self):
        with open(self.paths.salt, "w") as fh:
            fh.write("short")
        self.assertEqual(len(cache.read_salt(self.paths)), 32)


if __name__ == "__main__":
    unittest.main()
