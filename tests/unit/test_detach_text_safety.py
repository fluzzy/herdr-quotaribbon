"""Detach removes only our tab-bar element: user comments and text survive, every copy goes."""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest

from quotaribbon import setup_config as sc

HOME = os.path.join(tempfile.gettempdir(), "qr-home")
TABBAR = os.path.join(HOME, ".config/herdr/plugins/config/fluzzy.herdr-quotaribbon/tabbar.py")
PYTHON = os.path.join(HOME, ".pyenv/versions/3.12.13/bin/python3")


class DetachTextSafety(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.cfg = os.path.join(self.tmp, "config.toml")
        self.backups = os.path.join(self.tmp, "backups")
        self.entry = sc.build_entry(PYTHON, TABBAR)
        self.ours = sc.entry_text(self.entry)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self) -> str:
        with open(self.cfg, encoding="utf-8") as fh:
            return fh.read()

    def detach(self, text: str, *, force: bool = False):
        self.write(text)
        return sc.plan_detach(self.cfg, None, tabbar_path=TABBAR, expected_entry=self.entry, force=force)

    def assert_clean_removal(self, text: str, expected_after: str | None = None):
        plan = self.detach(text)
        self.assertIsNone(plan.refusal, plan.reason)
        after = plan.after_text
        self.assertNotIn(self.ours, after)
        self.assertEqual(sc._comments(after), sc._comments(text))
        doc = tomllib.loads(text)
        doc["ui"]["tab_bar_right"] = [e for e in doc["ui"]["tab_bar_right"] if e != self.entry]
        self.assertEqual(tomllib.loads(after), doc)
        if expected_after is not None:
            self.assertEqual(after, expected_after)
        res = sc.apply_detach(plan, self.backups)
        self.assertTrue(res.written)
        self.assertEqual(self.read(), after)
        return plan

    # -- comments next to the removed element ---------------------------------------------

    def test_comment_after_previous_element_is_kept(self):
        text = (f'[ui]\ntab_bar_right = [\n  {{ type = "zoom" }}, # keep me: zoom must stay first\n'
                f'  {self.ours} # added by me\n]\n')
        self.assert_clean_removal(
            text, '[ui]\ntab_bar_right = [\n  { type = "zoom" } # keep me: zoom must stay first\n'
                  '  # added by me\n]\n')

    def test_leading_comma_style_keeps_comments(self):
        text = f'[ui]\ntab_bar_right = [\n  {{ type = "zoom" }} # keep me\n  , {self.ours}\n]\n'
        self.assert_clean_removal(text, '[ui]\ntab_bar_right = [\n  { type = "zoom" } # keep me\n]\n')

    def test_leading_comma_style_in_the_middle_keeps_comments(self):
        text = (f'[ui]\ntab_bar_right = [\n  {{ type = "zoom" }} # one\n  , {self.ours} # two\n'
                f'  , {{ type = "hostname" }} # three\n]\n')
        self.assert_clean_removal(text)

    def test_comment_between_element_and_its_comma(self):
        text = f'[ui]\ntab_bar_right = [\n  {self.ours} # mine\n  , {{ type = "zoom" }}\n]\n'
        self.assert_clean_removal(text)

    def test_single_line_with_trailing_comment(self):
        text = f'[ui]\ntab_bar_right = [{{ type = "zoom" }}, {self.ours}] # right side\n'
        self.assert_clean_removal(text, '[ui]\ntab_bar_right = [{ type = "zoom" }] # right side\n')

    def test_plain_layouts_are_unchanged(self):
        self.assert_clean_removal(f'[ui]\ntab_bar_right = [\n  {{ type = "zoom" }},\n  {self.ours}\n]\n',
                                  '[ui]\ntab_bar_right = [\n  { type = "zoom" }\n]\n')
        self.assert_clean_removal(f'[ui]\ntab_bar_right = [\n  {self.ours},\n  {{ type = "zoom" }},\n]\n',
                                  '[ui]\ntab_bar_right = [\n  { type = "zoom" },\n]\n')

    def test_comment_inside_a_string_is_not_a_comment(self):
        text = f'[ui]\ntab_bar_right = [{{ type = "text", text = "a # b" }}, {self.ours}]\n'
        self.assert_clean_removal(text, '[ui]\ntab_bar_right = [{ type = "text", text = "a # b" }]\n')

    def test_created_key_with_a_user_comment_keeps_the_key(self):
        # A record says setup created the key; the user later commented on it.
        text = f'[ui]\ntab_bar_right = [{self.ours}] # my note\n'
        self.write(text)
        stmts = sc.scan(text)
        doc = tomllib.loads(text)
        after, refusal = sc._detach_structural(text, doc, [0], {"created_key": True, "created_ui_header": True})
        self.assertIsNone(refusal)
        self.assertEqual(after, "[ui]\ntab_bar_right = [] # my note\n")
        self.assertTrue(stmts)

    def test_a_comment_losing_edit_is_refused(self):
        # Guard: whatever the removal, a changed comment list is refused, never written.
        text = f'[ui]\ntab_bar_right = [\n  {{ type = "zoom" }}, # keep\n  {self.ours}\n]\n'
        doc = tomllib.loads(text)
        real = sc._remove_element
        try:
            sc._remove_element = lambda t, vs, idx: t.replace(", # keep\n  " + self.ours, "")
            after, refusal = sc._detach_structural(text, doc, [1], None)
        finally:
            sc._remove_element = real
        self.assertIsNone(after)
        self.assertIn("comment", refusal)

    # -- preview ---------------------------------------------------------------------------

    def test_preview_shows_changed_user_lines_without_their_content(self):
        text = (f'[ui]\ntab_bar_right = [\n  {{ type = "command", command = "secret-token" }}, # keep me\n'
                f'  {self.ours}\n]\n')
        plan = self.detach(text)
        self.assertIsNone(plan.refusal)
        self.assertNotIn("secret-token", plan.diff)
        self.assertNotIn("keep me", plan.diff)
        self.assertIn(f"-  {sc.MASK}, # {sc.MASK}\n", plan.diff)
        self.assertIn(f"+  {sc.MASK} # {sc.MASK}\n", plan.diff)

    # -- duplicate entries -----------------------------------------------------------------

    def test_all_exact_copies_are_removed(self):
        text = (f'[ui]\ntab_bar_right = [\n  {self.ours},\n  {{ type = "zoom" }}, # keep\n'
                f'  {self.ours},\n]\n')
        plan = self.assert_clean_removal(text, '[ui]\ntab_bar_right = [\n  { type = "zoom" }, # keep\n]\n')
        self.assertEqual(plan.removed_count, 2)
        self.assertIn("2 copies", plan.reason)

    def test_an_extra_edited_copy_is_refused_unless_forced(self):
        edited = dict(self.entry, interval_seconds=30)
        text = (f'[ui]\ntab_bar_right = [\n  {self.ours},\n  {{ type = "zoom" }},\n'
                f'  {sc.entry_text(edited)},\n]\n')
        plan = self.detach(text)
        self.assertIsNotNone(plan.refusal)
        self.assertTrue(plan.modified)
        self.assertIn("interval_seconds", plan.entry_diff)
        forced = self.detach(text, force=True)
        self.assertIsNone(forced.refusal)
        self.assertEqual(forced.removed_count, 2)
        self.assertEqual(tomllib.loads(forced.after_text), {"ui": {"tab_bar_right": [{"type": "zoom"}]}})

    def test_single_copy_reports_one(self):
        plan = self.detach(f'[ui]\ntab_bar_right = [{{ type = "zoom" }}, {self.ours}]\n')
        self.assertEqual(plan.removed_count, 1)
        self.assertEqual(plan.reason, "remove only the QuotaRibbon entry")


class CommentLexer(unittest.TestCase):
    def test_strings_and_comments(self):
        text = ('a = "x # not" # one\nb = \'y # not\'\nc = """\n# not\n\\""" # still not\n""" # two\n'
                "d = '''# not''' # three\n")
        self.assertEqual(sc._comments(text), ["# one", "# two", "# three"])

    def test_unterminated_string(self):
        self.assertIsNone(sc._comments('a = "x\n'))


if __name__ == "__main__":
    unittest.main()
