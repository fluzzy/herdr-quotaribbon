"""The host adapter takes credential selection from the settings snapshot it is given."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

from quotaribbon import connection as conn
from quotaribbon.paths import Paths
from quotaribbon.types import ProviderSettings, Settings


def _settings(claude: ProviderSettings) -> Settings:
    base = Settings.default()
    provs = dict(base.providers)
    provs["claude"] = claude
    return Settings(schema_version=1, providers=provs, display_mode=base.display_mode,
                    interval_seconds=base.interval_seconds, stale_after_seconds=base.stale_after_seconds,
                    hide_values_after_seconds=base.hide_values_after_seconds)


class SettingsSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.real = os.path.join(self.root, "claude-work")
        os.mkdir(self.real)
        self.alias_a = os.path.join(self.root, "alias-a")
        self.alias_b = os.path.join(self.root, "alias-b")
        os.symlink(self.real, self.alias_a)
        os.symlink(self.real, self.alias_b)
        self.paths = Paths(config_dir=os.path.join(self.root, "cfg"), state_dir=os.path.join(self.root, "st"))
        # The host was built from an older settings load that named alias A.
        self.host = conn.LiveHostAdapter(self.paths, {"HOME": self.root, "PATH": "/usr/bin:/bin"},
                                         _settings(ProviderSettings(enabled=True, profile_dir=self.alias_a)))

    def test_explicit_snapshot_decides_the_profile_spelling(self):
        ps_b = ProviderSettings(enabled=True, profile_dir=self.alias_b)
        self.assertEqual(self.host.profile_spelling("claude", self.real, ps_b), self.alias_b)
        self.assertEqual(self.host.profile_spelling("claude", self.real), self.alias_a)  # fallback only

    def test_collector_receives_the_snapshot_spelling(self):
        made = {}

        class Fake:
            def __init__(self, **kw):
                made.update(kw)

        mod = types.ModuleType("quotaribbon.providers.claude")
        mod.ClaudeCollector = Fake
        saved = sys.modules.get(mod.__name__)
        sys.modules[mod.__name__] = mod
        try:
            self.host.make_collector("claude", "/bin/claude", self.real,
                                     ps=ProviderSettings(enabled=True, profile_dir=self.alias_b))
        finally:
            if saved is None:
                sys.modules.pop(mod.__name__, None)
            else:
                sys.modules[mod.__name__] = saved
        self.assertEqual(made["profile_spelling"], self.alias_b)


if __name__ == "__main__":
    unittest.main()
