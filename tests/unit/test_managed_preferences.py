"""Managed-preferences plists close the Claude policy gate, and diagnostics list them."""

from __future__ import annotations

import os
import pwd
import tempfile
import unittest
from unittest import mock

from quotaribbon import diagnostics
from quotaribbon.providers import claude


class ManagedPreferencesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, "ClaudeCode")
        os.mkdir(self.root)

    def test_default_paths_are_per_user_then_device_level_on_macos(self):
        name = pwd.getpwuid(os.getuid()).pw_name
        with mock.patch.object(claude.sys, "platform", "darwin"):
            self.assertEqual(claude.default_managed_preferences(), (
                f"/Library/Managed Preferences/{name}/com.anthropic.claudecode.plist",
                "/Library/Managed Preferences/com.anthropic.claudecode.plist",
            ))
        with mock.patch.object(claude.sys, "platform", "linux"):
            self.assertEqual(claude.default_managed_preferences(), ())

    def test_without_an_account_name_only_the_device_level_path_applies(self):
        with mock.patch.object(claude.sys, "platform", "darwin"), \
                mock.patch.object(claude.pwd, "getpwuid", side_effect=KeyError("uid")):
            self.assertEqual(claude.default_managed_preferences(),
                             ("/Library/Managed Preferences/com.anthropic.claudecode.plist",))

    def test_any_present_plist_closes_the_gate(self):
        plist = os.path.join(self.tmp, "com.anthropic.claudecode.plist")
        self.assertFalse(claude.managed_policy_present(self.root, (plist,)))
        open(plist, "w").close()
        self.assertTrue(claude.managed_policy_present(self.root, (plist,)))
        os.unlink(plist)
        os.symlink(os.path.join(self.tmp, "missing"), plist)  # a dangling link still counts
        self.assertTrue(claude.managed_policy_present(self.root, (plist,)))

    def test_an_unreadable_parent_fails_closed(self):
        locked = os.path.join(self.tmp, "locked")
        os.mkdir(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.X_OK):
            self.skipTest("running with privileges that bypass permissions")
        self.assertTrue(claude.managed_policy_present(self.root, (os.path.join(locked, "x.plist"),)))

    def test_sources_name_every_present_kind(self):
        plist = os.path.join(self.tmp, "p.plist")
        open(plist, "w").close()
        open(os.path.join(self.root, "managed-settings.json"), "w").close()
        os.mkdir(os.path.join(self.root, "managed-settings.d"))
        open(os.path.join(self.root, "managed-settings.d", "10-org.json"), "w").close()
        self.assertEqual(claude.managed_policy_sources(self.root, (plist,)), [
            os.path.join(self.root, "managed-settings.json"),
            os.path.join(self.root, "managed-settings.d"),
            plist,
        ])

    def test_the_collector_checks_the_plists_before_starting_claude(self):
        plist = os.path.join(self.tmp, "p.plist")
        open(plist, "w").close()
        c = claude.ClaudeCollector(executable="/nonexistent/claude", profile_dir=self.tmp, work_dir=self.tmp,
                                   env={"PATH": "/usr/bin:/bin", "HOME": self.tmp}, salt=b"s",
                                   managed_settings_dir=self.root, managed_preferences=(plist,))
        with mock.patch.object(c, "cli_version", return_value="2.1.318"), \
                mock.patch.object(c, "_auth", side_effect=AssertionError("must not start claude")):
            r = c.collect(10)
        self.assertEqual((r.outcome, r.error_code), ("protocol_unsupported", "managed_policy"))
        self.assertEqual(r.notes, ("managed_policy_present",))


class DiagnosticsTests(unittest.TestCase):
    def test_present_plists_are_listed_with_the_account_name_masked(self):
        sources = ["/Library/Application Support/ClaudeCode/managed-settings.json",
                   "/Library/Managed Preferences/someone/com.anthropic.claudecode.plist",
                   "/Library/Managed Preferences/com.anthropic.claudecode.plist"]
        with mock.patch.object(claude, "managed_policy_sources", return_value=sources):
            self.assertEqual(diagnostics._managed_policy_files(), [
                "/Library/Application Support/ClaudeCode/managed-settings.json",
                "/Library/Managed Preferences/<user>/com.anthropic.claudecode.plist",
                "/Library/Managed Preferences/com.anthropic.claudecode.plist",
            ])


if __name__ == "__main__":
    unittest.main()
