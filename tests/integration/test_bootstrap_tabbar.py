"""The stable tab-bar bootstrap, run exactly like Herdr runs it (``/bin/sh -lc``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import unittest

from quotaribbon import cache
from quotaribbon import setup_config as sc
from quotaribbon.paths import Paths
from tests.helpers.fake_herdr import (
    PLUGIN_ID,
    FakeHerdrServer,
    plugin_entry,
    short_tempdir,
)
from tests.helpers.state_seed import LINES, seed_connected

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOOTSTRAP = os.path.join(REPO_ROOT, "bootstrap", "tabbar.py")
PYTHON = os.path.realpath(sys.executable)

FAKE_ENTRY = '''
import os
def run(boot_cfg, socket_path, plugin_info, now=None):
    marker = os.environ.get("QR_ENTRY_MARKER")
    if marker:
        with open(marker, "a") as fh:
            fh.write(__file__ + "\\n")
    return {line!r}
'''

HOSTILE_INIT = """
import os
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "HIJACKED"), "w").close()
"""

FORBIDDEN_ON_TICK = ("dataclasses", "logging", "logging.handlers", "threading", "tempfile", "pathlib",
                     "subprocess", "uuid", "hashlib", "typing", "shlex")


def make_fake_root(base: str, name: str, line: str, *, api: int = 1, manifest: bool = True) -> str:
    root = os.path.join(base, name)
    pkg = os.path.join(root, "quotaribbon")
    os.makedirs(pkg)
    if manifest:
        with open(os.path.join(root, "herdr-plugin.toml"), "w") as fh:
            fh.write('id = "fluzzy.herdr-quotaribbon"\n')
    with open(os.path.join(pkg, "__init__.py"), "w") as fh:
        fh.write(f"BOOTSTRAP_API = {api}\n")
    with open(os.path.join(pkg, "tabbar_entry.py"), "w") as fh:
        fh.write(FAKE_ENTRY.format(line=line))
    return root


class BootstrapBase(unittest.TestCase):
    config_dir_name = "cfg"

    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        self.server = FakeHerdrServer(os.path.join(self.tmp, "h.sock")).start()
        self.cfg = os.path.join(self.tmp, self.config_dir_name)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg)
        os.makedirs(self.state)
        shutil.copy(BOOTSTRAP, os.path.join(self.cfg, "tabbar.py"))
        sc.install_wrapper(PYTHON, os.path.join(self.cfg, "tabbar.py"))
        self.write_bootstrap_json()
        self.marker = os.path.join(self.tmp, "entry-ran.log")
        self.cwd = os.path.join(self.tmp, "pane cwd")
        os.makedirs(self.cwd)

    def tearDown(self):
        self.server.stop()
        self._tmp.cleanup()

    def write_bootstrap_json(self, **overrides) -> None:
        cfg = {"schema_version": 1, "bootstrap_version": 1, "plugin_id": PLUGIN_ID, "state_dir": self.state,
               "config_dir": self.cfg, "python": PYTHON}
        cfg.update(overrides)
        with open(os.path.join(self.cfg, "bootstrap.json"), "w") as fh:
            json.dump(cfg, fh)

    def env(self, **extra) -> dict:
        env = {"HOME": self.tmp, "PATH": "/usr/bin:/bin", "HERDR_SOCKET_PATH": self.server.socket_path,
               "QR_ENTRY_MARKER": self.marker, "LANG": "C"}
        env.update(extra)
        return env

    def command(self) -> str:
        return sc.build_command(PYTHON, os.path.join(self.cfg, "tabbar.py"))

    def tick(self, env=None, cwd=None, command=None) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", "-lc", command or self.command()], cwd=cwd or self.cwd,
                              env=env or self.env(), capture_output=True, timeout=10)

    def entry_runs(self) -> list[str]:
        if not os.path.exists(self.marker):
            return []
        with open(self.marker) as fh:
            return fh.read().splitlines()

    def bootstrap_log(self) -> list[str]:
        path = os.path.join(self.state, "logs", "bootstrap.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()


class BootstrapTests(BootstrapBase):
    def test_enabled_plugin_prints_the_entry_line(self):
        root = make_fake_root(self.tmp, "root1", "Claude 5h 32% | 7d 68%")
        self.server.plugins = [plugin_entry(root)]
        r = self.tick()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode(), "Claude 5h 32% | 7d 68%\n")
        self.assertEqual(r.stderr, b"")

    def test_disabled_or_unlinked_plugin_prints_nothing_and_invokes_nothing(self):
        root = make_fake_root(self.tmp, "root1", "Claude 1%")
        for plugins in ([plugin_entry(root, enabled=False)], [], [plugin_entry(root, plugin_id="other.plugin")]):
            with self.subTest(plugins=len(plugins)):
                self.server.plugins = plugins
                r = self.tick()
                self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(self.entry_runs(), [])
        self.assertNotIn("plugin.action.invoke", self.server.methods())

    def test_unreachable_or_missing_socket_prints_nothing(self):
        root = make_fake_root(self.tmp, "root1", "Claude 1%")
        self.server.plugins = [plugin_entry(root)]
        env = self.env()
        del env["HERDR_SOCKET_PATH"]
        self.assertEqual(self.tick(env=env).stdout, b"")
        self.server.stop()
        r = self.tick()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(self.entry_runs(), [])

    def test_new_plugin_root_is_used_on_the_next_tick(self):
        a = make_fake_root(self.tmp, "checkout-a", "from A")
        b = make_fake_root(self.tmp, "checkout-b", "from B")
        self.server.plugins = [plugin_entry(a)]
        self.assertEqual(self.tick().stdout, b"from A\n")
        self.server.plugins = [plugin_entry(b)]
        self.assertEqual(self.tick().stdout, b"from B\n")
        runs = self.entry_runs()
        self.assertTrue(runs[0].startswith(a) and runs[1].startswith(b), runs)

    def test_invalid_plugin_roots_are_refused_and_logged_once(self):
        no_manifest = make_fake_root(self.tmp, "nomanifest", "x", manifest=False)
        for root in ("relative/root", os.path.join(self.tmp, "missing"), no_manifest):
            with self.subTest(root=root):
                self.server.plugins = [plugin_entry(root)]
                r = self.tick()
                self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(self.entry_runs(), [])
        reasons = [line.split(" ")[1] for line in self.bootstrap_log()]
        self.assertEqual(reasons, ["invalid_plugin_root"])  # rate-limited

    def test_incompatible_bootstrap_api_prints_nothing(self):
        root = make_fake_root(self.tmp, "future", "x", api=2)
        self.server.plugins = [plugin_entry(root)]
        self.assertEqual(self.tick().stdout, b"")
        self.assertEqual(self.entry_runs(), [])

    def test_missing_or_corrupt_bootstrap_json_prints_nothing(self):
        root = make_fake_root(self.tmp, "root1", "x")
        self.server.plugins = [plugin_entry(root)]
        path = os.path.join(self.cfg, "bootstrap.json")
        for content in (None, b"{not json", b'{"schema_version": 1}', json.dumps({
                "schema_version": True, "bootstrap_version": 1, "plugin_id": PLUGIN_ID, "state_dir": self.state,
                "config_dir": self.cfg, "python": PYTHON}).encode(),
                json.dumps({"schema_version": 1, "bootstrap_version": 1, "plugin_id": PLUGIN_ID,
                            "state_dir": "relative", "config_dir": self.cfg, "python": PYTHON}).encode()):
            with self.subTest(content=content):
                if content is None:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    with open(path, "wb") as fh:
                        fh.write(content)
                r = self.tick()
                self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(self.entry_runs(), [])

    def test_hanging_socket_exits_quietly_within_two_seconds(self):
        self.server.hang = True
        t0 = time.monotonic()
        r = self.tick()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_hanging_entry_hits_the_self_timeout(self):
        root = make_fake_root(self.tmp, "slow", "never")
        with open(os.path.join(root, "quotaribbon", "tabbar_entry.py"), "w") as fh:
            fh.write("import time\ndef run(*a, **k):\n    time.sleep(10)\n    return 'late'\n")
        self.server.plugins = [plugin_entry(root)]
        t0 = time.monotonic()
        r = self.tick()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_entry_exception_prints_nothing_and_logs_reason_only(self):
        root = make_fake_root(self.tmp, "boom", "x")
        with open(os.path.join(root, "quotaribbon", "tabbar_entry.py"), "w") as fh:
            fh.write("def run(*a, **k):\n    raise RuntimeError('token=sk-SECRETSECRET user@example.invalid')\n")
        self.server.plugins = [plugin_entry(root)]
        r = self.tick()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        log = "\n".join(self.bootstrap_log())
        self.assertIn("entry_failed RuntimeError", log)
        self.assertNotIn("SECRET", log)
        self.assertNotIn("@", log)

    def test_output_is_one_sanitized_line(self):
        root = make_fake_root(self.tmp, "multi", "first\nsecond \x1b[31mred\x1b[0m \x07end")
        self.server.plugins = [plugin_entry(root)]
        out = self.tick().stdout.decode()
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertTrue(out.startswith("first second"))

    def test_hostile_cwd_package_and_python_are_ignored(self):
        root = make_fake_root(self.tmp, "real", "real line")
        self.server.plugins = [plugin_entry(root)]
        os.makedirs(os.path.join(self.cwd, "quotaribbon"))
        with open(os.path.join(self.cwd, "quotaribbon", "__init__.py"), "w") as fh:
            fh.write(HOSTILE_INIT)
        with open(os.path.join(self.cwd, "quotaribbon", "tabbar_entry.py"), "w") as fh:
            fh.write(HOSTILE_INIT + "def run(*a, **k):\n    return 'hijacked'\n")
        for name in ("python3", "python"):
            p = os.path.join(self.cwd, name)
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\ntouch \"$(dirname \"$0\")/HIJACKED_PY\"\necho hijacked\n")
            os.chmod(p, 0o755)
        env = self.env(PATH=".:" + "/usr/bin:/bin", PYTHONPATH=self.cwd, PYTHONSTARTUP=os.path.join(self.cwd, "x.py"))
        r = self.tick(env=env)
        self.assertEqual(r.stdout, b"real line\n")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "HIJACKED")))
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "HIJACKED_PY")))


class OddConfigDirTests(BootstrapBase):
    config_dir_name = "설정 dir it's \"quoted\" $(x)"

    def test_command_with_spaces_korean_and_quotes_runs_under_sh_lc(self):
        root = make_fake_root(self.tmp, "root with space", "Codex 15m 25%")
        self.server.plugins = [plugin_entry(root)]
        entry = sc.build_entry(PYTHON, os.path.join(self.cfg, "tabbar.py"))
        command = tomllib.loads("x = [" + sc.entry_text(entry) + "]\n")["x"][0]["command"]
        r = self.tick(command=command)
        self.assertEqual(r.stdout.decode(), "Codex 15m 25%\n")
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "x")))


class LauncherFailureTests(BootstrapBase):
    """Whatever breaks before the bootstrap's own guards run, the tab bar sees exit 0 and
    no output, within the deadline."""

    def assert_silent(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def tabbar_py(self) -> str:
        return os.path.join(self.cfg, "tabbar.py")

    def test_missing_interpreter_is_silent(self):
        sc.install_wrapper(os.path.join(self.tmp, "gone", "python3"), self.tabbar_py())
        self.assert_silent(self.tick())

    def test_missing_bootstrap_or_launcher_is_silent(self):
        os.unlink(self.tabbar_py())
        self.assert_silent(self.tick())
        os.unlink(sc.wrapper_path(self.tabbar_py()))
        self.assert_silent(self.tick())

    def test_import_failure_before_main_is_silent(self):
        for body in ("import quotaribbon_no_such_module\n", "raise SystemExit(3)\n", "def broken(:\n"):
            with self.subTest(body=body):
                with open(self.tabbar_py(), "w") as fh:
                    fh.write(body)
                self.assert_silent(self.tick())

    def test_hang_before_main_is_cut_by_the_launcher_deadline(self):
        with open(self.tabbar_py(), "w") as fh:
            fh.write("import time\ntime.sleep(10)\nprint('late')\n")
        t0 = time.monotonic()
        r = self.tick()
        elapsed = time.monotonic() - t0
        self.assert_silent(r)
        self.assertLess(elapsed, 2.0)

    def test_hostile_cwd_and_path_do_not_influence_the_launcher(self):
        root = make_fake_root(self.tmp, "real", "unused")
        with open(os.path.join(root, "quotaribbon", "tabbar_entry.py"), "w") as fh:
            fh.write("import os\ndef run(*a, **k):\n    return 'cwd=' + os.getcwd()\n")
        self.server.plugins = [plugin_entry(root)]
        hijack = os.path.join(self.cwd, "HIJACKED")
        for name in ("sleep", "kill", "sh", "python3"):
            p = os.path.join(self.cwd, name)
            with open(p, "w") as fh:
                fh.write(f"#!/bin/sh\ntouch {hijack!r}\n")
            os.chmod(p, 0o755)
        r = self.tick(env=self.env(PATH=".:" + self.cwd + ":/usr/bin:/bin"))
        self.assertEqual(r.stdout, b"cwd=/\n")
        self.assertFalse(os.path.exists(hijack))

    def test_bootstrap_with_broken_standard_library_import_exits_zero(self):
        # Simulate a standard-library import failure at module level.
        code = ("import builtins, runpy, sys\n"
                "real = builtins.__import__\n"
                "def fake(name, *a, **k):\n"
                "    if name == 'socket':\n"
                "        raise ImportError('boom')\n"
                "    return real(name, *a, **k)\n"
                "builtins.__import__ = fake\n"
                f"runpy.run_path({BOOTSTRAP!r}, run_name='__main__')\n")
        r = subprocess.run([PYTHON, "-I", "-c", code], capture_output=True, timeout=10, cwd="/")
        self.assert_silent(r)


class RealEntryTests(BootstrapBase):
    """Bootstrap → real ``tabbar_entry`` and core modules → real state files → fake socket."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "plugin")
        os.makedirs(self.root)
        shutil.copy(os.path.join(REPO_ROOT, "herdr-plugin.toml"), self.root)
        shutil.copytree(os.path.join(REPO_ROOT, "quotaribbon"), os.path.join(self.root, "quotaribbon"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.server.plugins = [plugin_entry(self.root)]
        self.paths = Paths(config_dir=self.cfg, state_dir=self.state)

    def seed(self, **kw):
        return seed_connected(self.paths, self.env(), **kw)

    def test_rendered_line_is_printed_without_dispatch_when_nothing_is_due(self):
        self.seed()
        r = self.tick()
        self.assertEqual(r.stdout.decode("utf-8"), LINES["claude"] + "  ·  " + LINES["codex"] + "\n")
        self.assertNotIn("plugin.action.invoke", self.server.methods())

    def test_never_verified_providers_print_nothing_and_dispatch_refresh(self):
        r = self.tick()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(len(self.server.requests_for("plugin.action.invoke")), 1)

    def test_due_work_invokes_refresh_and_never_prints_the_invocation_record(self):
        self.seed(usage_due=True)
        self.server.invoke_result = {"type": "plugin_action_invoked", "log": {"log_id": "LOGMARKER-123",
                                                                              "status": "running"}}
        r = self.tick()
        self.assertEqual(r.stdout.decode("utf-8"), LINES["claude"] + "  ·  " + LINES["codex"] + "\n")
        self.assertNotIn(b"LOGMARKER", r.stdout + r.stderr)
        inv = self.server.requests_for("plugin.action.invoke")
        self.assertEqual(len(inv), 1)
        lease = cache.read_dispatch_at(self.paths.state_dir)
        self.assertEqual(inv[0]["params"], {"plugin_id": PLUGIN_ID, "action_id": "refresh", "context": {
            "invocation_source": "quotaribbon-tabbar", "correlation_id": lease["id"]}})
        self.assertIsNone(lease["finished_at"])
        # The lease is held: the next tick does not dispatch again.
        self.tick()
        self.assertEqual(len(self.server.requests_for("plugin.action.invoke")), 1)

    def test_failed_invocation_releases_the_lease_and_prints_only_the_line(self):
        self.seed(usage_due=True)
        self.server.errors["plugin.action.invoke"] = ("plugin_disabled", "disabled")
        r = self.tick()
        self.assertEqual(r.stdout.decode("utf-8"), LINES["claude"] + "  ·  " + LINES["codex"] + "\n")
        lease = cache.read_dispatch_at(self.paths.state_dir)
        self.assertIsNotNone(lease["finished_at"])

    def test_settings_edit_without_refresh_shows_ellipsis(self):
        self.seed()
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "providers": {"claude": {"enabled": True, "binary": "/x/claude"},
                                                          "codex": {"enabled": True}}}, fh)
        r = self.tick()
        self.assertEqual(r.stdout.decode("utf-8"), "Claude …  ·  " + LINES["codex"] + "\n")

    def test_clock_rollback_hides_numbers(self):
        self.seed()
        cache.note_wall_clock(self.paths, time.time() + 3600)
        r = self.tick()
        self.assertEqual(r.stdout.decode("utf-8"), "Claude --  ·  Codex --\n")

    def test_unusable_settings_print_nothing_and_dispatch_nothing(self):
        self.seed(usage_due=True)
        with open(self.paths.settings, "w") as fh:
            fh.write("{broken")
        r = self.tick()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertNotIn("plugin.action.invoke", self.server.methods())

    def test_warm_tick_latency(self):
        self.seed()
        self.tick()
        samples = []
        for _ in range(15):
            t0 = time.monotonic()
            self.tick()
            samples.append(time.monotonic() - t0)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        self.assertLess(p95, 0.25, f"p95={p95:.3f}s samples={samples}")

    def test_full_tick_stays_within_the_import_budget(self):
        self.seed(usage_due=True)
        forbidden = FORBIDDEN_ON_TICK + ("quotaribbon.types", "quotaribbon.paths", "quotaribbon.fsutil", "quotaribbon.log")
        code = (
            "import json, os, sys\n"
            f"sys.path.insert(0, {self.root!r})\n"
            "from quotaribbon import tabbar_entry\n"
            f"boot = json.load(open({os.path.join(self.cfg, 'bootstrap.json')!r}))\n"
            "info = {'plugin_id': boot['plugin_id'], 'enabled': True}\n"
            "line = tabbar_entry.run(boot, os.environ['HERDR_SOCKET_PATH'], info)\n"
            f"bad = [m for m in {forbidden!r} if m in sys.modules]\n"
            "print(json.dumps({'line': line, 'bad': bad}))\n"
        )
        r = subprocess.run([PYTHON, "-I", "-c", code], capture_output=True, text=True, timeout=30, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["bad"], [])
        self.assertEqual(got["line"], LINES["claude"] + "  ·  " + LINES["codex"])
        self.assertEqual(len(self.server.requests_for("plugin.action.invoke")), 1)


if __name__ == "__main__":
    unittest.main()
