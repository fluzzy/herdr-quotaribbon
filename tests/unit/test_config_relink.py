"""A config symlink repointed between preview and apply is detected; neither file changes."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from quotaribbon import setup_config as sc

HOME = os.path.join(tempfile.gettempdir(), "qr-home")
TABBAR = os.path.join(HOME, ".config/herdr/plugins/config/fluzzy.herdr-quotaribbon/tabbar.py")
PYTHON = os.path.join(HOME, ".pyenv/versions/3.12.13/bin/python3")

A_TEXT = '[ui]\ntab_bar_right = [{ type = "hostname" }]\n'
B_TEXT = '# the other file\n[theme]\nname = "x"\n'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.backups = os.path.join(self.tmp, "backups")
        self.cfg = os.path.join(self.tmp, "config.toml")
        self.a = os.path.join(self.tmp, "a", "herdr.toml")
        self.b = os.path.join(self.tmp, "b", "herdr.toml")
        for path, text in ((self.a, A_TEXT), (self.b, B_TEXT)):
            os.makedirs(os.path.dirname(path))
            self.put(path, text)
        os.symlink(self.a, self.cfg)
        self.entry = sc.build_entry(PYTHON, TABBAR)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def put(path: str, text: str) -> None:
        with open(path, "w", newline="") as fh:
            fh.write(text)

    @staticmethod
    def get(path: str) -> str | None:
        try:
            with open(path, newline="") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    @staticmethod
    def relink(link: str, target: str) -> None:
        tmp_link = link + ".new"
        os.symlink(target, tmp_link)
        os.rename(tmp_link, link)  # atomic swap; os.replace stays free for the code under test

    def repoint(self, target: str) -> None:
        self.relink(self.cfg, target)

    def assert_untouched(self, a_text: str | None = A_TEXT) -> None:
        self.assertEqual(self.get(self.a), a_text)
        self.assertEqual(self.get(self.b), B_TEXT)
        for d in (os.path.dirname(self.a), os.path.dirname(self.b)):
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".qr-tmp")], [])

    def setup_applied(self) -> dict:
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNone(plan.refusal, plan.refusal)
        sc.apply_plan(plan, self.backups)
        return sc.setup_record(plan)


class SetupRelinkTests(Base):
    def test_unchanged_symlink_is_still_edited_through(self):
        self.setup_applied()
        self.assertTrue(os.path.islink(self.cfg))
        self.assertIn(sc.entry_text(self.entry), self.get(self.a))
        self.assertEqual(self.get(self.b), B_TEXT)

    def test_symlink_repointed_before_apply_is_refused(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertIsNone(plan.refusal, plan.refusal)
        self.repoint(self.b)
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assert_untouched()

    def test_symlink_replaced_by_another_link_to_the_same_file_is_refused(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        os.unlink(self.cfg)
        os.symlink(os.path.relpath(self.a, self.tmp), self.cfg)  # same file, different link
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assert_untouched()

    def test_missing_config_that_becomes_a_symlink_before_apply_is_refused(self):
        os.unlink(self.cfg)
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        self.assertEqual(plan.shape, "a")
        os.symlink(self.b, self.cfg)
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assert_untouched()

    def test_symlink_repointed_right_before_replace_is_refused(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        real_hash = sc._current_hash
        calls = {"n": 0}

        def racing_hash(path):
            calls["n"] += 1
            if calls["n"] == 2:  # the re-check immediately before os.replace
                self.repoint(self.b)
            return real_hash(path)

        with mock.patch.object(sc, "_current_hash", racing_hash), \
                mock.patch.object(sc.os, "replace", wraps=os.replace) as replace:
            with self.assertRaises(sc.ConcurrentEdit):
                sc.apply_plan(plan, self.backups)
        self.assertEqual(calls["n"], 2)
        replace.assert_not_called()
        self.assert_untouched()

    def test_plan_without_a_recorded_link_is_refused(self):
        plan = sc.plan_setup(self.cfg, self.entry, tabbar_path=TABBAR)
        plan.link_identity = None
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_plan(plan, self.backups)
        self.assert_untouched()


class DetachRelinkTests(Base):
    def test_symlink_repointed_before_detach_is_refused(self):
        rec = self.setup_applied()
        a_after = self.get(self.a)
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertIsNone(dp.refusal, dp.refusal)
        self.assertFalse(dp.delete_file)
        self.repoint(self.b)
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_detach(dp, self.backups)
        self.assert_untouched(a_after)

    def created_through_dir_link(self) -> tuple[str, str, str, dict]:
        """Setup creates the file through a symlinked config directory (``herdr -> a/``);
        ``b/config.toml`` is the file a repointed directory link would name instead."""
        link_dir = os.path.join(self.tmp, "herdr")
        os.symlink(os.path.dirname(self.a), link_dir)
        cfg = os.path.join(link_dir, "config.toml")
        created = os.path.join(os.path.dirname(self.a), "config.toml")
        other = os.path.join(os.path.dirname(self.b), "config.toml")
        self.put(other, B_TEXT)
        plan = sc.plan_setup(cfg, self.entry, tabbar_path=TABBAR)
        self.assertEqual(plan.shape, "a")
        sc.apply_plan(plan, self.backups)
        self.assertTrue(os.path.isfile(created))
        return link_dir, cfg, other, sc.setup_record(plan)

    def test_dir_link_repointed_before_deleting_a_created_file_is_refused(self):
        link_dir, cfg, other, rec = self.created_through_dir_link()
        created = os.path.join(os.path.dirname(self.a), "config.toml")
        created_text = self.get(created)
        dp = sc.plan_detach(cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.delete_file, dp.reason)
        self.relink(link_dir, os.path.dirname(self.b))
        with self.assertRaises(sc.ConcurrentEdit) as cm:
            sc.apply_detach(dp, self.backups)
        self.assertIn("nothing was deleted", str(cm.exception))
        self.assertEqual(self.get(created), created_text)
        self.assertEqual(self.get(other), B_TEXT)

    def test_created_file_replaced_by_a_symlink_before_delete_is_refused(self):
        os.unlink(self.cfg)
        rec = self.setup_applied()  # creates config.toml itself
        created_text = self.get(self.cfg)
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.delete_file, dp.reason)
        os.rename(self.cfg, self.a)
        os.symlink(self.a, self.cfg)  # same bytes, now reached through a link
        with self.assertRaises(sc.ConcurrentEdit):
            sc.apply_detach(dp, self.backups)
        self.assertEqual(self.get(self.a), created_text)
        self.assertEqual(self.get(self.b), B_TEXT)

    def test_dir_link_repointed_right_before_unlink_is_refused(self):
        link_dir, cfg, other, rec = self.created_through_dir_link()
        created = os.path.join(os.path.dirname(self.a), "config.toml")
        created_text = self.get(created)
        dp = sc.plan_detach(cfg, rec, tabbar_path=TABBAR)
        self.assertTrue(dp.delete_file, dp.reason)
        real_hash = sc._current_hash

        def racing_hash(path):
            self.relink(link_dir, os.path.dirname(self.b))
            return real_hash(path)

        with mock.patch.object(sc, "_current_hash", racing_hash), \
                mock.patch.object(sc.os, "unlink", wraps=os.unlink) as unlink:
            with self.assertRaises(sc.ConcurrentEdit):
                sc.apply_detach(dp, self.backups)
        # Only the backup of the change that was never made is removed.
        self.assertEqual([c.args[0] for c in unlink.call_args_list
                          if os.path.dirname(c.args[0]) != self.backups], [])
        self.assertEqual(os.listdir(self.backups), [])
        self.assertEqual(self.get(created), created_text)
        self.assertEqual(self.get(other), B_TEXT)

    def test_symlink_repointed_right_before_detach_replace_is_refused(self):
        rec = self.setup_applied()
        a_after = self.get(self.a)
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        real_hash = sc._current_hash

        def racing_hash(path):
            self.repoint(self.b)
            return real_hash(path)

        with mock.patch.object(sc, "_current_hash", racing_hash), \
                mock.patch.object(sc.os, "replace", wraps=os.replace) as replace:
            with self.assertRaises(sc.ConcurrentEdit):
                sc.apply_detach(dp, self.backups)
        replace.assert_not_called()
        self.assert_untouched(a_after)

    def test_revert_follows_the_current_link_and_refuses_a_foreign_target(self):
        rec = self.setup_applied()
        a_after = self.get(self.a)
        self.repoint(self.b)
        # The record belongs to A; through the repointed link it is ignored and B is left alone.
        res = sc.revert_setup(self.cfg, rec, self.backups, TABBAR)
        self.assertFalse(res.written)
        self.assert_untouched(a_after)

    def test_unchanged_symlink_detach_still_works(self):
        rec = self.setup_applied()
        dp = sc.plan_detach(self.cfg, rec, tabbar_path=TABBAR)
        sc.apply_detach(dp, self.backups)
        self.assertEqual(self.get(self.a), A_TEXT)
        self.assertTrue(os.path.islink(self.cfg))


if __name__ == "__main__":
    unittest.main()
