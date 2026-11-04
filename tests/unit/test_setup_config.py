"""Herdr config.toml editing: supported shapes, refusals, safety checks and detach."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from quotaribbon import setup_config as sc

HOME = os.path.join(tempfile.gettempdir(), "qr-home")
TABBAR = os.path.join(HOME, ".config/herdr/plugins/config/fluzzy.herdr-quotaribbon/tabbar.py")
PYTHON = os.path.join(HOME, ".pyenv/versions/3.12.13/bin/python3")

USER_SHAPE = 'onboarding = false\n\n[theme]\nname = "catppuccin-mocha"\n\n[ui.sound]\nenabled = false\n'

SUPPORTED = {
    "no_ui_key": ("b", '# my herdr config\nonboarding = false  # keep me\n\n[theme]\nname = "x"\n'),
    "ui_without_array": ("c", '# top\n[ui]\nhide_tab_bar_when_single_tab = false # keep\n\n[keys]\nprefix = "ctrl-b"\n'),
    "ui_header_at_eof": ("c", "[ui]"),
    "multiline_array": ("d", '[ui]\ntab_bar_right = [\n  { type = "hostname" },  # host\n  { type = "datetime" }\n]\n'),
    "multiline_trailing_comma": ("d", '[ui]\ntab_bar_right = [\n    { type = "hostname" },\n]\n[theme]\nname = "x"\n'),
    "single_line_array": ("d", '[ui]\ntab_bar_right = [{ type = "hostname" }, { type = "text", text = "a]b,#c" }]\nfoo = 1\n'),
    "single_line_trailing_comma": ("d", '[ui]\ntab_bar_right = [{ type = "hostname" },]\n'),
    "empty_array": ("d", "[ui]\ntab_bar_right = []\n"),
    "empty_multiline_array": ("d", "[ui]\ntab_bar_right = [\n]\n"),
    "crlf_line_endings": ("d", '[ui]\r\ntab_bar_right = [\r\n  { type = "hostname" },\r\n]\r\n'),
    "tricky_strings": ("d", 'x = """\n[ui]\ntab_bar_right = 1\n"""\n[ui]\ntab_bar_right = [ { type = "text", text = \'\'\'a]\'\'\' } ]\n'),
    "user_shape_only_ui_subtables": ("e", USER_SHAPE),
    "subtable_without_final_newline": ("e", "[ui.sound]\nenabled = false"),
}

UNSUPPORTED = {
    "root_dotted_ui_key": 'ui.tab_bar_position = "top"\n',
    "root_inline_ui_table": 'ui = { tab_bar_position = "top" }\n',
    "array_of_tables": '[[ui.tab_bar_right]]\ntype = "hostname"\n',
    "quoted_ui_header": '["ui"]\nfoo = 1\n',
    "tab_bar_right_not_array": '[ui]\ntab_bar_right = "x"\n',
    "tab_bar_right_subtable": '[ui]\n[ui.tab_bar_right]\nx = 1\n',
    "does_not_parse": "[ui\n",
    "duplicate_table": "[ui]\na = 1\n[ui]\nb = 2\n",
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

    def write(self, text: str, mode: int = 0o644) -> None:
        with open(self.cfg, "w", newline="") as fh:
            fh.write(text)
        os.chmod(self.cfg, mode)

    def read(self) -> str | None:
        if not os.path.exists(self.cfg):
            return None
        with open(self.cfg, newline="") as fh:
            return fh.read()

    def setup_once(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNone(plan.refusal, plan.refusal)
        sc.apply_plan(plan, self.backups)
        return plan, sc.setup_record(plan)


class EntryTests(unittest.TestCase):
    def test_entry_round_trips_paths_with_spaces_quotes_backslashes_and_korean(self):
        py = "/opt/my python/bin/python3"
        tab = os.path.join(HOME, "설정 dir/it's \"quoted\" \\ back/tabbar.py")
        entry = sc.build_entry(py, tab)
        doc = tomllib.loads("x = [" + sc.entry_text(entry) + "]\n")
        self.assertEqual(doc["x"][0], entry)
        self.assertEqual(shlex.split(entry["command"]),
                         ["/bin/sh", os.path.join(os.path.dirname(tab), "tabbar.sh"), "2>/dev/null", "||", ":"])
        self.assertEqual(entry["interval_seconds"], 5)
        self.assertEqual(entry["timeout_seconds"], 2)
        script = sc.wrapper_script(py, tab)
        self.assertIn("qr_python=" + shlex.quote(py) + "\n", script)
        self.assertIn("qr_bootstrap=" + shlex.quote(tab) + "\n", script)

    def test_command_runs_under_sh_lc_without_injection(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "pwned")
            tab = os.path.join(d, f"한글 it's \"q\" $(touch {marker}) `id`", "tabbar.py")
            os.makedirs(os.path.dirname(tab))
            with open(tab, "w") as fh:
                fh.write("import sys; print('ran:' + sys.argv[0])\n")
            import sys as _sys

            python = os.path.realpath(_sys.executable)
            entry = sc.build_entry(python, tab)
            launcher = sc.install_wrapper(python, tab)
            self.assertEqual(stat.S_IMODE(os.stat(launcher).st_mode), 0o600)
            parsed = tomllib.loads("x = [" + sc.entry_text(entry) + "]\n")["x"][0]["command"]
            r = subprocess.run(["/bin/sh", "-lc", parsed], capture_output=True, text=True, timeout=30, cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "ran:" + tab)
            self.assertFalse(os.path.exists(marker))

    def test_basic_string_escapes_control_characters(self):
        s = "a\tb\nc\x01d\x7fe\"f\\g"
        self.assertEqual(tomllib.loads("v = " + sc.toml_basic_string(s))["v"], s)


class SetupShapeTests(Base):
    def test_missing_file_is_created_with_only_our_entry(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertEqual(plan.shape, "a")
        sc.apply_plan(plan, self.backups)
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": [self.entry]}})
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)

    def test_each_supported_shape_appends_exactly_one_entry_and_keeps_other_bytes(self):
        for name, (shape, text) in SUPPORTED.items():
            with self.subTest(name):
                self.write(text)
                before_doc = tomllib.loads(text)
                plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
                self.assertIsNone(plan.refusal, plan.refusal)
                self.assertEqual(plan.shape, shape)
                self.assertIn("+", plan.diff)
                sc.apply_plan(plan, self.backups)
                after = self.read()
                after_doc = tomllib.loads(after)
                expected = before_doc
                expected.setdefault("ui", {}).setdefault("tab_bar_right", []).append(self.entry)
                self.assertEqual(after_doc, expected)
                # Everything outside the inserted text is byte-identical.
                self.assertEqual(sc._inverse_edits(after, plan.edits), text)

    def test_setup_twice_leaves_one_entry(self):
        for name, (_shape, text) in SUPPORTED.items():
            with self.subTest(name):
                self.write(text)
                self.setup_once()
                once = self.read()
                again = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
                self.assertTrue(again.already_present)
                res = sc.apply_plan(again, self.backups)
                self.assertFalse(res.written)
                self.assertEqual(self.read(), once)
                ours = [e for e in tomllib.loads(once)["ui"]["tab_bar_right"] if e == self.entry]
                self.assertEqual(len(ours), 1)

    def test_user_shape_keeps_sound_table_and_gets_a_new_ui_table(self):
        self.write(USER_SHAPE)
        plan, _ = self.setup_once()
        self.assertEqual(plan.shape, "e")
        doc = tomllib.loads(self.read())
        self.assertEqual(doc["ui"]["sound"], {"enabled": False})
        self.assertEqual(doc["theme"], {"name": "catppuccin-mocha"})
        self.assertTrue(self.read().startswith(USER_SHAPE))

    def test_unsupported_shapes_are_refused_untouched_with_manual_snippet(self):
        for name, text in UNSUPPORTED.items():
            with self.subTest(name):
                self.write(text)
                plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
                self.assertIsNotNone(plan.refusal)
                self.assertIn(sc.entry_text(self.entry), plan.manual_snippet)
                with self.assertRaises(sc.SetupRefused):
                    sc.apply_plan(plan, self.backups)
                self.assertEqual(self.read(), text)

    def test_existing_edited_quotaribbon_entry_is_refused(self):
        other = dict(self.entry, command=sc.legacy_command("/usr/local/bin/python3.11", TABBAR))
        self.write("[ui]\ntab_bar_right = [" + sc.entry_text(other) + "]\n")
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIn("detach", plan.refusal)


class SetupSafetyTests(Base):
    def test_file_changed_between_preview_and_apply_is_refused_and_untouched(self):
        self.write("[ui]\nhide_tab_bar_when_single_tab = false\n")
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        concurrent = "[ui]\nhide_tab_bar_when_single_tab = true\n# edited elsewhere\n"
        self.write(concurrent)
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assertEqual(self.read(), concurrent)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".qr-tmp")], [])

    def test_file_changed_right_before_rename_is_refused(self):
        self.write("[ui]\n")
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        real_hash = sc._current_hash
        calls = {"n": 0}

        def racing_hash(path):
            calls["n"] += 1
            if calls["n"] == 2:  # the re-check immediately before os.replace
                with open(self.cfg, "a") as fh:
                    fh.write("# racing editor\n")
            return real_hash(path)

        with mock.patch.object(sc, "_current_hash", racing_hash):
            with self.assertRaises(sc.ConcurrentEdit):
                sc.apply_plan(plan, self.backups)
        self.assertEqual(self.read(), "[ui]\n# racing editor\n")

    def test_file_appearing_after_preview_of_missing_file_is_refused(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.write("x = 1\n")
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assertEqual(self.read(), "x = 1\n")

    def test_file_owned_by_someone_else_is_refused(self):
        self.write("[ui]\n")
        with mock.patch.object(sc.os, "geteuid", return_value=os.geteuid() + 1):
            plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIn("not owned", plan.refusal)

    def test_backup_mode_and_symlink_are_preserved(self):
        real = os.path.join(self.tmp, "dotfiles", "herdr.toml")
        os.makedirs(os.path.dirname(real))
        with open(real, "w") as fh:
            fh.write("[ui]\n")
        os.chmod(real, 0o640)
        os.symlink(real, self.cfg)
        plan, _ = self.setup_once()
        self.assertTrue(os.path.islink(self.cfg))
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o640)
        backups = os.listdir(self.backups)
        self.assertEqual(len(backups), 1)
        bpath = os.path.join(self.backups, backups[0])
        self.assertEqual(stat.S_IMODE(os.stat(bpath).st_mode), 0o600)
        with open(bpath) as fh:
            self.assertEqual(fh.read(), "[ui]\n")

    def test_tab_bar_layout_settings_are_reported_and_never_changed(self):
        text = '[ui]\nhide_tab_bar_when_single_tab = true\ntab_bar_position = "bottom"\n'
        self.write(text)
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertTrue(plan.ui_info["hide_tab_bar_when_single_tab"])
        self.assertEqual(plan.ui_info["tab_bar_position"], "bottom")
        self.assertEqual(len(plan.ui_info["warnings"]), 2)
        sc.apply_plan(plan, self.backups)
        ui = tomllib.loads(self.read())["ui"]
        self.assertIs(ui["hide_tab_bar_when_single_tab"], True)
        self.assertEqual(ui["tab_bar_position"], "bottom")

    def test_inspect_ui_marks_our_entry_without_echoing_commands(self):
        doc = {"ui": {"tab_bar_right": [{"type": "command", "command": "secret-cmd --token x"}, self.entry]}}
        info = sc.inspect_ui(doc, TABBAR)
        self.assertEqual(info["tab_bar_right"], [{"type": "command", "quotaribbon": False},
                                                 {"type": "command", "quotaribbon": True}])
        self.assertNotIn("secret-cmd", repr(info))


class PreviewPrivacyTests(unittest.TestCase):
    """Previews show our own lines only; other config content never reaches output."""

    SECRET = "sk-ant-NOT-A-REAL-SECRET-123456"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.toml")
        self.entry = sc.build_entry("/usr/bin/python3.12", "/cfg/tabbar.py")

    def write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)

    def test_setup_diff_masks_existing_commands_on_the_same_line(self):
        self.write(f'[ui]\ntab_bar_right = [{{ type = "command", command = "curl -H {self.SECRET}" }}]\n'
                   'theme = "dark"\n')
        plan = sc.plan_setup(self.path, self.entry)
        self.assertIsNone(plan.refusal, plan.reason)
        self.assertNotIn(self.SECRET, plan.diff)
        self.assertNotIn("theme", plan.diff)
        self.assertIn(sc.entry_text(self.entry), plan.diff)
        self.assertIn(sc.MASK, plan.diff)

    def test_setup_diff_has_no_context_lines(self):
        self.write(f'token = "{self.SECRET}"\n[ui]\nhide_tab_bar_when_single_tab = true\n')
        plan = sc.plan_setup(self.path, self.entry)
        self.assertIsNone(plan.refusal, plan.reason)
        self.assertNotIn(self.SECRET, plan.diff)
        self.assertNotIn("hide_tab_bar_when_single_tab", plan.diff)
        body = [ln for ln in plan.diff.splitlines() if not ln.startswith(("---", "+++", "@@"))]
        self.assertTrue(body and all(ln.startswith("+") for ln in body), plan.diff)


class DetachTests(Base):
    def test_a_record_naming_an_arbitrary_command_never_removes_it(self):
        foreign = {"type": "command", "command": "/usr/local/bin/my-status --verbose", "interval_seconds": 5,
                   "timeout_seconds": 2}
        text = "[ui]\ntab_bar_right = [" + sc.entry_text(foreign) + "]\n"
        self.write(text)
        etext = sc.entry_text(foreign)
        forged = {"schema_version": 1, "config_path": self.cfg, "real_path": os.path.realpath(self.cfg),
                  "entry": foreign, "entry_text": etext, "edits": [], "created_key": False,
                  "created_ui_header": False, "created_file": False, "before_hash": None, "shape": None,
                  "after_hash": "0" * 64, "added_at": 0}
        self.assertIsNotNone(sc.validate_record(forged, self.cfg))  # self-consistent on its own
        self.assertIsNone(sc.validate_record(forged, self.cfg, TABBAR))  # but not a QuotaRibbon command
        dp = sc.plan_detach(self.cfg, forged, tabbar_path=TABBAR)
        self.assertTrue(dp.nothing_to_do, dp.reason)
        self.assertEqual(self.read(), text)
        dp = sc.plan_detach(self.cfg, forged)  # no trusted bootstrap path: the record is ignored
        self.assertTrue(dp.nothing_to_do, dp.reason)

    def test_revert_uses_the_trusted_bootstrap_path(self):
        text = '[ui]\nhide_tab_bar_when_single_tab = true\n'
        self.write(text)
        _plan, rec = self.setup_once()
        sc.revert_setup(self.cfg, rec, self.backups, TABBAR)
        self.assertEqual(self.read(), text)

    def test_detach_of_unmodified_entry_restores_original_bytes(self):
        for name, (_shape, text) in SUPPORTED.items():
            with self.subTest(name):
                self.write(text)
                _plan, rec = self.setup_once()
                dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
                self.assertIsNone(dp.refusal)
                self.assertTrue(dp.exact_restore)
                sc.apply_detach(dp, self.backups)
                self.assertEqual(self.read(), text)

    def test_detach_removes_the_file_setup_created(self):
        _plan, rec = self.setup_once()
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.delete_file)
        res = sc.apply_detach(dp, self.backups)
        self.assertTrue(res.deleted)
        self.assertFalse(os.path.exists(self.cfg))
        self.assertEqual(len(os.listdir(self.backups)), 1)

    def test_detach_after_unrelated_user_edits_removes_only_our_entry(self):
        for name, (_shape, text) in SUPPORTED.items():
            with self.subTest(name):
                self.write(text)
                _plan, rec = self.setup_once()
                with open(self.cfg, "a") as fh:
                    fh.write("\n[extra]\nkept = true\n")
                user_doc = tomllib.loads(self.read())
                dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
                self.assertIsNone(dp.refusal, dp.refusal)
                self.assertFalse(dp.exact_restore)
                sc.apply_detach(dp, self.backups)
                got = tomllib.loads(self.read())
                user_doc["ui"]["tab_bar_right"].remove(self.entry)
                if rec["created_key"] and not user_doc["ui"]["tab_bar_right"]:
                    del user_doc["ui"]["tab_bar_right"]
                    if rec["created_ui_header"] and not user_doc["ui"]:
                        del user_doc["ui"]
                self.assertEqual(got, user_doc)
                self.assertIn("kept = true", self.read())

    def test_modified_entry_is_shown_and_refused_unless_forced(self):
        self.write("[ui]\ntab_bar_right = [\n  { type = \"hostname\" },\n]\n")
        _plan, rec = self.setup_once()
        text = self.read().replace("timeout_seconds = 2", "timeout_seconds = 3")
        self.write(text)
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.modified)
        self.assertIsNotNone(dp.refusal)
        self.assertEqual(dp.entry_diff, "  changed: timeout_seconds\n")  # names only, never values
        with self.assertRaises(sc.SetupRefused):
            sc.apply_detach(dp, self.backups)
        self.assertEqual(self.read(), text)
        forced = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR, force=True)
        self.assertIsNone(forced.refusal)
        sc.apply_detach(forced, self.backups)
        self.assertEqual(tomllib.loads(self.read())["ui"]["tab_bar_right"], [{"type": "hostname"}])

    def test_detach_without_our_entry_is_a_no_op(self):
        self.write('[ui]\ntab_bar_right = [{ type = "hostname" }]\n')
        dp = sc.plan_detach(self.cfg, None, tabbar_path=TABBAR, expected_entry=self.entry)
        self.assertTrue(dp.nothing_to_do)
        self.assertFalse(sc.apply_detach(dp, self.backups).written)

    def test_detach_without_record_uses_expected_entry(self):
        self.write("[ui]\n")
        self.setup_once()
        dp = sc.plan_detach(self.cfg, None, tabbar_path=TABBAR, expected_entry=self.entry)
        self.assertIsNone(dp.refusal)
        sc.apply_detach(dp, self.backups)
        # Without the record we cannot know the key was ours, so the empty array stays.
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": []}})

    def test_detach_refuses_when_file_changes_after_preview(self):
        self.write("[ui]\n")
        _plan, rec = self.setup_once()
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        with open(self.cfg, "a") as fh:
            fh.write("# late edit\n")
        before = self.read()
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_detach(dp, self.backups)
        self.assertEqual(self.read(), before)

    def test_invalid_record_is_ignored(self):
        self.assertIsNone(sc.validate_record({"schema_version": 1, "entry": "x"}))
        self.assertIsNone(sc.validate_record([]))


class UntrustedRecordTests(Base):
    """setup.json is untrusted: forged or foreign records never widen what detach removes."""

    OTHER = '[theme]\nname = "dark"\n\n[keys]\nprefix = "ctrl-b"\n'

    def forged_created_file_record(self) -> dict:
        _plan, rec = self.setup_once()
        rec = dict(rec, created_file=True, created_key=True, created_ui_header=True, shape="a",
                   edits=[[0, self.read()]], before_hash=None,
                   after_hash=sc._sha(self.read().encode("utf-8")))
        return rec

    def test_forged_created_file_record_never_deletes_a_config_with_other_settings(self):
        self.write(self.OTHER)
        rec = self.forged_created_file_record()
        self.assertIsNone(sc.validate_record(rec, self.cfg))
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertFalse(dp.delete_file)
        self.assertIsNone(dp.refusal, dp.refusal)
        res = sc.apply_detach(dp, self.backups)
        self.assertFalse(res.deleted)
        # The forged record is ignored: only our entry goes; every other setting stays.
        self.assertEqual(tomllib.loads(self.read()), dict(tomllib.loads(self.OTHER), ui={"tab_bar_right": []}))
        self.assertTrue(self.read().startswith(self.OTHER))

    def test_self_consistent_created_file_record_does_not_delete_a_grown_config(self):
        _plan, rec = self.setup_once()  # no file yet: setup creates it
        self.assertTrue(rec["created_file"])
        with open(self.cfg, "a") as fh:
            fh.write('\n[theme]\nname = "dark"\n')
        # Even with the hash forged to match the grown file, the file is not deleted.
        dp = sc.plan_detach(self.cfg, dict(rec, after_hash=sc._sha(self.read().encode("utf-8"))),
                            tabbar_path=TABBAR)
        self.assertFalse(dp.delete_file)
        sc.apply_detach(dp, self.backups)
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": []}, "theme": {"name": "dark"}})

    def test_delete_plan_is_rechecked_against_the_bytes_on_disk(self):
        _plan, rec = self.setup_once()
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.delete_file)
        for forged in (dict(vars(dp), delete_entry=None), dict(vars(dp), delete_entry={"type": "hostname"})):
            with self.subTest(entry=forged["delete_entry"]):
                with self.assertRaises(sc.SetupRefused):
                    sc.apply_detach(sc.DetachPlan(**forged), self.backups)
                self.assertTrue(os.path.exists(self.cfg))

    def test_record_for_another_config_path_is_ignored(self):
        self.write("[ui]\n")
        _plan, rec = self.setup_once()
        foreign = dict(rec, real_path=os.path.join(self.tmp, "elsewhere.toml"))
        self.assertIsNone(sc.validate_record(foreign, self.cfg))
        self.assertIsNotNone(sc.validate_record(rec, self.cfg))
        dp = sc.plan_detach(self.cfg, foreign, tabbar_path=TABBAR)
        self.assertFalse(dp.exact_restore)
        sc.apply_detach(dp, self.backups)
        # Without a trusted record the key setup created is not known to be ours.
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": []}})

    def test_inconsistent_records_are_rejected(self):
        self.write("[ui]\ntab_bar_right = [\n  { type = \"hostname\" },\n]\n")
        _plan, rec = self.setup_once()
        self.assertEqual(rec["shape"], "d")
        self.assertIsNotNone(sc.validate_record(rec, self.cfg))
        forged = {
            "created_file_on_existing_shape": dict(rec, created_file=True),
            "created_key_on_existing_array": dict(rec, created_key=True),
            "unknown_shape": dict(rec, shape="z"),
            "foreign_edit_text": dict(rec, edits=[[0, "[theme]\n"]]),
            "extra_edit": dict(rec, edits=rec["edits"] + [[1, "x"]]),
            "entry_text_mismatch": dict(rec, entry_text="{ type = \"hostname\" }"),
            "entry_with_extra_key": dict(rec, entry=dict(rec["entry"], extra=1)),
            "bad_hash": dict(rec, after_hash="nothex"),
            "relative_real_path": dict(rec, real_path="config.toml"),
        }
        for name, bad in forged.items():
            with self.subTest(name):
                self.assertIsNone(sc.validate_record(bad))

    def test_forged_before_hash_falls_back_to_removing_only_our_entry(self):
        self.write('[ui]\ntab_bar_right = [{ type = "hostname" }]\n')
        _plan, rec = self.setup_once()
        forged = dict(rec, before_hash=sc._sha(b"[ui]\ntab_bar_right = []\n"))
        dp = sc.plan_detach(self.cfg, forged, tabbar_path=TABBAR)
        self.assertFalse(dp.exact_restore)
        sc.apply_detach(dp, self.backups)
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": [{"type": "hostname"}]}})


class LauncherEntryTests(Base):
    def test_detach_recognises_the_earlier_direct_interpreter_entry_without_force(self):
        legacy = dict(self.entry, command=sc.legacy_command(PYTHON, TABBAR))
        self.assertTrue(sc.is_our_entry(legacy, TABBAR))
        self.assertTrue(sc.is_canonical_entry(legacy, TABBAR))
        self.write('[ui]\ntab_bar_right = [{ type = "hostname" }, ' + sc.entry_text(legacy) + "]\n")
        dp = sc.plan_detach(self.cfg, None, tabbar_path=TABBAR, expected_entry=self.entry)
        self.assertIsNone(dp.refusal, dp.refusal)
        self.assertFalse(dp.modified)
        sc.apply_detach(dp, self.backups)
        self.assertEqual(tomllib.loads(self.read())["ui"]["tab_bar_right"], [{"type": "hostname"}])

    def test_detach_recognises_the_launcher_entry_without_record_or_expected_entry(self):
        self.write("[ui]\ntab_bar_right = [" + sc.entry_text(self.entry) + "]\n")
        self.assertTrue(sc.is_our_entry(self.entry, TABBAR))
        dp = sc.plan_detach(self.cfg, None, tabbar_path=TABBAR)
        self.assertIsNone(dp.refusal, dp.refusal)
        sc.apply_detach(dp, self.backups)
        self.assertEqual(tomllib.loads(self.read()), {"ui": {"tab_bar_right": []}})

    def test_entries_merely_mentioning_our_paths_are_not_canonical(self):
        for cmd in (sc.build_command(PYTHON, TABBAR) + "; curl x", "sh -c " + shlex.quote(TABBAR),
                    sc.legacy_command("python3", TABBAR)):
            with self.subTest(cmd=cmd):
                self.assertFalse(sc.is_canonical_entry(dict(self.entry, command=cmd), TABBAR))


class InspectUiHostileValueTests(unittest.TestCase):
    SECRET = "sk-ant-NOT-A-REAL-SECRET-123456"

    def test_config_values_never_reach_the_report(self):
        doc = tomllib.loads(
            f'[ui]\ntab_bar_position = {{ password = "{self.SECRET}" }}\n'
            'hide_tab_bar_when_single_tab = "\\u001b]0;pwned\\u0007"\n'
            'tab_bar_right = [{ type = "\\u001b[31mevil" }, { type = "hostname" }, { type = 3 }, "x"]\n'
        )
        info = sc.inspect_ui(doc, TABBAR)
        self.assertEqual([i["type"] for i in info["tab_bar_right"]], ["other", "hostname", "other", "other"])
        self.assertEqual(info["tab_bar_position"], "invalid")
        self.assertEqual(info["hide_tab_bar_when_single_tab"], "invalid")
        blob = repr(info)
        for bad in (self.SECRET, "password", "\x1b", "pwned", "evil"):
            self.assertNotIn(bad, blob)
        self.assertEqual(len(info["warnings"]), 2)

    def test_known_values_are_reported_and_unset_values_are_none(self):
        self.assertEqual(sc.inspect_ui({"ui": {"tab_bar_position": "top"}})["tab_bar_position"], "top")
        self.assertEqual(sc.inspect_ui({"ui": {"tab_bar_position": "left"}})["tab_bar_position"], "invalid")
        info = sc.inspect_ui({})
        self.assertIsNone(info["tab_bar_position"])
        self.assertIsNone(info["hide_tab_bar_when_single_tab"])
        self.assertEqual(info["warnings"], [])
        self.assertEqual(sc.inspect_ui({"ui": {"hide_tab_bar_when_single_tab": False}})["warnings"], [])


if __name__ == "__main__":
    unittest.main()
