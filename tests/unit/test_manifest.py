"""Tests for herdr-plugin.toml against the Herdr 0.9.5 manifest rules."""

from __future__ import annotations

import os
import re
import tomllib
import unittest

import quotaribbon

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO_ROOT, "herdr-plugin.toml")

PLUGIN_ID_RE = re.compile(r"[A-Za-z0-9.:_-]+\Z")
LOCAL_ID_RE = re.compile(r"[A-Za-z0-9:_-]+\Z")
ACTION_IDS = ("claude-on", "claude-off", "codex-on", "codex-off", "refresh", "interval", "diagnostics", "setup", "detach")
PANE_IDS = ("setup-ui", "diagnostics-ui", "detach-ui", "interval-ui")
LAUNCHER = ["/bin/sh", "bin/quotaribbon"]


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, "rb") as fh:
            cls.m = tomllib.load(fh)

    def test_required_top_level_keys(self):
        for key in ("id", "name", "version", "min_herdr_version"):
            self.assertIsInstance(self.m.get(key), str, key)
        self.assertEqual(self.m["id"], "fluzzy.herdr-quotaribbon")
        self.assertEqual(self.m["id"], quotaribbon.PLUGIN_ID)
        self.assertEqual(self.m["name"], "QuotaRibbon")
        self.assertEqual(self.m["version"], "0.1.0")
        self.assertEqual(self.m["version"], quotaribbon.__version__)
        self.assertEqual(self.m["min_herdr_version"], "0.9.5")
        self.assertEqual(self.m["platforms"], ["macos"])
        self.assertEqual(self.m["description"], "Claude Code and Codex subscription limits in your Herdr tab bar.")
        self.assertEqual(quotaribbon.REFRESH_ACTION_QUALIFIED, f"{self.m['id']}.refresh")

    def test_plugin_id_charset(self):
        self.assertRegex(self.m["id"], PLUGIN_ID_RE)
        self.assertLessEqual(len(self.m["id"]), 120)

    def test_only_known_tables(self):
        allowed = {"id", "name", "version", "min_herdr_version", "description", "platforms",
                   "build", "startup", "actions", "events", "panes", "link_handlers"}
        self.assertLessEqual(set(self.m), allowed)

    def test_actions(self):
        actions = self.m["actions"]
        ids = [a["id"] for a in actions]
        for aid in ACTION_IDS:
            self.assertEqual(ids.count(aid), 1, aid)
        self.assertEqual(len(ids), len(ACTION_IDS))
        for a in actions:
            with self.subTest(action=a["id"]):
                self.assertRegex(a["id"], LOCAL_ID_RE)
                self.assertNotIn(".", a["id"])
                self.assertTrue(a["title"].startswith("QuotaRibbon: "))
                self.assertIsInstance(a.get("description"), str)
                self.assertEqual(a["contexts"], ["global"])
                self.assertEqual(a["command"], LAUNCHER + [a["id"]])
                self.assertLessEqual(set(a), {"id", "title", "description", "contexts", "platforms", "command"})

    def test_panes(self):
        panes = self.m["panes"]
        self.assertEqual(sorted(p["id"] for p in panes), sorted(PANE_IDS))
        for p in panes:
            with self.subTest(pane=p["id"]):
                self.assertRegex(p["id"], LOCAL_ID_RE)
                self.assertNotIn(".", p["id"])
                self.assertIsInstance(p["title"], str)  # required by Herdr 0.9.5
                self.assertEqual(p["placement"], "popup")
                self.assertEqual(p["width"], "90%")
                self.assertEqual(p["height"], "80%")
                self.assertEqual(p["command"], LAUNCHER + [p["id"]])
                self.assertLessEqual(set(p), {"id", "title", "description", "platforms", "placement",
                                               "width", "height", "command"})

    def test_startup(self):
        self.assertEqual(self.m["startup"], [{"command": LAUNCHER + ["startup"]}])

    def test_launcher_is_executable(self):
        self.assertTrue(os.access(os.path.join(REPO_ROOT, "bin", "quotaribbon"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
