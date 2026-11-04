"""Tests for quotaribbon.paths: layout, env/bootstrap constructors, private dirs."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from quotaribbon.paths import Paths, PathsError


class PathsTests(unittest.TestCase):
    def test_layout(self):
        p = Paths("/c", "/s")
        self.assertEqual(p.settings, "/c/settings.json")
        self.assertEqual(p.tabbar_py, "/c/tabbar.py")
        self.assertEqual(p.bootstrap_json, "/c/bootstrap.json")
        self.assertEqual(p.control, "/s/control.json")
        self.assertEqual(p.server_epoch, "/s/server_epoch.json")
        self.assertEqual(p.settings_last_good, "/s/settings.last_good.json")
        self.assertEqual(p.dispatch, "/s/dispatch.json")
        self.assertEqual(p.salt, "/s/salt")
        self.assertEqual(p.setup_record, "/s/setup.json")
        bk = "0123456789abcdef01234567"
        self.assertEqual(p.clock, "/s/clock.json")
        self.assertEqual(p.binding_dir("claude", bk), f"/s/providers/claude/{bk}")
        self.assertEqual(p.connection("claude", bk), f"/s/providers/claude/{bk}/connection.json")
        self.assertEqual(p.status("codex", bk), f"/s/providers/codex/{bk}/status.json")
        self.assertEqual(p.snapshot("codex", bk), f"/s/providers/codex/{bk}/snapshot.json")
        self.assertEqual(p.dispatch_lock, "/s/locks/dispatch.lock")
        self.assertEqual(p.settings_lock, "/s/locks/settings.lock")
        self.assertEqual(p.setup_lock, "/s/locks/setup.lock")
        self.assertEqual(p.provider_lock("claude", "abc123"), "/s/locks/claude-abc123.lock")
        self.assertEqual(p.work_dir("claude"), "/s/work/claude")
        self.assertEqual(p.log_file, "/s/logs/quotaribbon.log")
        self.assertEqual(p.pycache_dir, "/s/pycache")

    def test_rejects_traversal_and_unknown_provider(self):
        p = Paths("/c", "/s")
        with self.assertRaises(PathsError):
            p.connection("../x", "0123456789abcdef01234567")
        for bad in ("../../../etc", "0123456789ABCDEF01234567", "0123456789abcdef0123456", "0123456789abcdef012345678",
                    "", None, "0123456789abcdef0123456/"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathsError):
                    p.snapshot("claude", bad)
        with self.assertRaises(PathsError):
            p.provider_lock("claude", "../../etc")

    def test_from_env(self):
        p = Paths.from_env({"HERDR_PLUGIN_CONFIG_DIR": "/c/", "HERDR_PLUGIN_STATE_DIR": "/s"})
        self.assertEqual((p.config_dir, p.state_dir), ("/c", "/s"))
        with self.assertRaisesRegex(PathsError, "HERDR_PLUGIN_STATE_DIR"):
            Paths.from_env({"HERDR_PLUGIN_CONFIG_DIR": "/c"})
        with self.assertRaisesRegex(PathsError, "HERDR_PLUGIN_CONFIG_DIR"):
            Paths.from_env({"HERDR_PLUGIN_CONFIG_DIR": "relative", "HERDR_PLUGIN_STATE_DIR": "/s"})

    def test_ensure_private_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Paths(os.path.join(tmp, "설정 dir"), os.path.join(tmp, "state 'q'"))
            os.makedirs(p.state_dir, mode=0o755)
            os.chmod(p.state_dir, 0o755)
            p.ensure_private_dirs()
            for d in p.private_dirs():
                self.assertTrue(os.path.isdir(d), d)
                self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)

    def test_ensure_binding_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Paths(os.path.join(tmp, "c"), os.path.join(tmp, "s"))
            d = p.ensure_binding_dir("codex", "0123456789abcdef01234567")
            self.assertEqual(d, p.binding_dir("codex", "0123456789abcdef01234567"))
            for path in (p.providers_dir, p.provider_dir("codex"), d):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
            other = os.path.join(tmp, "elsewhere")
            os.makedirs(other)
            os.makedirs(p.provider_dir("claude"))
            os.symlink(other, p.binding_dir("claude", "0123456789abcdef01234567"))
            with self.assertRaises(PathsError):
                p.ensure_binding_dir("claude", "0123456789abcdef01234567")

    def test_symlinked_state_subdir_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Paths(os.path.join(tmp, "c"), os.path.join(tmp, "s"))
            os.makedirs(p.state_dir)
            os.symlink(tmp, p.locks_dir)
            with self.assertRaises(PathsError):
                p.ensure_private_dirs()


if __name__ == "__main__":
    unittest.main()
