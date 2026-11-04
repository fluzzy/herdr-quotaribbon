"""CLI subcommands: toggles, refresh, startup, diagnostics, setup and detach flows."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import tomllib
import unittest
from unittest import mock

from quotaribbon import cache, cli, scheduler
from quotaribbon import refresh as refresh_mod
from quotaribbon import setup_config as sc
from quotaribbon.connection import LiveHostAdapter
from quotaribbon.paths import Paths
from quotaribbon.refresh import ProviderOutcome, RefreshReport
from quotaribbon.types import Settings
from tests.helpers.fake_herdr import (
    PLUGIN_ID,
    FakeHerdrServer,
    plugin_entry,
    short_tempdir,
    write_fake_herdr_cli,
    write_recording_executable,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.realpath(sys.executable)
# What setup pins: a Homebrew keg interpreter is recorded by its upgrade-stable opt/ path.
PINNED_PYTHON = cli.stable_python_path(PYTHON)


class CliBase(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, ".codex"))
        self.cfg_dir = os.path.join(self.tmp, "plugin-config")
        self.state_dir = os.path.join(self.tmp, "plugin-state")
        self.server = FakeHerdrServer(os.path.join(self.tmp, "h.sock")).start()
        self.server.plugins = [plugin_entry(REPO_ROOT)]
        self.bin = os.path.join(self.tmp, "bin")
        self.herdr_log = os.path.join(self.tmp, "herdr-calls.jsonl")
        self.herdr = write_fake_herdr_cli(self.bin, log=self.herdr_log,
                                          config_path=os.path.join(self.tmp, "default-config.toml"))
        self.provider_record = os.path.join(self.tmp, "provider-ran.log")
        self.config = os.path.join(self.tmp, "herdr", "config.toml")
        os.makedirs(os.path.dirname(self.config))
        self.paths = Paths(config_dir=self.cfg_dir, state_dir=self.state_dir)

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def env(self, **extra) -> dict:
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "HERDR_SOCKET_PATH": self.server.socket_path,
               "HERDR_BIN_PATH": self.herdr, "HERDR_PLUGIN_CONFIG_DIR": self.cfg_dir,
               "HERDR_PLUGIN_STATE_DIR": self.state_dir}
        env.update(extra)
        return env

    def run_cli(self, *argv, env=None, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            r, w = os.pipe()
            os.write(w, stdin.encode())
            os.close(w)
            sys.stdin = os.fdopen(r)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv), env=env or self.env())
        finally:
            if stdin is not None:
                sys.stdin.close()
                sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()

    def write_settings(self, obj) -> None:
        os.makedirs(self.cfg_dir, exist_ok=True)
        with open(os.path.join(self.cfg_dir, "settings.json"), "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def read_settings(self) -> dict:
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            return json.load(fh)

    def write_config(self, text: str) -> None:
        with open(self.config, "w") as fh:
            fh.write(text)

    def read_config(self) -> str | None:
        if not os.path.exists(self.config):
            return None
        with open(self.config) as fh:
            return fh.read()

    def herdr_calls(self) -> list[list[str]]:
        if not os.path.exists(self.herdr_log):
            return []
        with open(self.herdr_log) as fh:
            return [json.loads(line)["argv"] for line in fh]


class DispatchTests(CliBase):
    def test_usage_errors_exit_2(self):
        self.assertEqual(self.run_cli()[0], 2)
        code, _out, err = self.run_cli("no-such-thing")
        self.assertEqual(code, 2)
        self.assertIn("unknown subcommand", err)
        self.assertEqual(self.run_cli("claude-on", "extra")[0], 2)
        self.assertEqual(self.run_cli("setup", "--bogus")[0], 2)
        self.assertEqual(self.run_cli("detach", "--herdr-config", self.config, "--yes", "--dry-run")[0], 2)

    def test_toggles_print_one_status_line(self):
        code, out, _ = self.run_cli("claude-off")
        self.assertEqual((code, out), (0, "QuotaRibbon: Claude OFF\n"))
        code, out, _ = self.run_cli("claude-off")
        self.assertEqual(out, "QuotaRibbon: Claude OFF (unchanged)\n")
        code, out, _ = self.run_cli("codex-on")
        self.assertEqual(out, "QuotaRibbon: Codex ON (unchanged)\n")
        self.assertFalse(self.read_settings()["providers"]["claude"]["enabled"])
        self.assertTrue(self.read_settings()["providers"]["codex"]["enabled"])

    def test_toggle_refusal_exits_1_and_leaves_the_file_alone(self):
        self.write_settings("{broken")
        code, out, _ = self.run_cli("codex-off")
        self.assertEqual(code, 1)
        self.assertIn("not changed", out)
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            self.assertEqual(fh.read(), "{broken")

    def test_startup_rotates_the_epoch_and_bumps_directly_edited_providers(self):
        self.run_cli("claude-on")
        gen_before = cache.read_control(self.paths)
        epoch_before = cache.read_server_epoch(self.paths)
        settings = Settings.default().to_json()
        settings["providers"]["codex"]["binary"] = "/opt/codex"
        self.write_settings(settings)
        code, out, _ = self.run_cli("startup")
        self.assertEqual(code, 0)
        self.assertNotEqual(cache.read_server_epoch(self.paths), epoch_before)
        gens = cache.read_control(self.paths)
        self.assertEqual(gens["claude"], gen_before["claude"])
        self.assertGreater(gens["codex"], gen_before["codex"])
        with open(self.paths.settings_last_good) as fh:
            self.assertEqual(json.load(fh)["providers"]["codex"]["binary"], "/opt/codex")

    def test_unexpected_errors_are_reported_without_tracebacks(self):
        def boom(*a, **k):
            raise RuntimeError("token=sk-SECRET")

        with mock.patch.object(cache, "rotate_server_epoch", boom):
            code, out, err = self.run_cli("startup")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn("SECRET", out + err)


class IntervalCliTests(CliBase):
    def test_interval_sets_the_refresh_interval(self):
        code, out, _ = self.run_cli("interval", "300")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "QuotaRibbon: refresh every 5 min; values turn stale after 10 min "
                              "and are hidden after 15 min\n")
        self.assertEqual(self.read_settings()["refresh"], {"interval_seconds": 300})
        code, out, _ = self.run_cli("interval", "300")
        self.assertEqual(code, 0)
        self.assertIn("refresh every 5 min (unchanged)", out)

    def test_interval_reports_reset_custom_timings(self):
        self.write_settings({"schema_version": 1, "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}},
                             "refresh": {"interval_seconds": 120, "stale_after_seconds": 400,
                                         "hide_values_after_seconds": 1000}})
        code, out, _ = self.run_cli("interval", "600")
        self.assertEqual(code, 0, out)
        self.assertIn("stale after 20 min and are hidden after 30 min", out)
        self.assertIn("did not fit the new interval", out)

    def test_interval_usage_errors_exit_2_and_change_nothing(self):
        for argv in (("interval", "abc"), ("interval", "300", "600"), ("interval", "30"), ("interval", "901"),
                     ("interval", "-300"), ("interval", "3_00"), ("interval", "")):
            with self.subTest(argv):
                code, _out, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("quotaribbon interval:", err)
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, "settings.json")))

    def test_interval_refuses_an_invalid_settings_file(self):
        self.write_settings("{broken")
        code, out, _ = self.run_cli("interval", "300")
        self.assertEqual(code, 1)
        self.assertIn("not changed", out)
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            self.assertEqual(fh.read(), "{broken")

    def test_interval_action_opens_the_pane_or_explains_the_cli_form(self):
        code, out, _ = self.run_cli("interval", env=self.env(HERDR_PLUGIN_ID=PLUGIN_ID))
        self.assertEqual(code, 0, out)
        self.assertIn(["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "interval-ui"], self.herdr_calls())
        busy = write_fake_herdr_cli(os.path.join(self.tmp, "busy"), pane_error="ui_busy")
        code, out, _ = self.run_cli("interval", env=self.env(HERDR_PLUGIN_ID=PLUGIN_ID, HERDR_BIN_PATH=busy))
        self.assertEqual(code, 1)
        self.assertIn("ui_busy", out)
        self.assertIn("interval 300", out)

    def test_interactive_interval_choice(self):
        code, out, _ = self.run_cli("interval-ui", stdin="3\n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Current: every 2 min", out)
        self.assertIn("2) every 2 min (default)", out)
        self.assertIn("refresh every 5 min", out)
        self.assertEqual(self.read_settings()["refresh"], {"interval_seconds": 300})

    def test_interactive_interval_cancel_and_bad_choice_change_nothing(self):
        code, out, _ = self.run_cli("interval-ui", stdin="\n\n")
        self.assertEqual(code, 0)
        self.assertIn("Nothing changed.", out)
        code, out, _ = self.run_cli("interval-ui", stdin="9\n\n")
        self.assertEqual(code, 1)
        self.assertIn("not one of the choices", out)
        code, out, _ = self.run_cli("interval-ui", stdin="")
        self.assertIn("Nothing changed.", out)
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, "settings.json")))

    def test_interactive_interval_non_ascii_digit_is_a_bad_choice(self):
        code, out, _ = self.run_cli("interval-ui", stdin="\u00b2\n\n")
        self.assertEqual(code, 1, out)
        self.assertIn("not one of the choices", out)
        self.assertNotIn("unexpected error", out)
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, "settings.json")))


class RefreshTests(CliBase):
    """The CLI layer of ``refresh``: manual vs scheduled, lease id, real host adapter."""

    def run_refresh(self, env):
        calls = []

        def fake_run(paths, host, *, manual, now_fn=time.time, lease_id=None):
            calls.append((manual, lease_id, host))
            return RefreshReport(outcomes=[ProviderOutcome("claude", "published"),
                                           ProviderOutcome("codex", "not_connected", "no_evidence")], manual=manual)

        with mock.patch.object(refresh_mod, "run", fake_run):
            code, out, _ = self.run_cli("refresh", env=env)
        return code, out, calls

    def test_tabbar_invocation_with_the_held_lease_is_scheduled(self):
        lease = scheduler.acquire_dispatch_at(self.state_dir, time.time())
        ctx = json.dumps({"invocation_source": "quotaribbon-tabbar", "correlation_id": lease})
        code, out, calls = self.run_refresh(self.env(HERDR_PLUGIN_CONTEXT_JSON=ctx))
        self.assertEqual(code, 0)
        manual, lease_id, host = calls[0]
        self.assertEqual((manual, lease_id), (False, lease))
        self.assertIsInstance(host, LiveHostAdapter)
        self.assertEqual(out, "refresh (scheduled): claude=published codex=not_connected(no_evidence)\n")

    def test_forged_or_stale_lease_ids_are_manual(self):
        scheduler.acquire_dispatch_at(self.state_dir, time.time())
        ctx = json.dumps({"invocation_source": "quotaribbon-tabbar", "correlation_id": "not-the-lease"})
        _code, _out, calls = self.run_refresh(self.env(HERDR_PLUGIN_CONTEXT_JSON=ctx))
        self.assertEqual(calls[0][:2], (True, None))

    def test_user_invocation_is_manual_and_never_touches_the_lease(self):
        ctx = json.dumps({"invocation_source": "cli", "correlation_id": "cli:plugin"})
        code, out, calls = self.run_refresh(self.env(HERDR_PLUGIN_CONTEXT_JSON=ctx))
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][:2], (True, None))
        self.assertIn("manual", out)

    def test_unusable_settings_collect_nothing(self):
        self.write_settings("{broken")
        code, _out, calls = self.run_refresh(self.env())
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])


class DiagnosticsCliTests(CliBase):
    def test_diagnostics_prints_report_and_opens_the_pane(self):
        code, out, _ = self.run_cli("diagnostics", env=self.env(HERDR_PLUGIN_ID=PLUGIN_ID))
        self.assertEqual(code, 0)
        self.assertIn("QuotaRibbon diagnostics", out)
        self.assertIn("opened the diagnostics pane", out)
        self.assertIn(["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "diagnostics-ui"],
                      self.herdr_calls())

    def test_corrupt_state_and_config_files_do_not_crash_or_show_numbers(self):
        os.makedirs(self.cfg_dir)
        os.makedirs(self.state_dir)
        for path in (os.path.join(self.cfg_dir, "bootstrap.json"), os.path.join(self.state_dir, "clock.json"),
                     os.path.join(self.state_dir, "setup.json")):
            with open(path, "wb") as fh:
                fh.write(b"\x00garbage{")
        self.write_config("[ui\n")
        code, out, _ = self.run_cli("diagnostics-ui", stdin="\n",
                                    env=self.env(HERDR_CONFIG_PATH=self.config))
        self.assertEqual(code, 0)
        self.assertNotIn("%", out)
        code, out, _ = self.run_cli("tabbar-preview")
        self.assertEqual((code, out), (0, "\n"))


class SetupCliTests(CliBase):
    def setup_args(self, *extra):
        return ("setup", "--herdr-config", self.config, *extra)

    def test_dry_run_changes_nothing(self):
        self.write_config("[ui]\n")
        code, out, _ = self.run_cli(*self.setup_args("--dry-run"))
        self.assertEqual(code, 0)
        self.assertIn("+tab_bar_right = [", out)
        self.assertIn("Dry run", out)
        self.assertEqual(self.read_config(), "[ui]\n")
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, "tabbar.py")))

    def test_apply_installs_bootstrap_settings_record_and_entry_once(self):
        self.write_config('# mine\n[theme]\nname = "x"\n')
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload"))
        self.assertEqual(code, 0, out)
        with open(os.path.join(REPO_ROOT, "bootstrap", "tabbar.py"), "rb") as a, \
                open(os.path.join(self.cfg_dir, "tabbar.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(self.cfg_dir, "bootstrap.json")) as fh:
            boot = json.load(fh)
        self.assertEqual(boot, {"schema_version": 1, "bootstrap_version": 1, "plugin_id": PLUGIN_ID,
                                "state_dir": self.state_dir, "config_dir": self.cfg_dir, "python": PINNED_PYTHON})
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            self.assertEqual(json.load(fh), Settings.default().to_file_json())
        entry = sc.build_entry(PINNED_PYTHON, os.path.join(self.cfg_dir, "tabbar.py"))
        doc = tomllib.loads(self.read_config())
        self.assertEqual(doc["ui"]["tab_bar_right"], [entry])
        self.assertTrue(self.read_config().startswith('# mine\n[theme]\nname = "x"\n'))
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "setup.json")))
        self.assertEqual(len(os.listdir(os.path.join(self.state_dir, "backups"))), 1)
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload"))
        self.assertEqual(code, 0)
        self.assertIn("already present", out)
        self.assertEqual(tomllib.loads(self.read_config())["ui"]["tab_bar_right"], [entry])

    def test_saved_off_setting_is_never_flipped_on(self):
        os.makedirs(self.cfg_dir)
        saved = Settings.default().to_json()
        saved["providers"]["claude"]["enabled"] = False
        with open(os.path.join(self.cfg_dir, "settings.json"), "w") as fh:
            json.dump(saved, fh)
        self.assertEqual(self.run_cli(*self.setup_args("--yes", "--no-reload"))[0], 0)
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            self.assertFalse(json.load(fh)["providers"]["claude"]["enabled"])

    def test_unsupported_config_is_refused_with_snippet(self):
        self.write_config('ui.tab_bar_position = "top"\n')
        code, out, _ = self.run_cli(*self.setup_args("--yes"))
        self.assertEqual(code, 1)
        self.assertIn("Manual setup", out)
        self.assertEqual(self.read_config(), 'ui.tab_bar_position = "top"\n')

    def test_reload_status_is_checked_after_apply(self):
        self.write_config("[ui]\n")
        code, out, _ = self.run_cli(*self.setup_args("--yes"))
        self.assertEqual(code, 0)
        self.assertIn("applied", out)
        self.assertEqual(len(self.server.requests_for("server.reload_config")), 1)

    def test_failed_reload_is_reported_with_undo_command(self):
        self.write_config("[ui]\n")
        self.server.reload_status = "failed"
        self.server.reload_diagnostics = ['line 3: command = "curl -H token-SHOULD-NOT-APPEAR"']
        code, out, _ = self.run_cli(*self.setup_args("--yes"))
        self.assertEqual(code, 1)
        self.assertNotIn("SHOULD-NOT-APPEAR", out)
        self.assertIn("1 diagnostic", out)
        self.assertIn("failed", out)
        self.assertIn("detach --herdr-config", out)

    def test_pin_cli_records_resolved_paths(self):
        exe = write_recording_executable(self.bin, "claude", self.provider_record)
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload", "--pin-cli"))
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.cfg_dir, "settings.json")) as fh:
            provs = json.load(fh)["providers"]
        self.assertEqual(provs["claude"]["binary"], exe)
        self.assertNotIn("binary", provs["codex"])

    def fake_versioned_cli(self, name: str) -> str:
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(f"#!/bin/sh\necho \"$0 $*\" >> '{self.provider_record}'\n"
                     "echo '2.1.316 (Claude Code) token=sk-ant-SHOULD-NOT-APPEAR'\n")
        os.chmod(path, 0o755)
        return path

    def provider_runs(self) -> list[str]:
        if not os.path.exists(self.provider_record):
            return []
        with open(self.provider_record) as fh:
            return fh.read().splitlines()

    def test_preview_shows_the_recorded_version_and_never_starts_a_provider(self):
        from tests.helpers.state_seed import seed_connected

        self.fake_versioned_cli("claude")
        self.write_config("[ui]\n")
        seed_connected(self.paths, self.env(), providers=("claude",))  # records cli_version 1.0.0
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload"))
        self.assertEqual(code, 0, out)
        self.assertIn("(1.0.0)", out)
        self.assertNotIn("SHOULD-NOT-APPEAR", out)
        self.assertEqual(self.provider_runs(), [])

    def test_setup_never_starts_a_provider_that_is_off(self):
        self.fake_versioned_cli("claude")
        self.write_config("[ui]\n")
        settings = Settings.default().to_json()
        settings["providers"]["claude"]["enabled"] = False
        self.write_settings(settings)
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.provider_runs(), [])

    def test_setup_probe_is_refused_when_the_plugin_is_disabled_in_herdr(self):
        self.fake_versioned_cli("claude")
        self.write_config("[ui]\n")
        self.server.plugins = [plugin_entry(REPO_ROOT, enabled=False)]
        code, out, _ = self.run_cli(*self.setup_args("--yes", "--no-reload"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.provider_runs(), [])

    def test_shim_or_old_interpreters_are_refused(self):
        self.assertIsNone(cli.check_python(os.path.join(self.tmp, ".pyenv", "shims", "python3"))[0])
        self.assertIsNone(cli.check_python(PYTHON, (3, 9, 6))[0])
        script = os.path.join(self.tmp, "python3")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nexec python3 \"$@\"\n")
        os.chmod(script, 0o755)
        self.assertIsNone(cli.check_python(script)[0])
        self.assertEqual(cli.check_python(PYTHON)[0], PINNED_PYTHON)

    def test_an_interpreter_others_could_replace_is_never_pinned(self):
        from unittest import mock

        with mock.patch("quotaribbon.trust.trusted_executable", return_value=False):
            real, why = cli.check_python(PYTHON)
        self.assertIsNone(real)
        self.assertIn("another user", why)

    def test_setup_action_opens_the_pane_or_explains_the_cli_form(self):
        code, out, _ = self.run_cli("setup", env=self.env(HERDR_PLUGIN_ID=PLUGIN_ID))
        self.assertEqual(code, 0)
        self.assertIn(["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "setup-ui"], self.herdr_calls())
        busy = write_fake_herdr_cli(os.path.join(self.tmp, "busy"), pane_error="ui_busy")
        code, out, _ = self.run_cli("setup", env=self.env(HERDR_PLUGIN_ID=PLUGIN_ID, HERDR_BIN_PATH=busy))
        self.assertEqual(code, 1)
        self.assertIn("ui_busy", out)
        self.assertIn("setup --herdr-config", out)

    def test_interactive_setup_applies_only_after_yes(self):
        self.write_config("[ui]\n")
        env = self.env(HERDR_CONFIG_PATH=self.config)
        code, out, _ = self.run_cli("setup-ui", env=env, stdin="n\n\n")
        self.assertEqual(code, 0)
        self.assertIn("Nothing changed", out)
        self.assertEqual(self.read_config(), "[ui]\n")
        code, out, _ = self.run_cli("setup-ui", env=env, stdin="y\ny\n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Herdr reloaded the config", out)
        self.assertEqual(len(tomllib.loads(self.read_config())["ui"]["tab_bar_right"]), 1)

    def test_interactive_setup_offers_revert_when_reload_fails(self):
        self.write_config("[ui]\n")
        self.server.reload_status = "partial"
        env = self.env(HERDR_CONFIG_PATH=self.config)
        code, out, _ = self.run_cli("setup-ui", env=env, stdin="y\ny\ny\n\n")
        self.assertEqual(code, 1)
        self.assertIn("Reverted", out)
        self.assertEqual(self.read_config(), "[ui]\n")

    def test_closed_stdin_means_no(self):
        self.write_config("[ui]\n")
        code, out, _ = self.run_cli("setup-ui", env=self.env(HERDR_CONFIG_PATH=self.config), stdin="")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_config(), "[ui]\n")


class DetachCliTests(CliBase):
    def setUp(self):
        super().setUp()
        self.original = '# keep\n[ui]\ntab_bar_right = [\n  { type = "hostname" },\n]\n'
        self.write_config(self.original)
        code, out, _ = self.run_cli("setup", "--herdr-config", self.config, "--yes", "--no-reload")
        self.assertEqual(code, 0, out)

    def detach(self, *extra, **kw):
        return self.run_cli("detach", "--herdr-config", self.config, *extra, **kw)

    def test_dry_run_then_yes_restores_original_bytes(self):
        code, out, _ = self.detach("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("restore the exact bytes", out)
        self.assertNotEqual(self.read_config(), self.original)
        code, out, _ = self.detach("--yes", "--no-reload")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_config(), self.original)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "setup.json")))
        code, out, _ = self.detach("--yes", "--no-reload")
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)

    def test_modified_entry_requires_force(self):
        self.write_config(self.read_config().replace("interval_seconds = 5", "interval_seconds = 10"))
        modified = self.read_config()
        code, out, _ = self.detach("--yes", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("changed: interval_seconds", out)
        self.assertNotIn("interval_seconds = 10", out)
        self.assertEqual(self.read_config(), modified)
        code, out, _ = self.detach("--yes", "--no-reload", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(tomllib.loads(self.read_config())["ui"]["tab_bar_right"], [{"type": "hostname"}])

    def test_interactive_detach(self):
        env = self.env(HERDR_CONFIG_PATH=self.config)
        code, out, _ = self.run_cli("detach-ui", env=env, stdin="y\nn\n\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_config(), self.original)

    def test_detach_reloads_by_default(self):
        code, out, _ = self.detach("--yes")
        self.assertEqual(code, 0)
        self.assertIn("reload: applied", out)


class LauncherSmokeTests(CliBase):
    def test_settings_pin_rejects_invalid_settings(self):
        os.makedirs(self.cfg_dir)
        with open(os.path.join(self.cfg_dir, "settings.json"), "w") as fh:
            fh.write("{broken")
        from quotaribbon.paths import Paths

        err = cli.pin_cli_paths(Paths(self.cfg_dir, self.state_dir), {"claude": "/bin/sh"})
        self.assertIn("unreadable", err)

    def test_profile_override_is_used_for_cli_resolution(self):
        prof = os.path.join(self.tmp, "alt")
        os.makedirs(prof)
        settings = Settings.default().to_json()
        settings["providers"]["claude"]["profile_dir"] = prof
        self.write_settings(settings)
        self.write_config("[ui]\n")
        code, out, _ = self.run_cli("setup", "--herdr-config", self.config, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"profile {prof}", out)


if __name__ == "__main__":
    unittest.main()
