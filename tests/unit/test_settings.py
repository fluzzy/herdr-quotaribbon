"""Settings: strict validation, defaults, last-good fallback, ON/OFF toggles, fingerprints."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest

from quotaribbon import cache, settings
from quotaribbon import constants as C
from quotaribbon import types as t
from tests.helpers.core_fakes import temp_paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VALID = {
    "schema_version": 1,
    "providers": {"claude": {"enabled": True}, "codex": {"enabled": False}},
    "display": {"mode": "used"},
    "refresh": {"interval_seconds": 120, "stale_after_seconds": 300, "hide_values_after_seconds": 900},
}


def _mut(fn):
    d = copy.deepcopy(VALID)
    fn(d)
    return d


INVALID_CASES = {
    "interval_too_small": _mut(lambda d: d["refresh"].update(interval_seconds=59)),
    "interval_too_large": _mut(lambda d: d["refresh"].update(interval_seconds=901, stale_after_seconds=1802, hide_values_after_seconds=4000)),
    "stale_less_than_twice_interval": _mut(lambda d: d["refresh"].update(interval_seconds=200, stale_after_seconds=350)),
    "only_hide_too_small_for_derived_stale": _mut(lambda d: d["refresh"].update(interval_seconds=600, hide_values_after_seconds=1000)
                                                  or d["refresh"].pop("stale_after_seconds")),
    "stale_is_null": _mut(lambda d: d["refresh"].update(stale_after_seconds=None)),
    "hide_is_null_with_stale_missing": _mut(lambda d: d["refresh"].update(hide_values_after_seconds=None)
                                            or d["refresh"].pop("stale_after_seconds")),
    "stale_is_float": _mut(lambda d: d["refresh"].pop("hide_values_after_seconds") and d["refresh"].update(stale_after_seconds=400.0)),
    "legacy_pair_as_floats": _mut(lambda d: d["refresh"].update(interval_seconds=600, stale_after_seconds=300.0,
                                                                hide_values_after_seconds=900.0)),
    "hide_is_string": _mut(lambda d: d["refresh"].update(hide_values_after_seconds="900")),
    "stale_not_below_hide": _mut(lambda d: d["refresh"].update(stale_after_seconds=900)),
    "hide_above_one_day": _mut(lambda d: d["refresh"].update(hide_values_after_seconds=86401)),
    "hide_above_one_hour": _mut(lambda d: d["refresh"].update(hide_values_after_seconds=3601)),
    "huge_integer": _mut(lambda d: d["refresh"].update(interval_seconds=10 ** 400)),
    "interval_is_float": _mut(lambda d: d["refresh"].update(interval_seconds=120.0)),
    "interval_is_bool": _mut(lambda d: d["refresh"].update(interval_seconds=True)),
    "interval_is_null": _mut(lambda d: d["refresh"].update(interval_seconds=None)),
    "display_mode_unknown": _mut(lambda d: d["display"].update(mode="left")),
    "display_mode_not_a_string": _mut(lambda d: d["display"].update(mode=["used"])),
    "unknown_top_level_key": _mut(lambda d: d.update(theme="dark")),
    "unknown_refresh_key": _mut(lambda d: d["refresh"].update(jitter=3)),
    "unknown_provider_key": _mut(lambda d: d["providers"]["claude"].update(colour="red")),
    "missing_provider": _mut(lambda d: d["providers"].pop("codex")),
    "extra_provider": _mut(lambda d: d["providers"].update(gemini={"enabled": True})),
    "enabled_not_bool": _mut(lambda d: d["providers"]["claude"].update(enabled="yes")),
    "enabled_missing": _mut(lambda d: d["providers"]["claude"].pop("enabled")),
    "relative_binary": _mut(lambda d: d["providers"]["claude"].update(binary="bin/claude")),
    "relative_profile_dir": _mut(lambda d: d["providers"]["codex"].update(profile_dir="~/.codex")),
    "schema_version_2": _mut(lambda d: d.update(schema_version=2)),
    "schema_version_string": _mut(lambda d: d.update(schema_version="1")),
    "display_null": _mut(lambda d: d.update(display=None)),
    "display_format_unknown": _mut(lambda d: d["display"].update(format="short")),
    "display_format_not_a_string": _mut(lambda d: d["display"].update(format=1)),
    "display_format_null": _mut(lambda d: d["display"].update(format=None)),
    "display_format_list": _mut(lambda d: d["display"].update(format=["compact"])),
    "show_reset_int_one": _mut(lambda d: d["display"].update(show_reset=1)),
    "show_reset_int_zero": _mut(lambda d: d["display"].update(show_reset=0)),
    "show_reset_string_true": _mut(lambda d: d["display"].update(show_reset="true")),
    "show_reset_null": _mut(lambda d: d["display"].update(show_reset=None)),
    "show_reset_list": _mut(lambda d: d["display"].update(show_reset=[True])),
    "window_labels_int_one": _mut(lambda d: d["display"].update(window_labels=1)),
    "window_labels_string_false": _mut(lambda d: d["display"].update(window_labels="false")),
    "window_labels_null": _mut(lambda d: d["display"].update(window_labels=None)),
    "providers_list": _mut(lambda d: d.update(providers=["claude", "codex"])),
    "not_an_object": [1, 2, 3],
}

VALID_CASES = {
    "full": VALID,
    "minimal": {"schema_version": 1, "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}},
    "absolute_overrides": _mut(lambda d: d["providers"]["claude"].update(binary="/usr/local/bin/claude", profile_dir="/tmp/p")),
    "explicit_null_override": _mut(lambda d: d["providers"]["codex"].update(binary=None)),
    "edge_timings": _mut(lambda d: d["refresh"].update(interval_seconds=60, stale_after_seconds=120, hide_values_after_seconds=3600)),
    "max_interval": _mut(lambda d: d["refresh"].update(interval_seconds=900, stale_after_seconds=1800, hide_values_after_seconds=3600)),
    "empty_display_and_refresh": _mut(lambda d: d.update(display={}, refresh={})),
    "display_mode_remaining": _mut(lambda d: d["display"].update(mode="remaining")),
    "display_format_full": _mut(lambda d: d["display"].update(format="full")),
    "display_format_compact": _mut(lambda d: d["display"].update(format="compact")),
    "display_format_only": _mut(lambda d: d.update(display={"format": "compact"})),
    "display_compact_remaining": _mut(lambda d: d["display"].update(mode="remaining", format="compact")),
    "show_reset_true": _mut(lambda d: d["display"].update(show_reset=True)),
    "show_reset_false": _mut(lambda d: d["display"].update(show_reset=False)),
    "show_reset_only": _mut(lambda d: d.update(display={"show_reset": False})),
    "show_reset_compact_remaining": _mut(lambda d: d["display"].update(mode="remaining", format="compact", show_reset=False)),
    "window_labels_true": _mut(lambda d: d["display"].update(window_labels=True)),
    "window_labels_false": _mut(lambda d: d["display"].update(window_labels=False)),
    "window_labels_only": _mut(lambda d: d.update(display={"window_labels": False})),
    "interval_only": _mut(lambda d: d.update(refresh={"interval_seconds": 600})),
    "legacy_pair_with_long_interval": _mut(lambda d: d["refresh"].update(interval_seconds=600)),
    "only_stale_given": _mut(lambda d: d.update(refresh={"interval_seconds": 600, "stale_after_seconds": 1500})),
    "only_hide_given": _mut(lambda d: d.update(refresh={"interval_seconds": 120, "hide_values_after_seconds": 1200})),
    "only_stale_given_short_interval": _mut(lambda d: d.update(refresh={"interval_seconds": 120, "stale_after_seconds": 800})),
    "only_stale_given_mid_interval": _mut(lambda d: d.update(refresh={"interval_seconds": 400, "stale_after_seconds": 800})),
    "only_stale_above_derived_hide": _mut(lambda d: d.update(refresh={"interval_seconds": 600, "stale_after_seconds": 2000})),
}


class ValidationTests(unittest.TestCase):
    def test_lean_validator_matches_the_dataclass_contract(self):
        for name, obj in {**VALID_CASES, **INVALID_CASES}.items():
            with self.subTest(name):
                try:
                    expected = t.Settings.from_json(copy.deepcopy(obj)).to_json()
                except ValueError:
                    expected = None
                try:
                    got = settings.validate_settings_obj(copy.deepcopy(obj))
                except ValueError:
                    got = None
                self.assertEqual(got, expected)

    def test_valid_cases_are_accepted_and_invalid_rejected(self):
        for name, obj in VALID_CASES.items():
            with self.subTest(name):
                self.assertIsInstance(settings.parse_settings(obj), t.Settings)
        for name, obj in INVALID_CASES.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    settings.parse_settings(obj)

    def test_error_messages_name_our_key_but_never_echo_user_content(self):
        with self.assertRaisesRegex(ValueError, "interval_seconds"):
            settings.parse_settings(INVALID_CASES["interval_too_small"])
        with self.assertRaises(ValueError) as cm:
            settings.parse_settings(INVALID_CASES["unknown_top_level_key"])
        self.assertIn("unknown key", str(cm.exception))
        self.assertNotIn("theme", str(cm.exception))
        with self.assertRaisesRegex(ValueError, "schema_version"):
            settings.parse_settings(INVALID_CASES["schema_version_2"])

    def test_display_format_defaults_to_full_and_errors_never_echo_user_text(self):
        self.assertEqual(settings.parse_settings(VALID).display_format, "full")
        self.assertEqual(settings.validate_settings_obj(VALID)["display"],
                         {"mode": "used", "format": "full", "show_reset": True, "window_labels": True})
        self.assertEqual(settings.parse_settings(VALID_CASES["display_format_compact"]).display_format, "compact")
        both = settings.parse_settings(VALID_CASES["display_compact_remaining"])
        self.assertEqual((both.display_mode, both.display_format), ("remaining", "compact"))
        for validate in (settings.validate_settings_obj, t.Settings.from_json):
            with self.subTest(validate.__qualname__):
                with self.assertRaises(ValueError) as cm:
                    validate(copy.deepcopy(INVALID_CASES["display_format_unknown"]))
                self.assertIn("settings.display.format", str(cm.exception))
                self.assertNotIn("short", str(cm.exception))

    def test_window_labels_defaults_on_is_bool_only_and_omitted_from_file_when_on(self):
        self.assertTrue(settings.parse_settings(VALID).display_window_labels)
        on = settings.parse_settings(VALID_CASES["window_labels_true"])
        self.assertEqual(settings.settings_to_file_json(on)["display"], {"mode": "used"})
        off = settings.parse_settings(VALID_CASES["window_labels_false"])
        self.assertFalse(off.display_window_labels)
        self.assertEqual(settings.settings_to_file_json(off)["display"], {"mode": "used", "window_labels": False})
        self.assertIs(settings.validate_settings_obj(VALID_CASES["window_labels_only"])["display"]["window_labels"], False)
        for s in (on, off):
            self.assertEqual(settings.parse_settings(settings.settings_to_file_json(s)), s)
        for validate in (settings.validate_settings_obj, t.Settings.from_json):
            with self.subTest(validate.__qualname__):
                with self.assertRaises(ValueError) as cm:
                    validate(copy.deepcopy(INVALID_CASES["window_labels_string_false"]))
                self.assertIn("settings.display.window_labels", str(cm.exception))
                self.assertNotIn("'false'", str(cm.exception))

    def test_file_json_omits_the_default_format_and_keeps_compact(self):
        full = settings.parse_settings(VALID_CASES["display_format_full"])
        self.assertEqual(settings.settings_to_file_json(full)["display"], {"mode": "used"})
        self.assertEqual(settings.settings_to_json(full)["display"], {"mode": "used", "format": "full", "show_reset": True, "window_labels": True})
        comp = settings.parse_settings(VALID_CASES["display_format_compact"])
        self.assertEqual(settings.settings_to_file_json(comp)["display"], {"mode": "used", "format": "compact"})
        for s in (full, comp):
            self.assertEqual(settings.parse_settings(settings.settings_to_file_json(s)), s)

    def test_show_reset_defaults_on_is_bool_only_and_omitted_from_file_when_on(self):
        self.assertTrue(settings.parse_settings(VALID).display_show_reset)
        self.assertTrue(settings.parse_settings(VALID_CASES["show_reset_true"]).display_show_reset)
        self.assertIs(settings.validate_settings_obj(VALID_CASES["show_reset_only"])["display"]["show_reset"], False)
        on = settings.parse_settings(VALID_CASES["show_reset_true"])
        self.assertEqual(settings.settings_to_file_json(on)["display"], {"mode": "used"})
        off = settings.parse_settings(VALID_CASES["show_reset_false"])
        self.assertFalse(off.display_show_reset)
        self.assertEqual(settings.settings_to_file_json(off)["display"], {"mode": "used", "show_reset": False})
        comp = settings.parse_settings(VALID_CASES["show_reset_compact_remaining"])
        self.assertEqual(settings.settings_to_file_json(comp)["display"],
                         {"mode": "remaining", "format": "compact", "show_reset": False})
        for s in (on, off, comp):
            self.assertEqual(settings.parse_settings(settings.settings_to_file_json(s)), s)
        for validate in (settings.validate_settings_obj, t.Settings.from_json):
            with self.subTest(validate.__qualname__):
                with self.assertRaises(ValueError) as cm:
                    validate(copy.deepcopy(INVALID_CASES["show_reset_string_true"]))
                self.assertIn("settings.display.show_reset", str(cm.exception))
                self.assertNotIn("'true'", str(cm.exception))

    def test_refresh_timings_are_derived_when_left_out(self):
        def timings(name):
            s = settings.parse_settings(VALID_CASES[name])
            return s.interval_seconds, s.stale_after_seconds, s.hide_values_after_seconds

        self.assertEqual(timings("interval_only"), (600, 1200, 1800))
        self.assertEqual(timings("legacy_pair_with_long_interval"), (600, 1200, 1800))
        self.assertEqual(timings("only_stale_given"), (600, 1500, 1800))
        self.assertEqual(timings("only_stale_given_short_interval"), (120, 800, 900))
        self.assertEqual(timings("only_stale_given_mid_interval"), (400, 800, 1200))
        self.assertEqual(timings("only_stale_above_derived_hide"), (600, 2000, 2600))
        self.assertEqual(timings("only_hide_given"), (120, 300, 1200))
        self.assertEqual(timings("full"), (120, 300, 900))
        self.assertEqual(settings.parse_settings(VALID_CASES["display_mode_remaining"]).display_mode, "remaining")

    def test_settings_to_json_round_trips(self):
        s = settings.parse_settings(VALID)
        self.assertEqual(settings.parse_settings(settings.settings_to_json(s)), s)

    def test_file_form_omits_derived_timings_and_keeps_custom_ones(self):
        default = settings.DEFAULT_SETTINGS
        self.assertEqual(settings.settings_to_file_json(default)["refresh"], {"interval_seconds": 120})
        derived = settings.parse_settings(VALID_CASES["interval_only"])
        self.assertEqual(settings.settings_to_file_json(derived)["refresh"], {"interval_seconds": 600})
        custom = settings.parse_settings(VALID_CASES["only_stale_given"])
        self.assertEqual(settings.settings_to_file_json(custom)["refresh"],
                         {"interval_seconds": 600, "stale_after_seconds": 1500, "hide_values_after_seconds": 1800})
        for name, obj in VALID_CASES.items():
            with self.subTest(name):
                s = settings.parse_settings(obj)
                self.assertEqual(settings.parse_settings(settings.settings_to_file_json(s)), s)
                self.assertEqual(settings.validate_settings_obj(settings.settings_to_file_json(s)), s.to_json())

    def test_default_settings_have_both_providers_on(self):
        d = settings.DEFAULT_SETTINGS
        self.assertTrue(d.providers["claude"].enabled and d.providers["codex"].enabled)
        self.assertEqual((d.interval_seconds, d.stale_after_seconds, d.hide_values_after_seconds), (120, 300, 900))

    def test_lazy_dataclasses_are_real_dataclasses(self):
        import dataclasses

        self.assertTrue(dataclasses.is_dataclass(settings.LoadedSettings))
        self.assertTrue(dataclasses.is_dataclass(settings.ToggleResult))
        self.assertTrue(dataclasses.is_dataclass(settings.IntervalResult))
        with self.assertRaises(AttributeError):
            settings.NoSuchName  # noqa: B018

    def test_fingerprint_changes_only_with_effective_provider_settings(self):
        a = settings.provider_fingerprint({"enabled": True})
        self.assertEqual(a, settings.provider_fingerprint(t.ProviderSettings(enabled=True)))
        self.assertNotEqual(a, settings.provider_fingerprint({"enabled": False}))
        self.assertNotEqual(a, settings.provider_fingerprint({"enabled": True, "binary": "/x/claude"}))
        self.assertNotEqual(a, settings.provider_fingerprint({"enabled": True, "profile_dir": "/x"}))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        os.makedirs(self.paths.config_dir)
        os.makedirs(self.paths.state_dir)

    def write(self, obj):
        with open(self.paths.settings, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_missing_settings_file_means_defaults(self):
        loaded = settings.load(self.paths, update_last_good=True)
        self.assertEqual((loaded.valid, loaded.source), (True, "default"))
        self.assertEqual(loaded.settings, t.Settings.default())
        self.assertEqual(settings.load_tick(self.paths.config_dir, self.paths.state_dir)[1], "default")

    def test_valid_file_is_adopted_as_last_good_only_when_requested(self):
        self.write(VALID)
        loaded = settings.load(self.paths, update_last_good=False)
        self.assertEqual((loaded.valid, loaded.source), (True, "file"))
        self.assertFalse(os.path.exists(self.paths.settings_last_good))
        settings.load(self.paths, update_last_good=True)
        with open(self.paths.settings_last_good) as fh:
            self.assertEqual(json.load(fh), settings.parse_settings(VALID).to_file_json())

    def test_last_good_is_stored_in_file_form_and_a_fallback_keeps_the_defaults(self):
        self.write(_mut(lambda d: d["display"].update(mode="remaining")))
        settings.load(self.paths, update_last_good=True)
        with open(self.paths.settings_last_good) as fh:
            stored = json.load(fh)
        self.assertEqual(stored["display"], {"mode": "remaining"})  # defaults left out, like settings.json
        self.assertEqual(stored["refresh"], {"interval_seconds": VALID["refresh"]["interval_seconds"]})
        self.write(INVALID_CASES["interval_too_small"])
        loaded = settings.load(self.paths, update_last_good=True)
        self.assertEqual(loaded.source, "last_good")
        self.assertTrue(loaded.settings.display_show_reset)
        self.assertTrue(loaded.settings.display_window_labels)
        self.assertEqual(loaded.settings.display_format, "full")
        tick_settings, source = settings.load_tick(self.paths.config_dir, self.paths.state_dir)
        self.assertEqual(source, "last_good")
        self.assertEqual(tick_settings, loaded.settings.to_json())
        self.assertEqual(tick_settings["display"],
                         {"mode": "remaining", "format": "full", "show_reset": True, "window_labels": True})

    def test_invalid_edits_keep_the_last_good_settings(self):
        self.write(VALID)
        settings.load(self.paths, update_last_good=True)
        for name, obj in INVALID_CASES.items():
            with self.subTest(name):
                self.write(obj)
                loaded = settings.load(self.paths, update_last_good=True)
                self.assertEqual((loaded.valid, loaded.source), (False, "last_good"))
                self.assertEqual(loaded.settings, settings.parse_settings(VALID))
                self.assertTrue(loaded.errors)
                tick_settings, source = settings.load_tick(self.paths.config_dir, self.paths.state_dir)
                self.assertEqual(source, "last_good")
                self.assertEqual(tick_settings, settings.parse_settings(VALID).to_json())

    def test_malformed_json_and_nan_keep_the_last_good_settings(self):
        self.write(VALID)
        settings.load(self.paths, update_last_good=True)
        for text in ("{not json", '{"schema_version": NaN}', '{"a": 1, "a": 2}', "\xff"):
            with self.subTest(text):
                self.write(text)
                loaded = settings.load(self.paths, update_last_good=True)
                self.assertEqual(loaded.source, "last_good")

    def test_invalid_first_settings_mean_no_settings_at_all(self):
        self.write(INVALID_CASES["interval_too_small"])
        loaded = settings.load(self.paths, update_last_good=True)
        self.assertEqual((loaded.settings, loaded.valid, loaded.source), (None, False, "none"))
        self.assertEqual(settings.load_tick(self.paths.config_dir, self.paths.state_dir), (None, "none"))


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)

    def read_settings(self):
        with open(self.paths.settings) as fh:
            return json.load(fh)

    def test_toggle_writes_settings_and_bumps_generation_only_on_change(self):
        first = settings.set_enabled(self.paths, "claude", False)
        self.assertTrue(first.changed)
        self.assertFalse(first.enabled)
        self.assertIsNone(first.error)
        self.assertFalse(self.read_settings()["providers"]["claude"]["enabled"])
        again = settings.set_enabled(self.paths, "claude", False)
        self.assertFalse(again.changed)
        self.assertEqual(again.generation, first.generation)
        on = settings.set_enabled(self.paths, "claude", True)
        self.assertTrue(on.changed)
        self.assertEqual(on.generation, first.generation + 1)
        self.assertEqual(cache.read_control(self.paths)["claude"], on.generation)

    def test_toggling_one_provider_never_bumps_the_other(self):
        settings.set_enabled(self.paths, "codex", True)
        codex_gen = cache.read_control(self.paths)["codex"]
        settings.set_enabled(self.paths, "claude", False)
        settings.set_enabled(self.paths, "claude", True)
        self.assertEqual(cache.read_control(self.paths)["codex"], codex_gen)

    def test_toggle_refuses_an_invalid_settings_file_and_leaves_it_untouched(self):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        bad = json.dumps(INVALID_CASES["stale_not_below_hide"]).encode()
        with open(self.paths.settings, "wb") as fh:
            fh.write(bad)
        res = settings.set_enabled(self.paths, "claude", False)
        self.assertFalse(res.changed)
        self.assertIsNotNone(res.error)
        with open(self.paths.settings, "rb") as fh:
            self.assertEqual(fh.read(), bad)
        self.assertFalse(os.path.exists(self.paths.control))

    def test_toggle_adopts_a_valid_file_as_last_good(self):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.settings, "w") as fh:
            json.dump(VALID, fh)
        settings.set_enabled(self.paths, "codex", True)
        with open(self.paths.settings_last_good) as fh:
            self.assertTrue(json.load(fh)["providers"]["codex"]["enabled"])

    def test_direct_user_edit_is_detected_by_fingerprint_and_bumps_that_generation(self):
        settings.set_enabled(self.paths, "claude", True)
        gens = cache.read_control(self.paths)
        data = self.read_settings()
        data["providers"]["codex"]["binary"] = "/opt/other/codex"
        with open(self.paths.settings, "w") as fh:
            json.dump(data, fh)
        res = settings.set_enabled(self.paths, "claude", True)
        self.assertFalse(res.changed)
        after = cache.read_control(self.paths)
        self.assertEqual(after["claude"], gens["claude"])
        self.assertEqual(after["codex"], gens["codex"] + 1)

    def test_off_value_survives_reloads(self):
        settings.set_enabled(self.paths, "claude", False)
        for _ in range(3):
            loaded = settings.load(self.paths, update_last_good=True)
            self.assertFalse(loaded.settings.providers["claude"].enabled)

    def test_concurrent_toggles_from_two_processes_lose_no_update(self):
        settings.set_enabled(self.paths, "claude", True)
        settings.set_enabled(self.paths, "codex", True)
        base = cache.read_control(self.paths)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from quotaribbon import settings\n"
            "from quotaribbon.paths import Paths\n"
            "p = Paths(config_dir=sys.argv[2], state_dir=sys.argv[3])\n"
            "prov, n = sys.argv[4], int(sys.argv[5])\n"
            "for i in range(n):\n"
            "    r = settings.set_enabled(p, prov, i % 2 == 1)\n"
            "    assert r.changed and r.error is None, r\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-I", "-c", script, REPO_ROOT, self.paths.config_dir, self.paths.state_dir, prov, str(n)])
            for prov, n in (("claude", 31), ("codex", 30))
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        data = self.read_settings()
        self.assertFalse(data["providers"]["claude"]["enabled"])
        self.assertTrue(data["providers"]["codex"]["enabled"])
        gens = cache.read_control(self.paths)
        self.assertEqual(gens["claude"], base["claude"] + 31)
        self.assertEqual(gens["codex"], base["codex"] + 30)


class ResolveRefreshTests(unittest.TestCase):
    def test_derived_refresh(self):
        self.assertEqual(C.derived_refresh(60), (300, 900))
        self.assertEqual(C.derived_refresh(120), (300, 900))
        self.assertEqual(C.derived_refresh(300), (600, 900))
        self.assertEqual(C.derived_refresh(600), (1200, 1800))
        self.assertEqual(C.derived_refresh(900), (1800, 2700))
        for interval in range(C.INTERVAL_MIN, C.INTERVAL_MAX + 1):
            stale, hide = C.derived_refresh(interval)
            self.assertTrue(stale >= 2 * interval and interval < stale < hide <= C.HIDE_AFTER_MAX, interval)

    def test_missing_values_are_derived(self):
        self.assertEqual(C.resolve_refresh(600), (1200, 1800))
        self.assertEqual(C.resolve_refresh(600, C.MISSING, C.MISSING), (1200, 1800))

    def test_old_default_pair_counts_as_absent(self):
        self.assertEqual(C.resolve_refresh(600, 300, 900), (1200, 1800))
        self.assertEqual(C.resolve_refresh(120, 300, 900), (300, 900))
        self.assertEqual(C.resolve_refresh(600, 300.0, 900.0), (300.0, 900.0))  # wrong type: left for the validator

    def test_only_one_value_given(self):
        self.assertEqual(C.resolve_refresh(600, 1500), (1500, 1800))
        self.assertEqual(C.resolve_refresh(120, 600), (600, 900))
        self.assertEqual(C.resolve_refresh(900, 3500), (3500, 3600))  # capped; the validator rejects it
        self.assertEqual(C.resolve_refresh(120, C.MISSING, 1200), (300, 1200))

    def test_only_stale_given_keeps_the_derived_hide_when_it_fits(self):
        self.assertEqual(C.resolve_refresh(120, 800), (800, 900))
        self.assertEqual(C.resolve_refresh(400, 800), (800, 1200))  # = derived hide (3 x interval)
        self.assertEqual(C.resolve_refresh(400, 800)[1], C.derived_refresh(400)[1])
        # Derived hide not above stale: fall back to stale + interval, capped at 3600.
        self.assertEqual(C.resolve_refresh(600, 1800), (1800, 2400))
        self.assertEqual(C.resolve_refresh(600, 2000), (2000, 2600))
        self.assertEqual(C.resolve_refresh(900, 3000), (3000, 3600))
        self.assertEqual(C.resolve_refresh(600, C.MISSING, 1000), (1200, 1000))  # the validator rejects it

    def test_explicit_null_and_wrong_types_pass_through(self):
        self.assertEqual(C.resolve_refresh(120, None, None), (None, None))
        self.assertEqual(C.resolve_refresh(120, None), (None, 900))
        self.assertEqual(C.resolve_refresh(120, C.MISSING, None), (300, None))
        self.assertEqual(C.resolve_refresh(120, "300", 900), ("300", 900))
        self.assertEqual(C.resolve_refresh(120, True, 900), (True, 900))

    def test_invalid_interval_falls_back_to_the_defaults(self):
        for interval in (None, 120.0, True, 0, 10 ** 400):
            with self.subTest(interval):
                self.assertEqual(C.resolve_refresh(interval), (C.DEFAULT_STALE_AFTER, C.DEFAULT_HIDE_AFTER))
                self.assertEqual(C.resolve_refresh(interval, 1000, None), (1000, None))


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)

    def read_settings(self):
        with open(self.paths.settings) as fh:
            return json.load(fh)

    def write(self, obj):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.settings, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_missing_file_gets_a_minimal_file(self):
        res = settings.set_interval(self.paths, 300)
        self.assertEqual((res.changed, res.interval_seconds, res.stale_after_seconds, res.hide_values_after_seconds,
                          res.timings_adjusted, res.error), (True, 300, 600, 900, False, None))
        data = self.read_settings()
        self.assertEqual(data["refresh"], {"interval_seconds": 300})
        self.assertEqual(data["display"], {"mode": "used"})
        self.assertTrue(data["providers"]["claude"]["enabled"] and data["providers"]["codex"]["enabled"])
        with open(self.paths.settings_last_good) as fh:
            self.assertEqual(json.load(fh)["refresh"], {"interval_seconds": 300})  # file form: derived timings left out

    def test_same_interval_is_unchanged_and_keeps_other_settings(self):
        self.write(_mut(lambda d: d["display"].update(mode="remaining")))
        res = settings.set_interval(self.paths, 120)
        self.assertFalse(res.changed)
        res = settings.set_interval(self.paths, 600)
        self.assertTrue(res.changed)
        data = self.read_settings()
        self.assertEqual(data["display"], {"mode": "remaining"})
        self.assertFalse(data["providers"]["codex"]["enabled"])
        self.assertEqual(data["refresh"], {"interval_seconds": 600})

    def test_compact_format_survives_an_interval_change(self):
        self.write(_mut(lambda d: d["display"].update(format="compact")))
        settings.set_interval(self.paths, 600)
        self.assertEqual(self.read_settings()["display"], {"mode": "used", "format": "compact"})

    def test_show_reset_survives_an_interval_change(self):
        self.write(_mut(lambda d: d["display"].update(show_reset=False)))
        settings.set_interval(self.paths, 600)
        self.assertEqual(self.read_settings()["display"], {"mode": "used", "show_reset": False})

    def test_custom_timings_are_kept_when_they_still_fit(self):
        self.write(_mut(lambda d: d["refresh"].update(stale_after_seconds=1300, hide_values_after_seconds=3000)))
        res = settings.set_interval(self.paths, 600)
        self.assertEqual((res.changed, res.stale_after_seconds, res.hide_values_after_seconds, res.timings_adjusted),
                         (True, 1300, 3000, False))
        self.assertEqual(self.read_settings()["refresh"],
                         {"interval_seconds": 600, "stale_after_seconds": 1300, "hide_values_after_seconds": 3000})

    def test_custom_timings_that_no_longer_fit_are_derived_again(self):
        self.write(_mut(lambda d: d["refresh"].update(stale_after_seconds=400, hide_values_after_seconds=1000)))
        res = settings.set_interval(self.paths, 600)
        self.assertEqual((res.changed, res.stale_after_seconds, res.hide_values_after_seconds, res.timings_adjusted),
                         (True, 1200, 1800, True))
        self.assertEqual(self.read_settings()["refresh"], {"interval_seconds": 600})

    def test_derived_timings_follow_the_interval_without_being_reported_as_adjusted(self):
        settings.set_interval(self.paths, 600)
        res = settings.set_interval(self.paths, 60)
        self.assertEqual((res.stale_after_seconds, res.hide_values_after_seconds, res.timings_adjusted), (300, 900, False))
        self.assertEqual(self.read_settings()["refresh"], {"interval_seconds": 60})

    def test_invalid_settings_file_is_refused_untouched(self):
        for name in ("stale_not_below_hide", "display_mode_unknown"):
            with self.subTest(name):
                bad = json.dumps(INVALID_CASES[name])
                self.write(bad)
                res = settings.set_interval(self.paths, 300)
                self.assertFalse(res.changed)
                self.assertIn("invalid", res.error)
                with open(self.paths.settings) as fh:
                    self.assertEqual(fh.read(), bad)
        self.write("{broken")
        res = settings.set_interval(self.paths, 300)
        self.assertIsNotNone(res.error)
        with open(self.paths.settings) as fh:
            self.assertEqual(fh.read(), "{broken")

    def test_out_of_range_or_non_integer_interval_raises(self):
        for bad in (59, 901, 300.0, True, "300"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    settings.set_interval(self.paths, bad)
        self.assertFalse(os.path.exists(self.paths.settings))


if __name__ == "__main__":
    unittest.main()
