"""Preview diffs mask user lines even when their content looks like a diff header."""

from __future__ import annotations

import unittest

from quotaribbon import setup_config as sc

PATH = "/tmp/qr/config.toml"


class PreviewHeaderMasking(unittest.TestCase):
    def diff(self, before: str, after: str) -> str:
        return sc._udiff(before, after, PATH)

    def assert_hidden(self, diff: str, secret: str = "secret") -> None:
        self.assertNotIn(secret, diff)

    def test_removed_line_starting_with_dashes_is_masked(self):
        before = 'x = """\n--secret\n"""\n'
        after = 'x = """\n"""\n'
        self.assert_hidden(self.diff(before, after))

    def test_added_line_starting_with_pluses_is_masked(self):
        before = 'x = """\n"""\n'
        after = 'x = """\n++secret\n"""\n'
        self.assert_hidden(self.diff(before, after))

    def test_changed_line_shaped_like_hunk_header_is_masked(self):
        before = 'x = """\n@@ -1 +1 @@ secret\n"""\n'
        after = 'x = """\n@@ -1 +1 @@ secret2\n"""\n'
        self.assert_hidden(self.diff(before, after))

    def test_bare_hunk_header_shaped_user_line_is_masked(self):
        before = 'x = """\n@@ -1 +1 @@\n"""\n'
        after = 'x = """\n"""\n'
        lines = self.diff(before, after).splitlines()
        self.assertNotIn("-@@ -1 +1 @@", lines)

    def test_all_three_in_one_changed_region(self):
        before = 'x = """\n--secret\n++secret\n@@ -1 +1 @@ secret\n"""\n'
        after = 'x = """\n++secret\n--secret\n@@ -9 +9 @@ secret\n"""\n'
        self.assert_hidden(self.diff(before, after))

    def test_normal_headers_still_appear(self):
        before = 'a = 1\n--secret\n'
        after = 'a = 1\n++secret\n'
        lines = self.diff(before, after).splitlines()
        self.assertEqual(lines[0], f"--- {PATH}")
        self.assertEqual(lines[1], f"+++ {PATH} (after)")
        self.assertEqual(lines[2], "@@ -2 +2 @@")
        self.assertTrue(lines[3].startswith("-"))
        self.assertTrue(lines[4].startswith("+"))
        self.assert_hidden("\n".join(lines))

    def test_header_for_new_file_appears(self):
        lines = sc._udiff(None, "a = 1\n", PATH).splitlines()
        self.assertEqual(lines[:3], [f"--- {PATH}", f"+++ {PATH} (after)", "@@ -0,0 +1 @@"])


if __name__ == "__main__":
    unittest.main()
