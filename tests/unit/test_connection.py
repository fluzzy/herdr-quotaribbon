"""LiveHostAdapter: Herdr evidence, trusted executable lookup, plugin state, collectors."""

from __future__ import annotations

import os
import sys
import types
import unittest

from quotaribbon import connection as conn
from quotaribbon.paths import Paths
from quotaribbon.types import ProviderSettings, Settings
from tests.helpers.fake_herdr import (
    FakeHerdrServer,
    agent,
    integration,
    plugin_entry,
    short_tempdir,
    write_recording_executable,
)


def _settings(**overrides) -> Settings:
    base = Settings.default()
    provs = dict(base.providers)
    provs.update(overrides)
    return Settings(schema_version=1, providers=provs, display_mode=base.display_mode,
                    interval_seconds=base.interval_seconds, stale_after_seconds=base.stale_after_seconds,
                    hide_values_after_seconds=base.hide_values_after_seconds)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        for d in (".claude", ".codex"):
            os.makedirs(os.path.join(self.home, d))
        self.paths = Paths(config_dir=os.path.join(self.tmp, "cfg"), state_dir=os.path.join(self.tmp, "st"))
        self.server = FakeHerdrServer(os.path.join(self.tmp, "h.sock")).start()

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "HERDR_SOCKET_PATH": self.server.socket_path}
        env.update(extra)
        return env

    def host(self, settings=None, env=None, integration_text=None) -> conn.LiveHostAdapter:
        return conn.LiveHostAdapter(self.paths, env or self.env(), settings or Settings.default(),
                                    integration_text=integration_text)


class EvidenceDefaultProfileTests(Base):
    def profile(self, p: str) -> str:
        return os.path.realpath(os.path.join(self.home, "." + p))

    def test_installed_integration_matches_by_construction(self):
        self.server.integrations = [integration("claude", "current"), integration("codex", "outdated")]
        ev = self.host().evidence("claude", self.profile("claude"))
        self.assertEqual((ev.linked, ev.source, ev.integration_state, ev.profile_correspondence),
                         (True, "integration", "current", "match"))
        ev = self.host().evidence("codex", self.profile("codex"))
        self.assertEqual(ev.profile_correspondence, "match")

    def test_detected_agent_in_any_workspace_is_inferred(self):
        self.server.agents = [agent("codex", "w9:p3")]
        ev = self.host().evidence("codex", self.profile("codex"))
        self.assertEqual((ev.linked, ev.source, ev.agent_detected, ev.profile_correspondence),
                         (True, "agent", True, "inferred"))

    def test_no_integration_and_no_agent_is_not_linked(self):
        self.server.agents = [agent("claude")]
        ev = self.host().evidence("codex", self.profile("codex"))
        self.assertFalse(ev.linked)
        self.assertEqual(ev.profile_correspondence, "unknown")
        self.assertIsNone(ev.error)

    def test_env_profile_counts_as_default(self):
        other = os.path.join(self.tmp, "claude-alt")
        os.makedirs(other)
        self.server.integrations = [integration("claude", "current")]
        host = self.host(env=self.env(CLAUDE_CONFIG_DIR=other))
        ev = host.evidence("claude", os.path.realpath(other))
        self.assertEqual(ev.profile_correspondence, "match")

    def test_unreachable_herdr_is_reported_as_error_not_as_disconnect(self):
        self.server.stop()
        ev = self.host().evidence("claude", self.profile("claude"))
        self.assertEqual(ev.error, "herdr_unreachable")
        self.assertFalse(ev.linked)
        ev = self.host(env={"HOME": self.home, "PATH": "/bin"}).evidence("claude", self.profile("claude"))
        self.assertEqual(ev.error, "herdr_unreachable")

    def test_herdr_error_answer_is_reported(self):
        self.server.errors["integration.list"] = ("internal", "boom")
        ev = self.host().evidence("claude", self.profile("claude"))
        self.assertEqual(ev.error, "herdr_error")


class EvidenceOverrideProfileTests(Base):
    def setUp(self):
        super().setUp()
        self.override = os.path.join(self.tmp, "work-claude")
        os.makedirs(self.override)
        self.settings = _settings(claude=ProviderSettings(enabled=True, profile_dir=self.override))
        self.seen: list = []

    def text(self, value):
        def fn(env, extra):
            self.seen.append(dict(extra))
            return value
        return fn

    def test_integration_status_runs_with_selected_profile_and_matches(self):
        host = self.host(self.settings, integration_text=self.text("pi: not installed (/x)\nclaude: current (v11) (/p)\n"))
        ev = host.evidence("claude", self.override)
        self.assertEqual(ev.profile_correspondence, "match")
        self.assertEqual(self.seen, [{"CLAUDE_CONFIG_DIR": self.override}])

    def test_not_installed_while_server_profile_has_it_is_a_mismatch(self):
        self.server.integrations = [integration("claude", "current")]
        host = self.host(self.settings, integration_text=self.text("claude: not installed (/p)\n"))
        ev = host.evidence("claude", self.override)
        self.assertEqual(ev.profile_correspondence, "mismatch")

    def test_unparsable_status_is_unknown_and_agents_do_not_count(self):
        self.server.agents = [agent("claude")]
        for text in (None, "garbage\n", "claude current\n"):
            with self.subTest(text=text):
                host = self.host(self.settings, integration_text=self.text(text))
                ev = host.evidence("claude", self.override)
                self.assertEqual(ev.profile_correspondence, "unknown")


class ExecutableTests(Base):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.tmp, "ran.log")
        self.good = os.path.join(self.tmp, "good-bin")
        write_recording_executable(self.good, "claude", self.record)

    def test_trusted_lookup_skips_relative_empty_cwd_pwd_and_pane_dirs(self):
        cwd = os.path.join(self.tmp, "repo")
        pane = os.path.join(self.tmp, "pane")
        pwd = os.path.join(self.tmp, "pwd")
        for d in (cwd, pane, pwd):
            write_recording_executable(d, "claude", self.record)
            write_recording_executable(os.path.join(d, "bin"), "claude", self.record)
        path = os.pathsep.join(["", ".", "bin", cwd, os.path.join(cwd, "bin"), pane, os.path.join(pane, "bin"),
                                pwd, self.good])
        env = self.env(PATH=path, HERDR_ACTIVE_PANE_CWD=pane, PWD=pwd)
        old = os.getcwd()
        os.chdir(cwd)
        try:
            found = conn.trusted_path_lookup("claude", env)
        finally:
            os.chdir(old)
        self.assertEqual(found, os.path.join(self.good, "claude"))
        self.assertFalse(os.path.exists(self.record), "lookup must never execute anything")

    def test_world_writable_and_non_executable_entries_are_skipped(self):
        ww = os.path.join(self.tmp, "ww")
        write_recording_executable(ww, "claude", self.record)
        os.chmod(ww, 0o777)
        noexec = os.path.join(self.tmp, "noexec")
        os.makedirs(noexec)
        with open(os.path.join(noexec, "claude"), "w") as fh:
            fh.write("x")
        env = self.env(PATH=os.pathsep.join([ww, noexec, self.good]))
        self.assertEqual(conn.trusted_path_lookup("claude", env, caller_cwd="/"), os.path.join(self.good, "claude"))

    def test_home_as_pane_cwd_still_allows_user_bin_dirs(self):
        local = os.path.join(self.home, ".local", "bin")
        write_recording_executable(local, "claude", self.record)
        env = self.env(PATH=local, HERDR_ACTIVE_PANE_CWD=self.home)
        self.assertEqual(conn.trusted_path_lookup("claude", env, caller_cwd="/"), os.path.join(local, "claude"))
        env = self.env(PATH=self.home, HERDR_ACTIVE_PANE_CWD=self.home)
        write_recording_executable(self.home, "claude", self.record)
        self.assertIsNone(conn.trusted_path_lookup("claude", env, caller_cwd="/"))

    def test_binary_setting_must_be_absolute_executable(self):
        host = self.host(env=self.env(PATH=self.good))
        exe = os.path.join(self.good, "claude")
        self.assertEqual(host.resolve_executable("claude", ProviderSettings(enabled=True, binary=exe)), exe)
        missing = os.path.join(self.tmp, "nope", "claude")
        self.assertIsNone(host.resolve_executable("claude", ProviderSettings(enabled=True, binary=missing)))
        self.assertEqual(host.resolve_executable("claude", ProviderSettings(enabled=True)), exe)
        self.assertIsNone(host.resolve_executable("codex", ProviderSettings(enabled=True)))

    def test_replaceable_executables_are_rejected_by_lookup_and_binary_setting(self):
        # A world-writable file inside an ordinary 0755 directory.
        loose = os.path.join(self.tmp, "loose")
        os.makedirs(loose)
        exe = os.path.join(loose, "claude")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o777)
        # An ordinary file below a world-writable, non-sticky ancestor.
        shared = os.path.join(self.tmp, "shared")
        nested = os.path.join(shared, "opt", "bin")
        write_recording_executable(nested, "claude", self.record)
        os.chmod(shared, 0o777)
        env = self.env(PATH=os.pathsep.join([loose, nested, self.good]))
        self.assertEqual(conn.trusted_path_lookup("claude", env, caller_cwd="/"), os.path.join(self.good, "claude"))
        host = self.host(env=env)
        for bad in (exe, os.path.join(nested, "claude")):
            with self.subTest(binary=bad):
                self.assertIsNone(host.resolve_executable("claude", ProviderSettings(enabled=True, binary=bad)))
        os.chmod(shared, 0o1777)  # a sticky ancestor (like /tmp) is fine
        self.assertEqual(host.resolve_executable("claude", ProviderSettings(
            enabled=True, binary=os.path.join(nested, "claude"))), os.path.join(nested, "claude"))
        self.assertFalse(os.path.exists(self.record))

    def test_installed_cli_without_herdr_evidence_never_runs(self):
        host = self.host(env=self.env(PATH=self.good))
        ev = host.evidence("claude", os.path.realpath(os.path.join(self.home, ".claude")))
        self.assertFalse(ev.linked)
        self.assertIsNotNone(host.resolve_executable("claude", ProviderSettings(enabled=True)))
        host.plugin_active()
        self.assertFalse(os.path.exists(self.record))


class PluginActiveTests(Base):
    def test_enabled_plugin_is_active(self):
        self.server.plugins = [plugin_entry("/x")]
        self.assertTrue(self.host().plugin_active())

    def test_disabled_missing_unreachable_or_malformed_is_inactive(self):
        self.server.plugins = [plugin_entry("/x", enabled=False)]
        self.assertFalse(self.host().plugin_active())
        self.server.plugins = [plugin_entry("/x", plugin_id="someone.else")]
        self.assertFalse(self.host().plugin_active())
        self.server.plugins = [dict(plugin_entry("/x"), enabled="true")]
        self.assertFalse(self.host().plugin_active())
        self.server.stop()
        self.assertFalse(self.host().plugin_active())


class CollectorFactoryTests(Base):
    def test_make_collector_passes_profile_work_dir_salt_and_lock_fd(self):
        made = {}

        class Fake:
            def __init__(self, **kw):
                made.update(kw)

        claude_mod = types.ModuleType("quotaribbon.providers.claude")
        claude_mod.ClaudeCollector = Fake
        codex_mod = types.ModuleType("quotaribbon.providers.codex")
        codex_mod.CodexCollector = Fake
        saved = {k: sys.modules.get(k) for k in (claude_mod.__name__, codex_mod.__name__)}
        sys.modules[claude_mod.__name__] = claude_mod
        sys.modules[codex_mod.__name__] = codex_mod
        try:
            host = self.host()
            host.make_collector("claude", "/bin/claude", "/prof", lock_fd=7)
            self.assertEqual(made["work_dir"], self.paths.work_dir("claude"))
            from quotaribbon import cache

            self.assertEqual(made["salt"], cache.read_salt(self.paths))
            self.assertEqual(len(made["salt"]), 32)
            self.assertEqual(made["lock_fd"], 7)
            self.assertEqual((made["executable"], made["profile_dir"]), ("/bin/claude", "/prof"))
            self.assertIsNone(made["profile_spelling"])
            made.clear()
            host.make_collector("codex", "/bin/codex", "/p2")
            self.assertIsNone(made["lock_fd"])
            self.assertEqual(made["work_dir"], self.paths.work_dir("codex"))
            # A profile_dir setting is passed in the user's own spelling.
            work = os.path.join(self.home, "work")
            os.makedirs(work)
            link = os.path.join(self.home, "work-link")
            os.symlink(work, link)
            host = self.host(settings=_settings(claude=ProviderSettings(enabled=True, profile_dir=link + "/")))
            host.make_collector("claude", "/bin/claude", os.path.realpath(work))
            self.assertEqual(made["profile_spelling"], link + "/")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_profile_and_config_path_delegate_to_binding(self):
        host = self.host(env=self.env(HERDR_CONFIG_PATH="/cfg/herdr.toml"))
        self.assertEqual(host.herdr_config_path(), "/cfg/herdr.toml")
        self.assertEqual(host.resolve_profile("codex", ProviderSettings(enabled=True)),
                         os.path.realpath(os.path.join(self.home, ".codex")))


if __name__ == "__main__":
    unittest.main()
