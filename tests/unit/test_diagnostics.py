"""Diagnostics report: content, secret-freedom and robustness against corrupt state."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from quotaribbon import binding, cache, diagnostics
from quotaribbon import settings as settings_mod
from quotaribbon.paths import Paths
from quotaribbon.types import ConnectionRecord, ProviderStatus, Snapshot, Window
from tests.helpers.fake_herdr import (
    FakeHerdrServer,
    integration,
    plugin_entry,
    short_tempdir,
    write_fake_herdr_cli,
    write_recording_executable,
)

CANARY_EMAIL = "canary.person@example.invalid"
CANARY_TOKEN = "sk-ant-api03-CANARYCANARYCANARY0123456789"
NOW = 1_800_000_000.0


def _conn(provider: str, **kw) -> ConnectionRecord:
    base = dict(schema_version=1, provider=provider, binding_key="0" * 24, profile_key="0" * 16, state="connected",
                ever_connected=True, evidence={"source": "integration", "integration_state": "current",
                                               "observed_at": NOW - 10},
                executable="/opt/bin/" + provider, cli_version="2.1.316", account_key="acct:" + "1" * 24,
                auth_epoch=1, verified_at=NOW - 5, verified_server_epoch="e1", verified_generation=0,
                next_verify_at=NOW + 300, last_reason=None, env_conflicts=["ANTHROPIC_API_KEY"])
    base.update(kw)
    return ConnectionRecord(**base)


def _status(provider: str, **kw) -> ProviderStatus:
    base = dict(schema_version=1, provider=provider, data_state="ok", attempt_started_at=NOW - 20,
                attempt_finished_at=NOW - 19, last_success_at=NOW - 19, last_failure_at=None,
                consecutive_failures=0, next_due_at=NOW + 100, retry_not_before=None, last_error_code=None,
                unsupported_cli_version=None, notes=["window_not_provided:five_hour"])
    base.update(kw)
    return ProviderStatus(**base)


def _snap(provider: str, profile_key: str = "0" * 16, server_epoch: str = "e1") -> Snapshot:
    return Snapshot(schema_version=1, provider=provider, scope_key="s", profile_key=profile_key,
                    account_key="acct:" + "1" * 24, auth_epoch=1,
                    source={"claude": "claude-control-get-usage", "codex": "codex-app-server-rate-limits"}[provider],
                    fetched_at="2027-01-15T08:00:00.000000Z", source_observed_at=None, status="ok",
                    windows=[Window("five_hour", "all", 18000, None, None, None),
                             Window("seven_day", "all", 604800, 73.0, 73, "2027-01-20T02:00:00Z")],
                    cli_version="2.1.316", server_epoch=server_epoch)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, ".codex"))
        self.paths = Paths(config_dir=os.path.join(self.tmp, "cfg"), state_dir=os.path.join(self.tmp, "st"))
        self.paths.ensure_private_dirs()
        self.server = FakeHerdrServer(os.path.join(self.tmp, "h.sock")).start()
        self.server.plugins = [plugin_entry(self.tmp)]
        self.server.integrations = [integration("claude", "current"), integration("codex", "not_installed")]
        self.bin = os.path.join(self.tmp, "bin")
        self.cfg_toml = os.path.join(self.tmp, "config.toml")
        write_fake_herdr_cli(self.bin, config_path=self.cfg_toml)
        self.record = os.path.join(self.tmp, "provider-ran.log")
        write_recording_executable(self.bin, "claude", self.record)
        write_recording_executable(self.bin, "codex", self.record)
        self.write_records()
        cache.write_json_atomic(self.paths.dispatch, {"id": "lease-1", "dispatched_at": NOW - 3, "finished_at": None})

    def bindings(self):
        return {p: binding.binding_for(p, settings_mod.DEFAULT_SETTINGS, self.env()) for p in ("claude", "codex")}

    def write_records(self, claude_status=None, claude_snapshot=True):
        epoch = cache.rotate_server_epoch(self.paths, now=NOW - 100)
        gens = cache.sync_fingerprints(self.paths, settings_mod.settings_fingerprints(settings_mod.DEFAULT_SETTINGS))
        path, ident = binding.server_identity(self.env())
        b = self.bindings()
        for p in b:
            self.paths.ensure_binding_dir(p, b[p].binding_key)
        common = dict(verified_server_epoch=epoch, verified_servers={path: ident})
        cache.write_connection(self.paths, _conn("claude", binding_key=b["claude"].binding_key,
                                                 profile_key=b["claude"].profile_key, verified_generation=gens["claude"],
                                                 last_reason=f"{CANARY_EMAIL} token={CANARY_TOKEN}", **common))
        cache.write_status(self.paths, claude_status or _status("claude"), b["claude"].binding_key)
        if claude_snapshot:
            cache.write_snapshot(self.paths, _snap("claude", profile_key=b["claude"].profile_key, server_epoch=epoch),
                                 b["claude"].binding_key)
        cache.write_connection(self.paths, _conn("codex", binding_key=b["codex"].binding_key,
                                                 profile_key=b["codex"].profile_key, state="not_connected",
                                                 ever_connected=False, evidence=None, env_conflicts=[], **common))

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def env(self) -> dict:
        return {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "HERDR_SOCKET_PATH": self.server.socket_path,
                "HERDR_BIN_PATH": os.path.join(self.bin, "herdr"), "HERDR_CONFIG_PATH": self.cfg_toml,
                "ANTHROPIC_API_KEY": CANARY_TOKEN, "SOME_EMAIL": CANARY_EMAIL}

    def build(self, **kw) -> dict:
        return diagnostics.build_report(self.paths, self.env(), now=NOW, **kw)

    def test_report_covers_providers_settings_and_tab_bar(self):
        with open(self.cfg_toml, "w") as fh:
            fh.write('[ui]\nhide_tab_bar_when_single_tab = true\ntab_bar_position = "bottom"\n')
        r = self.build()
        c = r["providers"]["claude"]
        self.assertTrue(c["enabled"])
        self.assertEqual(c["connection_state"], "connected")
        self.assertEqual(c["live_evidence"]["correspondence"], "match")
        self.assertEqual(c["env_conflicts"], ["ANTHROPIC_API_KEY"])
        self.assertEqual(c["cli_version"], "2.1.316")
        self.assertEqual(c["windows_not_provided"], ["five_hour"])
        self.assertEqual([w["label"] for w in c["windows"]], ["5h", "7d"])
        self.assertEqual([w["provided"] for w in c["windows"]], [False, True])
        self.assertEqual(c["next_retry_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 100)))
        x = r["providers"]["codex"]
        self.assertEqual(x["hidden_reason"], "never_connected")
        self.assertEqual(x["display"], "(hidden)")
        self.assertTrue(c["verified_in_this_server"])
        self.assertEqual(c["display"], "Claude 5h -- | 7d 73% (4d)")
        self.assertEqual(r["tab_bar_line"], "Claude 5h -- | 7d 73% (4d)")
        self.assertEqual(x["live_evidence"]["correspondence"], "unknown")
        self.assertIn("profile_unverified", x["hint"])
        self.assertEqual(r["herdr"]["version"], "0.9.5")
        self.assertEqual(r["herdr"]["tab_bar_entry"], "missing (run setup)")
        self.assertTrue(r["herdr"]["ui"]["hide_tab_bar_when_single_tab"])
        self.assertEqual(r["herdr"]["plugin_registered"], "enabled")
        self.assertTrue(r["dispatch"]["held"])
        text = diagnostics.format_report(r)
        for needle in ("[Claude]", "[Codex]", "hide_tab_bar_when_single_tab: yes", "tab_bar_position: bottom"):
            self.assertIn(needle, text)

    def test_report_contains_no_emails_tokens_or_env_values(self):
        text = json.dumps(self.build()) + diagnostics.format_report(self.build())
        self.assertNotIn(CANARY_EMAIL, text)
        self.assertNotIn(CANARY_TOKEN, text)
        self.assertNotIn("acct:", text)
        self.assertNotIn("canary", text.lower())

    def test_launcher_state_is_reported(self):
        from quotaribbon import setup_config as sc

        self.assertEqual(self.build(live=False)["plugin"]["launcher"], "missing (run setup)")
        boot = {"schema_version": 1, "bootstrap_version": 1, "plugin_id": "x", "state_dir": self.paths.state_dir,
                "config_dir": self.paths.config_dir, "python": "/usr/bin/python3"}
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.bootstrap_json, "w") as fh:
            json.dump(boot, fh)
        sc.install_wrapper("/usr/bin/python3", self.paths.tabbar_py)
        self.assertEqual(self.build(live=False)["plugin"]["launcher"], "ok")
        sc.install_wrapper("/opt/old/python3", self.paths.tabbar_py)
        self.assertEqual(self.build(live=False)["plugin"]["launcher"], "outdated (re-run setup)")

    def test_hostile_config_values_never_reach_the_report(self):
        with open(self.cfg_toml, "w") as fh:
            fh.write(f'[ui]\ntab_bar_position = {{ password = "{CANARY_TOKEN}" }}\n'
                     'hide_tab_bar_when_single_tab = "\\u001b]0;title\\u0007"\n'
                     'tab_bar_right = [{ type = "\\u001b[2J\\u001b[31mspoof" }, { type = "datetime" }]\n')
        r = self.build()
        ui = r["herdr"]["ui"]
        self.assertEqual(ui["tab_bar_position"], "invalid")
        self.assertEqual(ui["hide_tab_bar_when_single_tab"], "invalid")
        self.assertEqual([i["type"] for i in ui["tab_bar_right"]], ["other", "datetime"])
        text = json.dumps(r) + diagnostics.format_report(r)
        for bad in (CANARY_TOKEN, "password", "\x1b", "\\u001b", "spoof", "title"):
            self.assertNotIn(bad, text)
        self.assertIn("tab_bar_position: invalid", diagnostics.format_report(r))

    def test_format_report_sanitizes_every_line_it_emits(self):
        report = {"generated_at": "\x1b]0;x\x07now", "tab_bar_line": "a\nb\rc\x1b[31m",
                  "plugin": {"python": f"/opt/{CANARY_TOKEN}/python", "x" * 50: "y" * 5000},
                  "herdr": {"ui": {"tab_bar_right": [{"type": "\x1b[2Jevil"}], "warnings": ["w\x07"],
                                   "tab_bar_position": {"password": "hunter2"}}},
                  "providers": {"claude": {"name": "Claude", "last_reason": f"token={CANARY_TOKEN}"}}}
        text = diagnostics.format_report(report)
        self.assertNotIn(CANARY_TOKEN, text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("\r", text)
        self.assertFalse(any(ord(ch) < 0x20 and ch != "\n" or 0x7F <= ord(ch) <= 0x9F for ch in text))
        self.assertTrue(all(len(line) <= diagnostics.MAX_LINE for line in text.splitlines()))
        self.assertIn("line: a b c", text)

    def test_diagnostics_never_start_provider_clis(self):
        self.build()
        self.assertFalse(os.path.exists(self.record))

    def test_clock_rollback_and_managed_policy_are_explained(self):
        with open(self.paths.clock, "w") as fh:
            json.dump({"max_wall_seen": NOW + 3600}, fh)
        self.write_records(claude_status=_status("claude", data_state="protocol_unsupported",
                                                 notes=["managed_policy_present"]), claude_snapshot=False)
        r = self.build()
        self.assertTrue(r["clock"]["rollback"])
        self.assertIn("managed policy", r["providers"]["claude"]["managed_policy"])
        self.assertEqual(r["providers"]["claude"]["data_state"], "protocol_unsupported")

    def test_malformed_clock_records_are_reported_as_corrupt(self):
        for content in ('{"max_wall_seen": 1' + "0" * 400 + '}', '{"max_wall_seen": "x"}', '{"other": 1}', "[]"):
            with self.subTest(content=content[:30]):
                with open(self.paths.clock, "w") as fh:
                    fh.write(content)
                r = self.build(live=False)
                self.assertIn("corrupt", r["clock"]["state"])
                diagnostics.format_report(r)

    def test_corrupt_state_files_do_not_crash(self):
        for path in (self.paths.bootstrap_json, self.paths.clock, self.paths.setup_record, self.paths.tabbar_py):
            with open(path, "wb") as fh:
                fh.write(b"\xff{not json")
        with open(self.cfg_toml, "w") as fh:
            fh.write("[ui\n")
        r = self.build()
        self.assertEqual(r["plugin"]["bootstrap_json"], "corrupt")
        self.assertIn("corrupt", r["clock"]["state"])
        self.assertIn("unreadable", r["herdr"]["tab_bar_entry"])
        diagnostics.format_report(r)

    def test_display_lines_follow_the_compact_format(self):
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "display": {"format": "compact"},
                       "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}}, fh)
        r = self.build()
        self.assertTrue(r["settings"]["valid"])
        self.assertEqual(r["providers"]["claude"]["display"], "C 5h -- 7d 73% (4d)")
        self.assertEqual(r["tab_bar_line"], "C 5h -- 7d 73% (4d)")

    def test_invalid_settings_and_unreachable_herdr_are_reported(self):
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, CANARY_EMAIL: True, "providers": {}}, fh)
        self.server.stop()
        r = self.build()
        self.assertFalse(r["settings"]["valid"])
        self.assertEqual(r["herdr"]["plugin_registered"], "herdr unreachable")
        self.assertFalse(r["providers"]["claude"]["enabled"])
        self.assertNotIn(CANARY_EMAIL, json.dumps(r) + diagnostics.format_report(r))
        self.assertEqual(r["tab_bar_line"], "(empty)")

    def test_failures_inside_sections_are_contained(self):
        with mock.patch.object(diagnostics, "_herdr_section", side_effect=RuntimeError("x")):
            r = self.build(live=False)
        self.assertEqual(r["herdr"]["error"], "RuntimeError")
        self.assertIn("claude", r["providers"])


if __name__ == "__main__":
    unittest.main()
