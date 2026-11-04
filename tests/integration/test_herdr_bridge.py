"""Herdr socket client and herdr CLI helpers against a fake server and a fake binary."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest

from quotaribbon import herdr_bridge as hb
from tests.helpers.fake_herdr import (
    FakeHerdrServer,
    agent,
    integration,
    plugin_entry,
    short_tempdir,
    write_fake_herdr_cli,
)

HOME = os.path.join(tempfile.gettempdir(), "qr-home")
STATUS_TEXT = (
    f"pi: not installed ({HOME}/.pi/agent/extensions/herdr-agent-state.ts)\n"
    f"claude: current (v11) ({HOME}/.claude/hooks/herdr-agent-state.sh)\n"
    f"codex: outdated (v9) ({HOME}/my (odd) dir/.codex/herdr-agent-state.sh)\n"
    f"letta (experimental): not installed ({HOME}/.letta/hooks/herdr-agent-session.sh)\n"
)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.sock = os.path.join(self.tmp, "h.sock")
        self.server = FakeHerdrServer(self.sock).start()

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def client(self, timeout: float = 1.0) -> hb.HerdrClient:
        return hb.HerdrClient(self.sock, timeout)


class SocketClientTests(Base):
    def test_typed_helpers_return_results(self):
        self.server.plugins = [plugin_entry("/root/a"), plugin_entry("/root/b", plugin_id="other.plugin")]
        self.server.integrations = [integration("claude", "current")]
        self.server.agents = [agent("codex")]
        c = self.client()
        self.assertEqual(c.plugin_info(hb_plugin_id())["plugin_root"], "/root/a")
        self.assertIsNone(c.plugin_info("missing.plugin"))
        self.assertEqual(c.integration_list()[0]["state"], "current")
        self.assertEqual(c.agent_list()[0]["agent"], "codex")
        self.assertEqual(self.server.requests_for("plugin.list")[0]["params"], {"plugin_id": hb_plugin_id()})

    def test_request_ids_are_unique(self):
        c = self.client()
        for _ in range(5):
            c.agent_list()
        ids = [r["id"] for r in self.server.requests]
        self.assertEqual(len(ids), len(set(ids)))

    def test_invoke_refresh_sends_plugin_local_action_and_context(self):
        rec = self.client().invoke_refresh(hb_plugin_id(), "lease-42")
        req = self.server.requests_for("plugin.action.invoke")[0]["params"]
        self.assertEqual(req, {"plugin_id": hb_plugin_id(), "action_id": "refresh",
                               "context": {"invocation_source": "quotaribbon-tabbar", "correlation_id": "lease-42"}})
        self.assertEqual(rec["log"]["status"], "running")

    def test_error_answers_raise_herdr_error_with_string_code(self):
        self.server.errors["plugin.action.invoke"] = ("plugin_disabled", "plugin is disabled")
        with self.assertRaises(hb.HerdrError) as cm:
            self.client().invoke_refresh(hb_plugin_id(), "x")
        self.assertEqual(cm.exception.code, "plugin_disabled")

    def test_missing_socket_raises_unavailable(self):
        self.server.stop()
        with self.assertRaises(hb.HerdrUnavailable):
            self.client().agent_list()
        with self.assertRaises(hb.HerdrUnavailable):
            hb.HerdrClient("", 1).agent_list()

    def test_hanging_server_times_out_quickly(self):
        self.server.hang = True
        t0 = time.monotonic()
        with self.assertRaises(hb.HerdrUnavailable):
            self.client(0.3).agent_list()
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_malformed_and_oversized_answers(self):
        path = os.path.join(self.tmp, "raw.sock")
        payloads = [b"not json\n", b"[1,2]\n", b'{"result":5}\n', b'{"id":"someone-else","result":{}}\n',
                    b'{"result":{"a":"' + b"x" * (hb.MAX_RESPONSE_BYTES + 10) + b'"}}\n']
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(4)

        def serve():
            for p in payloads:
                conn, _ = srv.accept()
                conn.recv(65536)
                try:
                    conn.sendall(p)
                except OSError:
                    pass
                conn.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        codes = []
        for _ in payloads:
            with self.assertRaises(hb.HerdrError) as cm:
                hb.HerdrClient(path, 5).request("agent.list")
            codes.append(cm.exception.code)
        t.join(5)
        srv.close()
        self.assertEqual(codes, ["invalid_response", "invalid_response", "invalid_response", "id_mismatch",
                                 "response_too_large"])

    def test_reload_config_reports_structured_status(self):
        env = {"HERDR_SOCKET_PATH": self.sock}
        self.assertEqual(hb.reload_config(env), ("applied", []))
        self.server.reload_status = "partial"
        self.server.reload_diagnostics = ["unknown key ui.foo"]
        self.assertEqual(hb.reload_config(env), ("partial", ["unknown key ui.foo"]))
        self.server.stop()
        self.assertEqual(hb.reload_config(env)[0], "unreachable")


def hb_plugin_id() -> str:
    from quotaribbon import PLUGIN_ID

    return PLUGIN_ID


class CliHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.bin = os.path.join(self.tmp, "bin")
        self.herdr = write_fake_herdr_cli(self.bin, config_path=os.path.join(HOME, ".config", "herdr", "config.toml"),
                                          integration_status=STATUS_TEXT, log=self.log)

    def tearDown(self):
        self._tmp.cleanup()

    def calls(self) -> list[dict]:
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def test_executable_prefers_absolute_herdr_bin_path_and_ignores_relative_path_entries(self):
        self.assertEqual(hb.herdr_executable({"HERDR_BIN_PATH": self.herdr, "PATH": ""}), self.herdr)
        self.assertEqual(hb.herdr_executable({"HERDR_BIN_PATH": "herdr", "PATH": self.bin}), self.herdr)
        cwd = os.getcwd()
        os.chdir(self.bin)
        try:
            self.assertIsNone(hb.herdr_executable({"PATH": ".:bin:"}))
        finally:
            os.chdir(cwd)

    def test_version_and_config_path(self):
        env = {"HERDR_BIN_PATH": self.herdr, "PATH": "/usr/bin:/bin", "HOME": HOME}
        self.assertEqual(hb.herdr_version(env), "0.9.5")
        self.assertEqual(hb.resolve_config_path(env), os.path.join(HOME, ".config", "herdr", "config.toml"))
        self.assertEqual(hb.resolve_config_path(dict(env, HERDR_CONFIG_PATH="/x/herdr.toml")), "/x/herdr.toml")
        self.assertEqual(hb.resolve_config_path({"PATH": "", "HOME": self.tmp}),
                         os.path.join(self.tmp, ".config", "herdr", "config.toml"))

    def test_integration_status_injects_profile_and_parses_state_tokens(self):
        env = {"HERDR_BIN_PATH": self.herdr, "PATH": "/usr/bin:/bin"}
        text = hb.integration_status_text(env, extra_env={"CLAUDE_CONFIG_DIR": "/p/claude"})
        self.assertEqual(self.calls()[-1]["claude"], "/p/claude")
        states = hb.parse_integration_states(text)
        self.assertEqual(states["claude"], "current")
        self.assertEqual(states["codex"], "outdated")
        self.assertEqual(states["letta"], "not_installed")
        self.assertEqual(hb.parse_integration_states("claude is fine\nCodex: current\n"), {})
        self.assertEqual(hb.parse_integration_states(None), {})

    def test_open_plugin_pane_reports_success_and_ui_busy(self):
        env = {"HERDR_BIN_PATH": self.herdr, "PATH": "", "HERDR_PLUGIN_ID": hb_plugin_id()}
        self.assertEqual(hb.open_plugin_pane("setup-ui", env), (True, None))
        self.assertEqual(self.calls()[-1]["argv"],
                         ["plugin", "pane", "open", "--plugin", hb_plugin_id(), "--entrypoint", "setup-ui"])
        busy = write_fake_herdr_cli(os.path.join(self.tmp, "busy"), pane_error="ui_busy")
        self.assertEqual(hb.open_plugin_pane("setup-ui", dict(env, HERDR_BIN_PATH=busy)), (False, "ui_busy"))
        self.assertEqual(hb.open_plugin_pane("setup-ui", {"PATH": ""}), (False, "herdr_missing"))

    def script(self, name: str, body: str, mode: int = 0o755) -> str:
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "herdr")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, mode)
        return path

    def assert_gone(self, pid_file: str) -> None:
        with open(pid_file) as fh:
            pids = [int(p) for p in fh.read().split()]
        self.assertTrue(pids)
        deadline = time.monotonic() + 3
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                if time.monotonic() > deadline:
                    self.fail(f"process {pid} survived")
                time.sleep(0.02)

    def test_runaway_output_is_bounded_and_the_process_group_is_killed(self):
        pids = os.path.join(self.tmp, "pids")
        exe = self.script("flood", f"/bin/sleep 30 &\necho $! $$ > '{pids}'\nexec /usr/bin/yes QuotaRibbon\n")
        t0 = time.monotonic()
        self.assertEqual(hb.run_herdr(["--version"], {"HERDR_BIN_PATH": exe, "PATH": ""}, timeout=10), (None, ""))
        self.assertLess(time.monotonic() - t0, 5)
        self.assert_gone(pids)

    def test_timeout_kills_the_process_group(self):
        from tests.helpers.fake_herdr import link_executable

        d = os.path.join(self.tmp, "hang")
        # Constant content (pids go next to "$0"): the cached executable starts without
        # the first-run scan delay, so the helper surely runs before the timeout hits.
        exe = link_executable(d, "herdr", '#!/bin/sh\n/bin/sleep 30 &\necho $! $$ > "$(/usr/bin/dirname "$0")/pids"\nwait\n')
        pids = os.path.join(d, "pids")
        t0 = time.monotonic()
        self.assertEqual(hb.run_herdr(["--help"], {"HERDR_BIN_PATH": exe, "PATH": ""}, timeout=1.0), (None, ""))
        self.assertLess(time.monotonic() - t0, 1.0 + hb.CLI_KILL_GRACE + 2)
        self.assert_gone(pids)

    def test_running_helper_is_registered_for_cancellation(self):
        from quotaribbon import procutil

        exe = self.script("slow", "/bin/sleep 0.3\necho done\n")
        base = procutil.live_session_count()
        seen = []
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "r", hb.run_herdr(["x"], {"HERDR_BIN_PATH": exe, "PATH": ""})))
        t.start()
        deadline = time.monotonic() + 3
        while t.is_alive() and time.monotonic() < deadline:
            seen.append(procutil.live_session_count())
            time.sleep(0.02)
        t.join(10)
        self.assertGreater(max(seen), base)
        self.assertEqual(procutil.live_session_count(), base)
        self.assertEqual(result["r"], (0, "done\n"))

    def test_untrusted_herdr_binaries_are_not_used(self):
        loose = self.script("loose", "echo hi\n", mode=0o777)
        self.assertIsNone(hb.herdr_executable({"HERDR_BIN_PATH": loose, "PATH": ""}))
        self.assertIsNone(hb.herdr_executable({"PATH": os.path.dirname(loose)}))
        shared = os.path.join(self.tmp, "shared")
        nested = self.script(os.path.join("shared", "bin"), "echo hi\n")
        os.chmod(shared, 0o777)
        self.assertIsNone(hb.herdr_executable({"PATH": os.path.dirname(nested)}))
        self.assertIsNone(hb.herdr_executable({"HERDR_BIN_PATH": nested, "PATH": ""}))
        self.assertEqual(hb.run_herdr(["--version"], {"HERDR_BIN_PATH": loose, "PATH": ""}), (None, ""))

    def test_reload_config_falls_back_to_cli_without_socket(self):
        env = {"HERDR_BIN_PATH": write_fake_herdr_cli(os.path.join(self.tmp, "r"), reload="failed"), "PATH": ""}
        self.assertEqual(hb.reload_config(env)[0], "failed")


if __name__ == "__main__":
    unittest.main()
