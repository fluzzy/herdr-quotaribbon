"""Provider children only see trusted PATH entries; validated spellings are executed as is."""

from __future__ import annotations

import os
import tempfile
import unittest

from quotaribbon import connection as conn
from quotaribbon import procutil
from quotaribbon.paths import Paths
from quotaribbon.types import ProviderSettings, Settings


def _settings_with_binary(binary: str) -> Settings:
    base = Settings.default()
    provs = dict(base.providers)
    provs["codex"] = ProviderSettings(enabled=True, binary=binary)
    return Settings(schema_version=1, providers=provs, display_mode=base.display_mode,
                    interval_seconds=base.interval_seconds, stale_after_seconds=base.stale_after_seconds,
                    hide_values_after_seconds=base.hide_values_after_seconds)


class ChildPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.trusted = os.path.join(self.root, "trusted-bin")
        self.open_dir = os.path.join(self.root, "shared-bin")
        os.mkdir(self.trusted, 0o755)
        os.mkdir(self.open_dir)
        os.chmod(self.open_dir, 0o777)  # anyone may drop files here
        self.marker = os.path.join(self.root, "interpreter-hijacked")
        # An "interpreter" that only an untrusted directory provides.
        fake = os.path.join(self.open_dir, "qrnode")
        with open(fake, "w") as fh:
            fh.write(f"#!/bin/sh\ntouch '{self.marker}'\n")
        os.chmod(fake, 0o755)
        # A trusted CLI written as an env-shebang script.
        self.cli = os.path.join(self.trusted, "codex")
        with open(self.cli, "w") as fh:
            fh.write("#!/usr/bin/env qrnode\n")
        os.chmod(self.cli, 0o755)
        self.paths = Paths(config_dir=os.path.join(self.root, "cfg"), state_dir=os.path.join(self.root, "st"))
        self.env = {"HOME": self.root, "PATH": os.pathsep.join([self.open_dir, self.trusted, "/usr/bin", "/bin"])}

    def test_child_path_keeps_only_trusted_entries_in_order(self):
        path = conn.trusted_child_path(self.env, caller_cwd="/")
        entries = path.split(os.pathsep)
        self.assertNotIn(self.open_dir, entries)
        self.assertEqual(entries[0], self.trusted)
        self.assertIn("/usr/bin", entries)

    def test_relative_duplicate_and_pane_cwd_entries_are_dropped(self):
        env = dict(self.env, PATH=os.pathsep.join(["bin", "", self.trusted, self.trusted]),
                   HERDR_ACTIVE_PANE_CWD=self.trusted)
        self.assertEqual(conn.trusted_child_path(env, caller_cwd="/"), "/usr/bin:/bin")  # never empty
        env = dict(self.env, PATH=os.pathsep.join(["bin", "", self.trusted, self.trusted]))
        self.assertEqual(conn.trusted_child_path(env, caller_cwd="/"), self.trusted)

    def test_child_path_is_never_empty(self):
        from unittest import mock

        for value in ("", "relative:bin", self.open_dir):
            with self.subTest(PATH=value):
                got = conn.trusted_child_path(dict(self.env, PATH=value), caller_cwd="/")
                self.assertTrue(got)
                self.assertNotIn("", got.split(os.pathsep))
        with mock.patch("quotaribbon.trust.trusted_path_entries", return_value=[]):
            self.assertEqual(conn.trusted_child_path(dict(self.env, PATH=""), caller_cwd="/"), conn.NOWHERE_PATH)

    def test_collector_environment_carries_the_filtered_path_and_everything_else(self):
        host = conn.LiveHostAdapter(self.paths, dict(self.env, OPENAI_API_KEY="kept"), Settings.default())
        child = host.child_env()
        self.assertNotIn(self.open_dir, child["PATH"].split(os.pathsep))
        self.assertEqual(child["OPENAI_API_KEY"], "kept")
        self.assertEqual(child["HOME"], self.root)

    def test_env_shebang_cannot_pick_an_interpreter_from_an_untrusted_dir(self):
        host = conn.LiveHostAdapter(self.paths, self.env, Settings.default())
        res = procutil.run_capture([self.cli, "--version"], cwd=self.root, env=host.child_env(), timeout_s=10,
                                   grace_s=1.0)
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotEqual(res.returncode, 0)
        # With the caller's full PATH the same script would have run the planted interpreter.
        procutil.run_capture([self.cli], cwd=self.root, env=self.env, timeout_s=10, grace_s=1.0)
        self.assertTrue(os.path.exists(self.marker))

    def test_script_whose_interpreter_is_untrusted_is_not_used(self):
        # codex is "#!/usr/bin/env qrnode" and qrnode exists only in the untrusted dir.
        self.assertIsNone(conn.trusted_path_lookup("codex", self.env, caller_cwd="/"))
        host = conn.LiveHostAdapter(self.paths, self.env, _settings_with_binary(self.cli))
        self.assertIsNone(host.resolve_executable("codex", host.settings.providers["codex"]))
        trusted_node = os.path.join(self.trusted, "qrnode")
        with open(trusted_node, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(trusted_node, 0o755)
        self.assertEqual(conn.trusted_path_lookup("codex", self.env, caller_cwd="/"), self.cli)

    def test_configured_binary_is_returned_exactly_as_validated(self):
        trusted_node = os.path.join(self.trusted, "qrnode")  # the script's interpreter is trusted here
        with open(trusted_node, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(trusted_node, 0o755)
        link = os.path.join(self.root, "link")
        os.symlink(self.trusted, link)
        spelled = os.path.join(link, "..", "trusted-bin", "codex")
        host = conn.LiveHostAdapter(self.paths, self.env, _settings_with_binary(spelled))
        got = host.resolve_executable("codex", host.settings.providers["codex"])
        self.assertEqual(got, spelled)  # never re-normalised after the trust check


if __name__ == "__main__":
    unittest.main()
