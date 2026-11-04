"""Setup refuses configs whose tab-bar fields Herdr would reject, and accepts valid ones."""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from unittest import mock

from quotaribbon import setup_config as sc

HOME = os.path.join(tempfile.gettempdir(), "qr-home")
TABBAR = os.path.join(HOME, ".config/herdr/plugins/config/fluzzy.herdr-quotaribbon/tabbar.py")
PYTHON = os.path.join(HOME, ".pyenv/versions/3.12.13/bin/python3")
SECRET = "sk-ant-NOT-A-REAL-SECRET-123456"

INVALID = {
    "hide_flag_as_string": '[ui]\nhide_tab_bar_when_single_tab = "true"\n',
    "hide_flag_as_integer": "[ui]\nhide_tab_bar_when_single_tab = 1\n",
    "element_is_an_integer": "[ui]\ntab_bar_right = [123]\n",
    "element_is_a_string": '[ui]\ntab_bar_right = ["hostname"]\n',
    "element_with_unknown_type": '[ui]\ntab_bar_right = [{ type = "battery" }]\n',
    "element_without_type": '[ui]\ntab_bar_right = [{ text = "x" }]\n',
    "element_with_non_string_type": "[ui]\ntab_bar_right = [{ type = 3 }]\n",
    "element_with_unknown_key": '[ui]\ntab_bar_right = [{ type = "hostname", colour = "red" }]\n',
    "key_of_another_type": '[ui]\ntab_bar_right = [{ type = "zoom", text = "Z" }]\n',
    "text_without_text": '[ui]\ntab_bar_right = [{ type = "text" }]\n',
    "text_with_integer_text": '[ui]\ntab_bar_right = [{ type = "text", text = 5 }]\n',
    "datetime_with_integer_format": '[ui]\ntab_bar_right = [{ type = "datetime", format = 5 }]\n',
    "datetime_with_offset_directive": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%H:%M %z" }]\n',
    "datetime_with_timestamp_directive": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%s" }]\n',
    "datetime_with_unknown_directive": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%H %Q" }]\n',
    "datetime_with_colon_modifier": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%:Y" }]\n',
    "datetime_with_fraction_variant": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%S%.3f" }]\n',
    "datetime_with_double_padding": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%--d" }]\n',
    "datetime_with_dangling_percent": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%H %" }]\n',
    "command_without_command": '[ui]\ntab_bar_right = [{ type = "command", interval_seconds = 5 }]\n',
    "command_with_string_interval": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                     'interval_seconds = "5" }]\n'),
    "command_with_float_timeout": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                   "timeout_seconds = 2.0 }]\n"),
    "command_with_boolean_interval": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                      "interval_seconds = true }]\n"),
    "command_with_zero_interval": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                   "interval_seconds = 0 }]\n"),
    "command_with_too_long_timeout": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                      "timeout_seconds = 3601 }]\n"),
    "invalid_element_after_valid_ones": ('[ui]\ntab_bar_right = [\n  { type = "hostname" },\n'
                                         '  { type = "zoom", extra = 1 },\n]\n'),
    "position_left": '[ui]\ntab_bar_position = "left"\n',
    "position_not_a_string": "[ui]\ntab_bar_position = 1\n",
    "separator_integer": "[ui]\ntab_bar_right_separator = 1\n",
    "separator_array": '[ui]\ntab_bar_right_separator = [" "]\n',
    "invalid_value_in_user_shape": ('onboarding = false\n\n[theme]\nname = "catppuccin-mocha"\n\n'
                                    '[ui]\ntab_bar_position = "middle"\n\n[ui.sound]\nenabled = false\n'),
}

VALID = {
    "user_shape": 'onboarding = false\n\n[theme]\nname = "catppuccin-mocha"\n\n[ui.sound]\nenabled = false\n',
    "every_documented_element": (
        "[ui]\n"
        "tab_bar_right = [\n"
        '  { type = "zoom" },\n'
        '  { type = "hostname" },\n'
        '  { type = "datetime", format = "%H:%M" },\n'
        '  { type = "text", text = "prod" },\n'
        '  { type = "command", command = "~/.config/herdr/status.sh", interval_seconds = 5, timeout_seconds = 2 },\n'
        "]\n"
        'tab_bar_right_separator = " · "\n'
    ),
    "datetime_without_format": '[ui]\ntab_bar_right = [{ type = "datetime" }]\n',
    "datetime_with_literal_percent": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%%s %Y-%m-%d" }]\n',
    "datetime_with_padding_modifiers": '[ui]\ntab_bar_right = [{ type = "datetime", format = "%a %-d %b %_H:%M" }]\n',
    "command_without_interval_or_timeout": '[ui]\ntab_bar_right = [{ type = "command", command = "x" }]\n',
    "command_at_range_limits": ('[ui]\ntab_bar_right = [{ type = "command", command = "x", '
                                "interval_seconds = 31536000, timeout_seconds = 3600 }]\n"),
    "flags_and_position": ('[ui]\nhide_tab_bar_when_single_tab = true\ntab_bar_position = "bottom"\n'
                           'tab_bar_right_separator = ""\n'),
    "top_position": '[ui]\ntab_bar_position = "top"\nhide_tab_bar_when_single_tab = false\n',
    "hostname_element_with_other_ui_keys": ('[ui]\nsidebar_width = 30\ntab_bar_right = [{ type = "hostname" }]\n'
                                            "\n[ui.sound]\nenabled = false\n"),
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.backups = os.path.join(self.tmp, "backups")
        self.cfg = os.path.join(self.tmp, "config.toml")
        self.entry = sc.build_entry(PYTHON, TABBAR)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        with open(self.cfg, "w", newline="") as fh:
            fh.write(text)

    def read(self) -> str:
        with open(self.cfg, newline="") as fh:
            return fh.read()


class InvalidTabBarFieldsAreRefusedTests(Base):
    def test_each_herdr_invalid_value_is_refused_with_nothing_written(self):
        for name, text in INVALID.items():
            with self.subTest(name):
                self.write(text)
                plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
                self.assertIsNotNone(plan.refusal, name)
                self.assertIn("Herdr would reject", plan.refusal)
                self.assertIsNone(plan.after_text)
                self.assertIn(sc.entry_text(self.entry), plan.manual_snippet)
                with self.assertRaises(sc.SetupRefused):
                    sc.apply_plan(plan, self.backups)
                self.assertEqual(self.read(), text)
                self.assertFalse(os.path.exists(self.backups))

    def test_invalid_config_already_holding_our_entry_is_refused_rather_than_reported_present(self):
        text = '[ui]\nhide_tab_bar_when_single_tab = "true"\ntab_bar_right = [' + sc.entry_text(self.entry) + "]\n"
        self.write(text)
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertFalse(plan.already_present)
        self.assertIsNotNone(plan.refusal)
        self.assertEqual(self.read(), text)

    def test_refusal_reason_never_echoes_config_values(self):
        self.write(f'[ui]\ntab_bar_right = [{{ type = "command", command = "x", {SECRET.replace("-", "_")} = 1 }}]\n')
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNotNone(plan.refusal)
        self.assertNotIn(SECRET.replace("-", "_"), plan.refusal)
        self.write(f'[ui]\ntab_bar_right = [{{ type = "{SECRET}" }}]\n')
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertNotIn(SECRET, plan.refusal)

    def test_display_values_stay_enum_validated_on_refusal(self):
        self.write('[ui]\nhide_tab_bar_when_single_tab = "true"\ntab_bar_position = "left"\n'
                   'tab_bar_right = [{ type = "battery" }, { type = "hostname" }]\n')
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNotNone(plan.refusal)
        self.assertEqual(plan.ui_info["hide_tab_bar_when_single_tab"], "invalid")
        self.assertEqual(plan.ui_info["tab_bar_position"], "invalid")
        self.assertEqual([i["type"] for i in plan.ui_info["tab_bar_right"]], ["other", "hostname"])

    def test_invalid_candidate_is_refused_before_apply(self):
        # The candidate check runs on the edited text, independently of the input check.
        self.write("[ui]\n")
        with mock.patch.object(sc, "_ui_schema_problem", side_effect=[None, "ui.tab_bar_right is not an array"]):
            plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNotNone(plan.refusal)
        self.assertIn("edited file", plan.refusal)
        self.assertEqual(self.read(), "[ui]\n")

    def test_apply_rechecks_the_candidate(self):
        self.write("[ui]\n")
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNone(plan.refusal, plan.refusal)
        bad = dict(self.entry, interval_seconds=0)
        plan.after_text = plan.after_text.replace(sc.entry_text(self.entry), sc.entry_text(bad))
        plan.entry = bad
        with self.assertRaises(sc.SetupRefused) as cm:
            sc.apply_plan(plan, self.backups)
        self.assertIn("edited file", str(cm.exception))
        self.assertEqual(self.read(), "[ui]\n")

    def test_our_own_entry_passes_the_check(self):
        self.assertIsNone(sc.herdr_ui_problem({"ui": {"tab_bar_right": [self.entry]}}))


class ValidTabBarFieldsAreAcceptedTests(Base):
    def test_valid_configs_get_exactly_one_entry_appended(self):
        for name, text in VALID.items():
            with self.subTest(name):
                self.write(text)
                plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
                self.assertIsNone(plan.refusal, plan.refusal)
                sc.apply_plan(plan, self.backups)
                expected = tomllib.loads(text)
                expected.setdefault("ui", {}).setdefault("tab_bar_right", []).append(self.entry)
                self.assertEqual(tomllib.loads(self.read()), expected)
                self.assertIsNone(sc.herdr_ui_problem(tomllib.loads(self.read())))

    def test_missing_config_is_still_created(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNone(plan.refusal, plan.refusal)
        sc.apply_plan(plan, self.backups)
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": [self.entry]}})


if __name__ == "__main__":
    unittest.main()
