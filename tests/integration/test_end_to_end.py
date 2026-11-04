"""End to end: the real plugin with only Herdr and the provider CLIs faked.

Setup writes the real config entry, the tab bar runs it via ``/bin/sh -lc`` from a hostile
pane cwd, and actions run the manifest's commands. Time passes by aging stored timestamps.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import unittest

from quotaribbon import fsutil
from quotaribbon import types as t
from tests.helpers.fake_herdr import (
    PLUGIN_ID,
    FakeHerdrServer,
    integration,
    link_executable,
    plugin_entry,
    short_tempdir,
    write_fake_herdr_cli,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_BIN = os.path.join(REPO_ROOT, "tests", "fixtures", "bin")
PYTHON = os.path.realpath(sys.executable)
CLAUDE = "Claude 5h 32% | 7d 68%"
CODEX = "Codex 5h 54% | 7d 21%"
SEP = "  ·  "
E = "…"

WRAPPER = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "{provider} $*" >> "$d/starts.log"
QR_FAKE_SCENARIO=${{QR_E2E_{P}_SCENARIO:-ok}} \\
QR_FAKE_ACCOUNT=${{QR_E2E_{P}_ACCOUNT:-a}} \\
QR_FAKE_USAGE_DELAY=${{QR_E2E_{P}_DELAY:-0}} \\
QR_FAKE_LOG="$d/fake-cli.jsonl" \\
exec {python} {fake} "$@"
"""

HOSTILE_INIT = "open(__file__ + '.HIJACKED', 'w').close()\n"

with open(os.path.join(REPO_ROOT, "herdr-plugin.toml"), "rb") as _fh:
    MANIFEST = tomllib.load(_fh)
ACTION_ARGV = {a["id"]: a["command"] for a in MANIFEST["actions"]}
STARTUP_ARGV = MANIFEST["startup"][0]["command"]

_TIME_FIELDS = {
    "connection": ("verified_at", "next_verify_at"),
    "status": ("attempt_started_at", "attempt_finished_at", "last_success_at", "last_failure_at", "next_due_at",
               "retry_not_before"),
}


class EndToEndBase(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        for d in (".claude", ".codex"):
            os.makedirs(os.path.join(self.home, d))
        self.cfg_dir = os.path.join(self.tmp, "cfg")
        self.state_dir = os.path.join(self.tmp, "state")
        self.herdr_config = os.path.join(self.tmp, "herdr", "config.toml")
        os.makedirs(os.path.dirname(self.herdr_config))
        with open(self.herdr_config, "w") as fh:
            fh.write('onboarding = false\n\n[theme]\nname = "dark"\n\n[ui.sound]\nenabled = false\n')
        self.bin = os.path.join(self.tmp, "bin")
        for p in ("claude", "codex"):
            self.assertTrue(os.access(write_wrapper(self.bin, p), os.X_OK))
        self.herdr = write_fake_herdr_cli(self.bin, config_path=self.herdr_config)
        self.server = FakeHerdrServer(os.path.join(self.tmp, "h.sock")).start()
        self.server.plugins = [plugin_entry(REPO_ROOT)]
        self.server.integrations = [integration("claude", "current"), integration("codex", "current")]
        self.fake: dict[str, str] = {}
        self.pane_cwd = make_hostile_cwd(os.path.join(self.tmp, "pane cwd"))
        r = self.launcher("setup", "--herdr-config", self.herdr_config, "--yes", "--no-reload")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.herdr_config, "rb") as fh:
            entries = tomllib.load(fh)["ui"]["tab_bar_right"]
        self.assertEqual(len(entries), 1)
        self.tab_command = entries[0]["command"]

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def server_env(self) -> dict:
        """The Herdr server's environment (inherited by tab-bar commands and actions)."""
        env = {
            "HOME": self.home,
            "PATH": f"{self.bin}:{os.path.dirname(PYTHON)}:/usr/bin:/bin",
            "LANG": "C",
            "HERDR_CONFIG_PATH": self.herdr_config,
            "HERDR_SOCKET_PATH": self.server.socket_path,
            "HERDR_BIN_PATH": self.herdr,
        }
        env.update(self.fake)
        return env

    def action_env(self, action_id: str, context: dict | None = None) -> dict:
        env = self.server_env()
        env.update({
            "HERDR_ENV": "1",
            "HERDR_PLUGIN_ID": PLUGIN_ID,
            "HERDR_PLUGIN_ROOT": REPO_ROOT,
            "HERDR_PLUGIN_CONFIG_DIR": self.cfg_dir,
            "HERDR_PLUGIN_STATE_DIR": self.state_dir,
            "HERDR_PLUGIN_ACTION_ID": action_id,
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context or {"invocation_source": "keybinding"}),
            "HERDR_PANE_ID": "w1:p1",
        })
        return env

    def launcher(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", "bin/quotaribbon", *args], cwd=REPO_ROOT, env=self.action_env("cli"),
                              capture_output=True, text=True, timeout=60)

    def action(self, action_id: str, context: dict | None = None, *, background: bool = False):
        argv = ACTION_ARGV[action_id]
        kw = dict(cwd=REPO_ROOT, env=self.action_env(action_id, context), stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True)
        if background:
            return subprocess.Popen(argv, **kw)
        r = subprocess.run(argv, timeout=90, **kw)
        self.outputs = getattr(self, "outputs", []) + [f"[{action_id}] rc={r.returncode} {r.stdout}{r.stderr}"]
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def output_log(self) -> str:
        """Every action's output so far, for failure messages."""
        return "\n".join(getattr(self, "outputs", []))

    def startup(self) -> None:
        env = self.action_env("startup")
        env["HERDR_PLUGIN_EVENT"] = "startup"
        r = subprocess.run(STARTUP_ARGV, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def tick(self) -> str:
        env = self.server_env()
        env["HERDR_ACTIVE_PANE_CWD"] = self.pane_cwd
        r = subprocess.run(["/bin/sh", "-lc", self.tab_command], cwd=self.pane_cwd, env=env,
                           capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, b"")
        self.assertFalse(glob.glob(os.path.join(self.pane_cwd, "**", "*.HIJACKED"), recursive=True))
        out = r.stdout.decode("utf-8")
        self.assertLessEqual(out.count("\n"), 1)
        return out.rstrip("\n")

    def invocations(self) -> list[dict]:
        return self.server.requests_for("plugin.action.invoke")

    def run_dispatched(self) -> subprocess.CompletedProcess:
        """Run the refresh the tab bar asked Herdr for (with its invocation context)."""
        invs = self.invocations()
        inv = invs[-1]["params"]
        self.assertEqual((inv["plugin_id"], inv["action_id"]), (PLUGIN_ID, "refresh"))
        self.ran_invocations = len(invs)
        return self.action("refresh", inv["context"])

    def tick_and_refresh(self) -> str:
        """Run the refresh the tab bar dispatched, then return the next tab-bar line."""
        self.tick()
        self.assertGreater(len(self.invocations()), getattr(self, "ran_invocations", 0),
                           "the tab bar did not dispatch a refresh")
        out = self.run_dispatched().stdout
        self.assertIn("refresh (scheduled)", out)
        self.last_refresh_output = out
        return self.tick()

    def provider_starts(self, provider: str, *, usage_only: bool = False) -> int:
        """How many ``provider`` CLI processes were started (``usage_only``: usage sessions)."""
        path = os.path.join(self.bin, "starts.log")
        if not os.path.exists(path):
            return 0
        with open(path) as fh:
            lines = [ln.split(" ", 1) + [""] for ln in fh.read().splitlines()]
        session = {"claude": "-p", "codex": "app-server"}[provider]
        return sum(1 for name, args, *_ in lines
                   if name == provider and (not usage_only or args.split(" ", 1)[0] == session))

    def age(self, seconds: float) -> None:
        """Simulate ``seconds`` passing: shift every stored timestamp into the past."""
        for path in glob.glob(os.path.join(self.state_dir, "providers", "*", "*", "*.json")):
            with open(path) as fh:
                rec = json.load(fh)
            kind = os.path.basename(path)[:-5]
            for f in _TIME_FIELDS.get(kind, ()):
                if isinstance(rec.get(f), (int, float)):
                    rec[f] = rec[f] - seconds
            if kind == "connection" and rec.get("evidence"):
                rec["evidence"]["observed_at"] -= seconds
            if kind == "snapshot":
                rec["fetched_at"] = t.iso_utc_precise(t.parse_iso_utc(rec["fetched_at"]) - seconds)
            fsutil.atomic_write_json(path, rec)
        for name, fields in (("dispatch.json", ("dispatched_at", "finished_at")), ("clock.json", ("max_wall_seen",))):
            path = os.path.join(self.state_dir, name)
            if os.path.exists(path):
                with open(path) as fh:
                    rec = json.load(fh)
                for f in fields:
                    if isinstance(rec.get(f), (int, float)):
                        rec[f] -= seconds
                fsutil.atomic_write_json(path, rec)

    def restart_herdr_server(self) -> None:
        """A new Herdr server on the same socket path (the startup hook is NOT run)."""
        plugins, integrations = self.server.plugins, self.server.integrations
        self.server.stop()
        time.sleep(0.01)
        self.server = FakeHerdrServer(self.server.socket_path).start()
        self.server.plugins, self.server.integrations = plugins, integrations
        self.ran_invocations = 0  # the new server has its own request log

    def connect_both(self) -> None:
        """Verify both providers and show their numbers.

        A socket check timing out under load fails the refresh closed; retry after its backoff.
        """
        line = self.tick_and_refresh()
        for _ in range(2):
            if line == CLAUDE + SEP + CODEX:
                return
            self.assertRegex(self.last_refresh_output, r"=(discarded|failed)", self.output_log())
            self.age(1000)
            self.action("refresh")
            line = self.tick()
        self.assertEqual(line, CLAUDE + SEP + CODEX, self.output_log())


def write_wrapper(directory: str, provider: str) -> str:
    content = WRAPPER.format(P=provider.upper(), provider=provider, python=PYTHON,
                             fake=os.path.join(FIXTURE_BIN, f"fake_{provider}.py"))
    return link_executable(directory, provider, content)


def make_hostile_cwd(path: str) -> str:
    """A pane cwd that tries to hijack imports and interpreter lookup."""
    pkg = os.path.join(path, "quotaribbon")
    os.makedirs(pkg)
    for name in ("__init__.py", "tabbar_entry.py", "renderer.py"):
        with open(os.path.join(pkg, name), "w") as fh:
            fh.write(HOSTILE_INIT)
    for name in ("json.py", "os.py", "socket.py"):
        with open(os.path.join(path, name), "w") as fh:
            fh.write(HOSTILE_INIT)
    py = os.path.join(path, "python3")
    with open(py, "w") as fh:
        fh.write("#!/bin/sh\ntouch \"$0.HIJACKED\"\n")
    os.chmod(py, 0o755)
    return path


class LifecycleTests(EndToEndBase):
    def test_first_tick_hides_dispatches_and_the_refresh_shows_numbers(self):
        self.assertEqual(self.tick(), "")  # never verified: not even the names
        inv = self.invocations()
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]["params"]["context"]["invocation_source"], "quotaribbon-tabbar")
        self.assertEqual(self.tick(), "")
        self.assertEqual(len(self.invocations()), 1, "the held lease must prevent a second dispatch")
        out = self.run_dispatched().stdout
        with open(os.path.join(self.state_dir, "dispatch.json")) as fh:
            self.assertIsNotNone(json.load(fh)["finished_at"], self.output_log())
        if self.tick() != CLAUDE + SEP + CODEX:
            # Only a transient fail-closed outcome may delay the numbers.
            self.assertRegex(out, r"=(discarded|failed)", self.output_log())
            self.age(1000)
            self.action("refresh")
        self.assertEqual(self.tick(), CLAUDE + SEP + CODEX, self.output_log())
        n = len(self.invocations())
        self.tick()
        self.assertEqual(len(self.invocations()), n, "nothing is due right after a refresh")

    def test_verification_then_ellipsis_while_fetching_then_numbers(self):
        self.fake["QR_E2E_CLAUDE_DELAY"] = "1"
        self.tick()
        inv = self.invocations()[-1]["params"]
        proc = self.action("refresh", inv["context"], background=True)
        try:
            seen = set()
            deadline = time.monotonic() + 20
            while proc.poll() is None and time.monotonic() < deadline:
                seen.add(self.tick())
                time.sleep(0.1)
        finally:
            out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue(any(line.startswith(f"Claude {E}") for line in seen), seen)
        self.assertEqual(self.tick(), CLAUDE + SEP + CODEX)

    def test_off_hides_at_once_stops_requests_and_on_reverifies_first(self):
        self.connect_both()
        self.action("claude-off")
        self.assertEqual(self.tick(), CODEX)
        before = self.provider_starts("claude")
        self.age(200)
        self.tick_and_refresh()
        self.assertEqual(self.provider_starts("claude"), before, "OFF must not start Claude processes")
        self.action("claude-on")
        self.assertEqual(self.tick().split(SEP)[0], f"Claude {E}")
        self.age(1)
        self.assertEqual(self.tick_and_refresh(), CLAUDE + SEP + CODEX, self.output_log())

    def test_logout_hides_the_provider_and_keeps_the_other(self):
        self.connect_both()
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "logged_out"
        self.age(200)
        self.assertEqual(self.tick_and_refresh(), CODEX, self.output_log())
        self.assertEqual(glob.glob(os.path.join(self.state_dir, "providers", "claude", "*", "snapshot.json")), [])

    def test_account_switch_never_shows_the_old_numbers(self):
        self.connect_both()
        self.fake["QR_E2E_CLAUDE_ACCOUNT"] = "b"
        self.age(200)
        line = self.tick_and_refresh()
        self.assertNotIn(CLAUDE, line)
        self.assertIn(CODEX, line)
        self.age(5)
        self.assertEqual(self.tick_and_refresh(), CLAUDE + SEP + CODEX, self.output_log())

    def test_transient_failure_keeps_the_values_marked_stale(self):
        self.connect_both()
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "usage_error"
        self.age(400)
        line = self.tick_and_refresh()
        claude = line.split(SEP)[0]
        self.assertRegex(claude, re.escape(CLAUDE) + r" \[stale [0-9]+m\]")
        self.assertEqual(line.split(SEP)[1], CODEX)

    def test_window_not_provided_is_left_out_and_none_shows_dashes(self):
        self.connect_both()
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "five_hour_null"
        self.age(200)
        self.assertEqual(self.tick_and_refresh(), "Claude 7d 79%" + SEP + CODEX, self.output_log())
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "no_windows"
        self.age(200)
        self.assertEqual(self.tick_and_refresh(), "Claude --" + SEP + CODEX, self.output_log())


class ServerAndPluginTests(EndToEndBase):
    def test_new_server_epoch_shows_ellipsis_until_reverified(self):
        self.connect_both()
        self.startup()
        self.assertEqual(self.tick(), f"Claude {E}{SEP}Codex {E}")
        self.age(1)
        line = self.tick_and_refresh()
        self.assertEqual(line, CLAUDE + SEP + CODEX, self.output_log())

    def test_restarted_server_without_startup_hook_shows_no_old_numbers(self):
        self.connect_both()
        self.restart_herdr_server()
        self.assertEqual(self.tick(), f"Claude {E}{SEP}Codex {E}")
        self.age(1)
        self.assertEqual(self.tick_and_refresh(), CLAUDE + SEP + CODEX, self.output_log())

    def test_disabled_plugin_shows_nothing_dispatches_nothing_and_collects_nothing(self):
        self.connect_both()
        self.server.plugins = [plugin_entry(REPO_ROOT, enabled=False)]
        n = len(self.invocations())
        self.assertEqual(self.tick(), "")
        self.assertEqual(len(self.invocations()), n)
        starts = (self.provider_starts("claude"), self.provider_starts("codex"))
        self.age(200)
        self.action("refresh")
        self.assertEqual((self.provider_starts("claude"), self.provider_starts("codex")), starts)

    def test_sigterm_mid_collection_exits_cleanly_and_leaves_no_provider_process(self):
        import signal

        self.connect_both()
        self.age(200)
        self.fake["QR_E2E_CLAUDE_DELAY"] = "5"
        before = self.provider_starts("claude", usage_only=True)
        proc = self.action("refresh", background=True)
        deadline = time.monotonic() + 20
        while self.provider_starts("claude", usage_only=True) == before and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)  # the usage session is waiting on its delayed answer
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out + err)
        self.assertNotIn("Traceback", out + err)
        # No provider child survived the action: the profile lock is free again.
        self.fake["QR_E2E_CLAUDE_DELAY"] = "0"
        r = self.action("refresh")
        self.assertIn("claude=published", r.stdout)
        self.assertEqual(self.tick(), CLAUDE + SEP + CODEX)

    def test_a_sigkilled_refresh_leaves_a_cli_that_exits_on_eof_and_the_next_refresh_publishes(self):
        import signal

        self.connect_both()
        self.age(200)
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "hold_until_eof"  # never answers; exits on stdin EOF
        before = len(self._claude_session_pids())
        proc = self.action("refresh", background=True)
        try:
            deadline = time.monotonic() + 20
            while len(self._claude_session_pids()) == before and time.monotonic() < deadline:
                time.sleep(0.05)
            child = self._claude_session_pids()[-1]
            ppid = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(child)], capture_output=True,
                                  text=True).stdout.strip()
            self.assertTrue(ppid.isdigit())
            os.kill(int(ppid), signal.SIGKILL)  # the refresh process: no cleanup runs
        finally:
            proc.communicate(timeout=30)
        end = time.monotonic() + 5
        while time.monotonic() < end and subprocess.run(["/bin/ps", "-p", str(child)],
                                                        capture_output=True).returncode == 0:
            time.sleep(0.05)
        self.assertNotEqual(subprocess.run(["/bin/ps", "-p", str(child)], capture_output=True).returncode, 0,
                            "the CLI child must exit once its pipes close")
        self.fake["QR_E2E_CLAUDE_SCENARIO"] = "ok"
        self.age(200)  # the next refresh, when it is due
        r = self.action("refresh")
        self.assertIn("claude=published", r.stdout)
        self.assertEqual(self.tick(), CLAUDE + SEP + CODEX)

    def _claude_session_pids(self) -> list[int]:
        with open(os.path.join(self.bin, "fake-cli.jsonl")) as fh:
            recs = [json.loads(line) for line in fh if line.strip()]
        return [r["pid"] for r in recs if r.get("mode") == "invoke" and r.get("kind") == "session"]

    def test_concurrent_refreshes_collect_once_per_provider(self):
        self.connect_both()
        self.age(200)
        before = (self.provider_starts("claude"), self.provider_starts("codex"))
        self.fake["QR_E2E_CLAUDE_DELAY"] = "1"
        self.fake["QR_E2E_CODEX_DELAY"] = "1"
        procs = [self.action("refresh", background=True) for _ in range(3)]
        outs = [p.communicate(timeout=90)[0] for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), outs)
        self.assertEqual(sum("claude=published" in o for o in outs), 1, outs)
        self.assertEqual(sum("codex=published" in o for o in outs), 1, outs)
        self.assertEqual(self.tick(), CLAUDE + SEP + CODEX)
        self.assertGreater(self.provider_starts("claude"), before[0])


if __name__ == "__main__":
    unittest.main()
