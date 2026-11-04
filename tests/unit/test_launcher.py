"""Tests for bin/quotaribbon + bootstrap/action.py: no cwd or PATH hijacking."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO_ROOT, "bin", "quotaribbon")
REAL_PY_DIR = os.path.dirname(os.path.realpath(sys.executable))

MALICIOUS_INIT = """
import os, sys
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "HIJACKED_PKG"), "w").close()
sys.exit(99)
"""

FAKE_PYTHON = """#!/bin/sh
touch "$(dirname "$0")/HIJACKED_PY"
exit 99
"""


def _base_env(path: str) -> dict[str, str]:
    env = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    return env


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        # A hostile cwd: a fake quotaribbon package and a fake python3.
        self.cwd = os.path.join(self.tmp, "hostile cwd")
        os.makedirs(os.path.join(self.cwd, "quotaribbon"))
        with open(os.path.join(self.cwd, "quotaribbon", "__init__.py"), "w") as fh:
            fh.write(MALICIOUS_INIT)
        with open(os.path.join(self.cwd, "quotaribbon", "cli.py"), "w") as fh:
            fh.write(MALICIOUS_INIT)
        for name in ("python3", "python3.13", "python3.12", "python3.11"):
            fake = os.path.join(self.cwd, name)
            with open(fake, "w") as fh:
                fh.write(FAKE_PYTHON)
            os.chmod(fake, 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_launcher(self, args, env, launcher=LAUNCHER, cwd=None):
        return subprocess.run(
            ["/bin/sh", launcher, *args],
            cwd=cwd or self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assertNotHijacked(self):
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "HIJACKED_PKG")), "cwd package was imported")
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "HIJACKED_PY")), "relative PATH python was run")

    def test_unknown_subcommand_uses_the_real_package_despite_a_hostile_cwd(self):
        env = _base_env(f".::{REAL_PY_DIR}:/usr/bin:/bin")
        env["PYTHONPATH"] = self.cwd
        env["PYTHONSTARTUP"] = os.path.join(self.cwd, "quotaribbon", "__init__.py")
        r = self.run_launcher(["no-such-subcommand"], env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("unknown subcommand", r.stderr)
        self.assertNotHijacked()

    def test_explicit_python_override(self):
        env = _base_env(os.path.join(self.tmp, "empty"))
        env["QUOTARIBBON_PYTHON"] = os.path.realpath(sys.executable)
        r = self.run_launcher(["bogus"], env)
        self.assertEqual(r.returncode, 2, r.stderr)
        env["QUOTARIBBON_PYTHON"] = "python3"  # relative → refused
        r = self.run_launcher(["bogus"], env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        self.assertNotHijacked()

    def test_bootstrap_json_python(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "bootstrap.json"), "w") as fh:
            fh.write('{\n  "schema_version": 1,\n  "python": "%s"\n}\n' % os.path.realpath(sys.executable))
        # PATH only has sed/head (and on macOS an old /usr/bin/python3 that must be skipped).
        env = _base_env("/usr/bin:/bin")
        env["HERDR_PLUGIN_CONFIG_DIR"] = cfg
        r = self.run_launcher(["bogus"], env)
        self.assertEqual(r.returncode, 2, r.stderr)
        # An escaped / relative value is skipped, falling through to PATH (no python here).
        with open(os.path.join(cfg, "bootstrap.json"), "w") as fh:
            fh.write('{"python": "py\\\\thon3"}\n')
        env["PATH"] = os.path.join(self.tmp, "nothing")
        r = self.run_launcher(["bogus"], env)
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_no_python_found(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        r = self.run_launcher(["bogus"], _base_env(f".:{empty}"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 or newer was not found", r.stderr)
        self.assertNotHijacked()

    def test_plugin_root_with_spaces_quotes_and_korean(self):
        root = os.path.join(self.tmp, "플러그인 it's \"q\" root")
        for sub in ("bin", "bootstrap", "quotaribbon"):
            shutil.copytree(os.path.join(REPO_ROOT, sub), os.path.join(root, sub),
                            ignore=shutil.ignore_patterns("__pycache__"))
        env = _base_env(f".:{REAL_PY_DIR}:/usr/bin:/bin")
        r = self.run_launcher(["bogus"], env, launcher=os.path.join(root, "bin", "quotaribbon"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("unknown subcommand", r.stderr)
        # Herdr style: relative launcher path with the plugin root as cwd.
        r = self.run_launcher(["bogus"], env, launcher="bin/quotaribbon", cwd=root)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotHijacked()

    def test_pycache_goes_to_state_dir(self):
        root = os.path.join(self.tmp, "root")
        for sub in ("bin", "bootstrap", "quotaribbon"):
            shutil.copytree(os.path.join(REPO_ROOT, sub), os.path.join(root, sub),
                            ignore=shutil.ignore_patterns("__pycache__"))
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        env = _base_env(f"{REAL_PY_DIR}:/usr/bin:/bin")
        env["HERDR_PLUGIN_STATE_DIR"] = state
        r = self.run_launcher(["bogus"], env, launcher=os.path.join(root, "bin", "quotaribbon"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(root, "quotaribbon", "__pycache__")))
        self.assertTrue(os.path.isdir(os.path.join(state, "pycache")))


PYTHON_NAMES = ("python3.13", "python3.12", "python3.11", "python3")


class LauncherTrustTests(unittest.TestCase):
    """No interpreter candidate (nor any helper tool) runs before it passed the trust walk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.marker_dir = os.path.join(self.tmp, "markers")
        os.makedirs(self.marker_dir)
        self.real_python = os.path.realpath(sys.executable)

    def tearDown(self):
        self._tmp.cleanup()

    def fake(self, directory: str, name: str, mode: int = 0o755) -> str:
        """An executable that records that it ran and claims success."""
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        marker = os.path.join(self.marker_dir, name + "@" + os.path.basename(directory))
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n/usr/bin/touch '%s'\nexit 0\n" % marker)
        os.chmod(path, mode)
        return path

    def fake_pythons(self, directory: str, mode: int = 0o755) -> None:
        for name in PYTHON_NAMES:
            self.fake(directory, name, mode)

    def ran(self) -> list[str]:
        return sorted(os.listdir(self.marker_dir))

    def bootstrap(self, python: str, cfg: str | None = None) -> str:
        cfg = cfg or os.path.join(self.tmp, "cfg")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "bootstrap.json"), "w") as fh:
            fh.write('{\n  "schema_version": 1,\n  "python": "%s"\n}\n' % python)
        return cfg

    def run_launcher(self, env):
        # PATH entries in or below the cwd are never searched: run from a neutral cwd.
        return subprocess.run(["/bin/sh", LAUNCHER, "bogus"], cwd="/", env=env,
                              capture_output=True, text=True, timeout=60)

    def test_python_in_an_attacker_writable_path_directory_never_runs(self):
        shared = os.path.join(self.tmp, "shared-bin")
        self.fake_pythons(shared)
        os.chmod(shared, 0o777)
        r = self.run_launcher(_base_env(f"{shared}:{REAL_PY_DIR}"))
        self.assertEqual(r.returncode, 2, r.stderr)  # the real interpreter ran the action
        self.assertEqual(self.ran(), [])
        r = self.run_launcher(_base_env(shared))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 or newer was not found", r.stderr)
        self.assertEqual(self.ran(), [])

    def test_python_reached_through_a_replaceable_directory_symlink_never_runs(self):
        private = os.path.join(self.tmp, "private-bin")
        self.fake_pythons(private)
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.symlink(private, os.path.join(shared, "bin"))
        os.chmod(shared, 0o777)
        r = self.run_launcher(_base_env(os.path.join(shared, "bin")))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.ran(), [])

    def test_world_or_group_writable_python_file_never_runs(self):
        private = os.path.join(self.tmp, "private-bin")
        self.fake_pythons(private, mode=0o777)
        r = self.run_launcher(_base_env(f"{private}:{REAL_PY_DIR}"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.ran(), [])

    def test_explicit_python_in_a_world_writable_directory_is_refused(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.symlink(self.real_python, os.path.join(shared, "python3"))
        fake = self.fake(shared, "python3.12")
        os.chmod(shared, 0o777)
        for candidate in (os.path.join(shared, "python3"), fake):
            env = _base_env(REAL_PY_DIR)
            env["QUOTARIBBON_PYTHON"] = candidate
            r = self.run_launcher(env)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        self.assertEqual(self.ran(), [])
        # The same interpreter reached through a private symlink is accepted.
        mine = os.path.join(self.tmp, "mine")
        os.makedirs(mine)
        os.symlink(self.real_python, os.path.join(mine, "python3"))
        env = _base_env(os.path.join(self.tmp, "empty"))
        env["QUOTARIBBON_PYTHON"] = os.path.join(mine, "python3")
        self.assertEqual(self.run_launcher(env).returncode, 2)

    def test_pinned_interpreter_is_preferred_over_path(self):
        trusted_path_dir = os.path.join(self.tmp, "path-bin")
        self.fake_pythons(trusted_path_dir)  # trusted, but must not even be probed
        env = _base_env(trusted_path_dir)
        env["HERDR_PLUGIN_CONFIG_DIR"] = self.bootstrap(self.real_python)
        r = self.run_launcher(env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.ran(), [])

    def test_untrusted_pinned_interpreter_is_skipped_without_running(self):
        shared = os.path.join(self.tmp, "shared")
        pinned = self.fake(shared, "python3.12")
        os.chmod(shared, 0o777)
        env = _base_env(REAL_PY_DIR)
        env["HERDR_PLUGIN_CONFIG_DIR"] = self.bootstrap(pinned)
        r = self.run_launcher(env)
        self.assertEqual(r.returncode, 2, r.stderr)  # fell through to PATH discovery
        self.assertEqual(self.ran(), [])

    def test_bootstrap_json_in_a_world_writable_directory_is_ignored(self):
        cfg = self.bootstrap(self.real_python, os.path.join(self.tmp, "shared-cfg"))
        os.chmod(cfg, 0o777)
        env = _base_env(os.path.join(self.tmp, "empty"))
        env["HERDR_PLUGIN_CONFIG_DIR"] = cfg
        r = self.run_launcher(env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 or newer was not found", r.stderr)
        os.chmod(cfg, 0o700)
        self.assertEqual(self.run_launcher(env).returncode, 2)

    def test_helper_tools_are_never_looked_up_in_path(self):
        tools = os.path.join(self.tmp, "tools")
        for name in ("sed", "head", "ls", "id", "readlink", "cat", "grep", "dirname",
                     "getent", "dscl", "dscacheutil", "test", "["):
            self.fake(tools, name)
        env = _base_env(f"{tools}:{REAL_PY_DIR}")
        env["HERDR_PLUGIN_CONFIG_DIR"] = self.bootstrap(self.real_python)
        self.assertEqual(self.run_launcher(env).returncode, 2)
        del env["HERDR_PLUGIN_CONFIG_DIR"]
        self.assertEqual(self.run_launcher(env).returncode, 2)
        self.assertEqual(self.ran(), [])

    def test_symlinked_interpreter_chain_in_private_directories_is_accepted(self):
        # Like a version-manager install: bin/python3 -> python3.12 -> ../lib/real.
        prefix = os.path.join(self.tmp, "prefix")
        os.makedirs(os.path.join(prefix, "bin"))
        os.makedirs(os.path.join(prefix, "lib"))
        os.symlink(self.real_python, os.path.join(prefix, "lib", "python-real"))
        os.symlink(os.path.join("..", "lib", "python-real"), os.path.join(prefix, "bin", "python3.12"))
        os.symlink("python3.12", os.path.join(prefix, "bin", "python3"))
        env = _base_env(os.path.join(prefix, "bin"))
        self.assertEqual(self.run_launcher(env).returncode, 2)
        env["HERDR_PLUGIN_CONFIG_DIR"] = self.bootstrap(os.path.join(prefix, "bin", "python3"))
        env["PATH"] = os.path.join(self.tmp, "empty")
        self.assertEqual(self.run_launcher(env).returncode, 2)

    def test_group_writable_directories_follow_the_same_group_rule_as_the_python_check(self):
        from quotaribbon import trust

        trust._GROUP_PRIVATE.clear()
        self.addCleanup(trust._GROUP_PRIVATE.clear)
        # One group of each verdict is enough: every launcher run costs a full start-up.
        verdicts = {}
        kinds_seen = set()
        for gid in list(dict.fromkeys(os.getgroups()))[:6]:
            private = trust._group_is_private(gid)
            if private in kinds_seen:
                continue
            d = os.path.join(self.tmp, f"g{gid}")
            self.fake(d, "python3")
            try:
                os.chown(d, -1, gid)
            except OSError:
                continue
            os.chmod(d, 0o775)
            kinds_seen.add(private)
            verdicts[gid] = (d, private)
        if not verdicts:
            self.skipTest("no group to test with")
        for gid, (d, private) in verdicts.items():
            with self.subTest(gid=gid, private=private):
                self.run_launcher(_base_env(d))
                ran = os.path.exists(os.path.join(self.marker_dir, "python3@" + os.path.basename(d)))
                self.assertEqual(ran, private)


if __name__ == "__main__":
    unittest.main()
