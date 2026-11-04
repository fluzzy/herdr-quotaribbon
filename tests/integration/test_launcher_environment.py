"""Launchers and the tab-bar bootstrap under hostile environments and file system states."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from quotaribbon import herdr_bridge as hb
from tests.helpers.fake_herdr import plugin_entry
from tests.integration.test_bootstrap_tabbar import BootstrapBase, make_fake_root

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO_ROOT, "bin", "quotaribbon")
REAL_PY_DIR = os.path.dirname(os.path.realpath(sys.executable))
REAL_PYTHON = os.path.realpath(sys.executable)

# Names the launchers use that bash would let an exported function replace.
HIJACKED_NAMES = ("cd", "kill", "wait", "command", "printf", "read", "pwd", "test", "die", "sleep")


def hostile_functions(marker_dir: str) -> dict[str, str]:
    """``BASH_FUNC_*`` exports that record their name when they run."""
    return {f"BASH_FUNC_{n}%%": f"() {{ /usr/bin/touch '{marker_dir}/{n}'; }}" for n in HIJACKED_NAMES}


class LauncherHostileEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.markers = os.path.join(self.tmp, "markers")
        os.makedirs(self.markers)

    def tearDown(self):
        self._tmp.cleanup()

    def run_launcher(self, env: dict, cwd: str = "/", args=("bogus",)):
        base = {"HOME": self.tmp, "LANG": "C"}
        base.update(env)
        return subprocess.run(["/bin/sh", LAUNCHER, *args], cwd=cwd, env=base, capture_output=True, text=True,
                              timeout=60)

    def write_exe(self, directory: str, name: str, body: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def test_exported_functions_never_run_and_the_action_still_starts(self):
        env = {"PATH": f"{REAL_PY_DIR}:/usr/bin:/bin", **hostile_functions(self.markers)}
        r = self.run_launcher(env)
        self.assertEqual(r.returncode, 2, r.stderr)  # the real CLI rejected the subcommand
        self.assertIn("unknown subcommand", r.stderr)
        self.assertEqual(sorted(os.listdir(self.markers)), [])

    def test_toggle_works_with_exported_functions(self):
        cfg = os.path.join(self.tmp, "cfg")
        state = os.path.join(self.tmp, "state")
        env = {"PATH": f"{REAL_PY_DIR}:/usr/bin:/bin", "HERDR_PLUGIN_CONFIG_DIR": cfg,
               "HERDR_PLUGIN_STATE_DIR": state, **hostile_functions(self.markers)}
        r = self.run_launcher(env, args=("claude-off",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "QuotaRibbon: Claude OFF\n")
        self.assertEqual(sorted(os.listdir(self.markers)), [])

    def test_python_in_or_below_the_pane_cwd_or_pwd_is_never_run(self):
        pane = os.path.join(self.tmp, "repo")
        for name in ("python3.13", "python3.12", "python3.11", "python3"):
            self.write_exe(os.path.join(pane, "bin"), name,
                           f"#!/bin/sh\n/usr/bin/touch '{self.markers}/{name}'\nexit 0\n")
        for env_extra, cwd in (({"HERDR_ACTIVE_PANE_CWD": pane}, "/"), ({"PWD": pane}, pane), ({}, pane)):
            with self.subTest(env=env_extra, cwd=cwd):
                env = {"PATH": f"{pane}/bin:{pane}:{REAL_PY_DIR}:/usr/bin:/bin", **env_extra}
                r = self.run_launcher(env, cwd=cwd)
                self.assertEqual(r.returncode, 2, r.stderr)  # the real interpreter after them ran
                self.assertEqual(sorted(os.listdir(self.markers)), [])

    def test_pinned_interpreter_runs_with_the_system_path_and_the_original_path_is_passed_on(self):
        py_dir = os.path.join(self.tmp, "py")
        reporter = self.write_exe(py_dir, "python3", """#!/bin/sh
for a; do [ "$a" = -c ] && exit 0; done
printf 'PATH=%s\\n' "$PATH"
printf 'HOST=%s\\n' "$QUOTARIBBON_HOST_PATH"
exit 7
""")
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "bootstrap.json"), "w") as fh:
            json.dump({"schema_version": 1, "python": reporter}, fh)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        original = f".:{other}:{REAL_PY_DIR}:/usr/bin:/bin"
        r = self.run_launcher({"PATH": original, "HERDR_PLUGIN_CONFIG_DIR": cfg})
        self.assertEqual(r.returncode, 7, r.stderr)
        seen = dict(line.split("=", 1) for line in r.stdout.splitlines())
        self.assertEqual(seen["PATH"], "/usr/bin:/bin")
        self.assertEqual(seen["HOST"], original)


class WrapperHostileEnvTests(BootstrapBase):
    def test_exported_functions_do_not_reach_the_tab_bar_line(self):
        root = make_fake_root(self.tmp, "root1", "Claude 5h 32% | 7d 68%")
        self.server.plugins = [plugin_entry(root)]
        markers = os.path.join(self.tmp, "markers")
        os.makedirs(markers)
        env = self.env(**hostile_functions(markers))
        env["BASH_FUNC_cd%%"] = '() { builtin cd "$@" && echo "cd-hook: now in $PWD"; }'
        r = self.tick(env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode(), "Claude 5h 32% | 7d 68%\n")
        self.assertEqual(sorted(os.listdir(markers)), [])

    def test_watchdog_still_kills_a_hung_interpreter_with_exported_functions(self):
        root = make_fake_root(self.tmp, "root1", "x")
        with open(os.path.join(root, "quotaribbon", "tabbar_entry.py"), "w") as fh:
            fh.write("import signal, time\nsignal.signal(signal.SIGALRM, signal.SIG_IGN)\n"
                     "def run(*a, **k):\n    time.sleep(30)\n")
        self.server.plugins = [plugin_entry(root)]
        markers = os.path.join(self.tmp, "markers")
        os.makedirs(markers)
        t0 = time.monotonic()
        r = self.tick(env=self.env(**hostile_functions(markers)))
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(sorted(os.listdir(markers)), [])


class BootstrapFileSystemTests(BootstrapBase):
    def test_fifo_bootstrap_json_and_log_never_block_the_tick(self):
        root = make_fake_root(self.tmp, "root1", "Claude 1%")
        self.server.plugins = [plugin_entry(root)]
        cfg_json = os.path.join(self.cfg, "bootstrap.json")
        os.unlink(cfg_json)
        os.mkfifo(cfg_json, 0o600)
        t0 = time.monotonic()
        r = self.tick()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        os.unlink(cfg_json)
        self.write_bootstrap_json()
        os.makedirs(os.path.join(self.state, "logs"))
        os.mkfifo(os.path.join(self.state, "logs", "bootstrap.log"), 0o600)
        t0 = time.monotonic()
        r = self.tick(env=self.env(HERDR_SOCKET_PATH=os.path.join(self.tmp, "missing.sock")))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_plugin_root_another_account_could_change_is_not_imported(self):
        root = make_fake_root(self.tmp, "root1", "Claude 1%")
        self.server.plugins = [plugin_entry(root)]
        self.assertEqual(self.tick().stdout, b"Claude 1%\n")
        pkg = os.path.join(root, "quotaribbon")
        for path, mode in ((root, 0o777), (pkg, 0o775), (os.path.join(pkg, "tabbar_entry.py"), 0o666)):
            with self.subTest(path=os.path.basename(path), mode=oct(mode)):
                old = os.stat(path).st_mode & 0o777
                os.chmod(path, mode)
                try:
                    r = self.tick()
                finally:
                    os.chmod(path, old)
                self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertTrue(any(" untrusted_plugin_root" in line for line in self.bootstrap_log()))

    def test_symlinked_plugin_root_imports_from_the_resolved_root(self):
        root = make_fake_root(self.tmp, "real-root", "Claude 1%")
        link = os.path.join(self.tmp, "linked-root")
        os.symlink(root, link)
        self.server.plugins = [plugin_entry(link)]
        r = self.tick()
        self.assertEqual(r.stdout, b"Claude 1%\n")
        with open(self.marker) as fh:
            self.assertTrue(fh.read().startswith(os.path.join(root, "quotaribbon") + os.sep))


class HerdrExecutableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def herdr(self, directory: str, body: str = "#!/bin/sh\necho 'herdr 0.9.5'\n") -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "herdr")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def test_herdr_in_or_below_the_pane_cwd_is_not_used(self):
        pane = os.path.join(self.tmp, "proj")
        self.herdr(os.path.join(pane, "bin"))
        env = {"PATH": f"{pane}/bin:/usr/bin:/bin", "HOME": self.tmp, "HERDR_ACTIVE_PANE_CWD": pane}
        self.assertIsNone(hb.herdr_executable(env))
        env.pop("HERDR_ACTIVE_PANE_CWD")
        self.assertEqual(hb.herdr_executable(env), os.path.join(pane, "bin", "herdr"))

    def test_script_with_an_untrusted_env_interpreter_is_refused_also_when_explicit(self):
        good = self.herdr(os.path.join(self.tmp, "bin"), "#!/usr/bin/env qrinterp\n")
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        with open(os.path.join(shared, "qrinterp"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(shared, "qrinterp"), 0o755)
        os.chmod(shared, 0o777)
        env = {"PATH": f"{shared}:{os.path.dirname(good)}:/usr/bin:/bin", "HOME": self.tmp}
        self.assertIsNone(hb.herdr_executable(env))
        self.assertIsNone(hb.herdr_executable(dict(env, HERDR_BIN_PATH=good, PATH="/usr/bin:/bin")))

    def test_launcher_host_path_is_searched_and_the_child_gets_a_clean_environment(self):
        bin_dir = os.path.join(self.tmp, "bin")
        out = os.path.join(self.tmp, "env.json")
        self.herdr(bin_dir, f"""#!/bin/sh
/usr/bin/env > '{out}'
echo 'herdr 0.9.5'
""")
        env = {"PATH": "/usr/bin:/bin", "QUOTARIBBON_HOST_PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": self.tmp,
               "BASH_FUNC_cd%%": "() { :; }"}
        self.assertEqual(hb.herdr_version(env), "0.9.5")
        with open(out) as fh:
            child = dict(line.split("=", 1) for line in fh.read().splitlines() if "=" in line)
        self.assertEqual(child["PATH"], f"{bin_dir}:/usr/bin:/bin")
        self.assertNotIn("QUOTARIBBON_HOST_PATH", child)
        self.assertFalse(any(k.startswith("BASH_FUNC_") for k in child))


if __name__ == "__main__":
    unittest.main()
