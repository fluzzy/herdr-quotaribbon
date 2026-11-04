"""Code the tab bar and the actions import: foreign-writable sources and bytecode never run."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib._bootstrap_external import _code_to_timestamp_pyc

from tests.helpers.fake_herdr import plugin_entry
from tests.integration.test_bootstrap_tabbar import BootstrapBase, make_fake_root

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.realpath(sys.executable)
ON_MACOS = sys.platform == "darwin" and os.access("/bin/chmod", os.X_OK)


def chmod_acl(path: str, entry: str) -> None:
    subprocess.run(["/bin/chmod", "+a", entry, path], check=True, capture_output=True)


def plant_pyc(prefix: str, source: str, payload: str) -> str:
    """A timestamp-valid cache entry for unchanged ``source`` under pycache ``prefix``."""
    source = os.path.realpath(source)
    st = os.stat(source)
    data = _code_to_timestamp_pyc(compile(payload, source, "exec"), int(st.st_mtime), st.st_size)
    name = os.path.splitext(os.path.basename(source))[0] + f".{sys.implementation.cache_tag}.pyc"
    directory = os.path.join(prefix, os.path.dirname(source).lstrip(os.sep))
    os.makedirs(directory, 0o700, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def marker_payload(marker: str, source: str) -> str:
    """Records that it ran, then behaves like the real module."""
    return (f"open({marker!r}, 'w').close()\n"
            f"exec(compile(open({source!r}).read(), {source!r}, 'exec'))\n")


def drop_acls(path: str) -> None:
    if ON_MACOS:
        subprocess.run(["/bin/chmod", "-R", "-N", path], capture_output=True)


class TabBarCodeTrustTests(BootstrapBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(drop_acls, self.tmp)
        self.root = make_fake_root(self.tmp, "root", "Claude 5h 1%")
        self.server.plugins = [plugin_entry(self.root)]
        self.hijack = os.path.join(self.tmp, "PLANTED-RAN")
        self.pycache = os.path.join(self.state, "pycache")

    def reasons(self) -> list[str]:
        return [line.split(" ")[1] for line in self.bootstrap_log()]

    def plant(self) -> str:
        init = os.path.join(self.root, "quotaribbon", "__init__.py")
        return plant_pyc(self.pycache, init, marker_payload(self.hijack, init))

    def test_planted_bytecode_in_a_private_cache_is_a_valid_plant(self):
        # Control: the planted entry is one Python would load, so the cases below are meaningful.
        os.makedirs(self.pycache, 0o700)
        self.plant()
        self.assertEqual(self.tick().stdout, b"Claude 5h 1%\n")
        self.assertTrue(os.path.exists(self.hijack))

    def test_planted_bytecode_in_a_shared_cache_never_runs(self):
        os.makedirs(self.pycache, 0o700)
        planted = self.plant()
        for path in (self.pycache, os.path.dirname(planted)):
            with self.subTest(path=path):
                os.chmod(path, 0o777)
                self.assertEqual(self.tick().stdout, b"Claude 5h 1%\n")  # compiled from source
                self.assertFalse(os.path.exists(self.hijack))
                os.chmod(path, 0o700)
        self.assertIn("untrusted_pycache", self.reasons())

    def test_planted_bytecode_behind_a_cache_symlink_never_runs(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere, 0o700)
        os.symlink(elsewhere, self.pycache)
        self.plant()
        self.assertEqual(self.tick().stdout, b"Claude 5h 1%\n")
        self.assertFalse(os.path.exists(self.hijack))

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_planted_bytecode_in_a_cache_with_a_write_acl_never_runs(self):
        os.makedirs(self.pycache, 0o700)
        planted = self.plant()
        chmod_acl(os.path.dirname(planted), "everyone allow add_file,delete_child")
        self.assertEqual(self.tick().stdout, b"Claude 5h 1%\n")
        self.assertFalse(os.path.exists(self.hijack))

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_source_with_a_write_acl_is_never_imported(self):
        pkg = os.path.join(self.root, "quotaribbon")
        for path in (os.path.join(pkg, "__init__.py"), os.path.join(pkg, "tabbar_entry.py"), pkg,
                     self.root, os.path.dirname(self.root)):
            with self.subTest(path=path):
                chmod_acl(path, "everyone allow write,append,add_file")
                r = self.tick()
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
                subprocess.run(["/bin/chmod", "-N", path], check=True)
        self.assertEqual(self.entry_runs(), [])
        self.assertIn("untrusted_plugin_root", self.reasons())
        self.assertEqual(self.tick().stdout, b"Claude 5h 1%\n")

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_subpackage_with_a_write_acl_is_never_imported(self):
        sub = os.path.join(self.root, "quotaribbon", "providers")
        os.makedirs(sub)
        with open(os.path.join(sub, "__init__.py"), "w") as fh:
            fh.write("")
        chmod_acl(os.path.join(sub, "__init__.py"), "group:staff allow write")
        self.assertEqual(self.tick().stdout, b"")
        self.assertEqual(self.entry_runs(), [])


class ActionBytecodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(drop_acls, self.tmp)
        self.root = os.path.join(self.tmp, "root")
        for sub in ("bootstrap", "quotaribbon"):
            shutil.copytree(os.path.join(REPO_ROOT, sub), os.path.join(self.root, sub),
                            ignore=shutil.ignore_patterns("__pycache__"))
        self.config = os.path.join(self.tmp, "config")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.config, 0o700)
        os.makedirs(self.state, 0o700)
        self.pycache = os.path.join(self.state, "pycache")
        self.hijack = os.path.join(self.tmp, "PLANTED-RAN")

    def plant(self, module: str = "__init__.py") -> str:
        source = os.path.join(self.root, "quotaribbon", module)
        return plant_pyc(self.pycache, source, marker_payload(self.hijack, source))

    def startup(self) -> subprocess.CompletedProcess:
        env = {"HOME": self.tmp, "PATH": "/usr/bin:/bin", "LANG": "C",
               "HERDR_PLUGIN_CONFIG_DIR": self.config, "HERDR_PLUGIN_STATE_DIR": self.state}
        return subprocess.run([PYTHON, "-I", os.path.join(self.root, "bootstrap", "action.py"), "startup"],
                              cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)

    def test_private_cache_is_used_and_a_plant_there_is_valid(self):
        os.makedirs(self.pycache, 0o700)
        self.plant("cli.py")  # the package itself is compiled from source to check the cache
        r = self.startup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.hijack))  # control: the plant is loadable bytecode

    def test_cache_is_written_to_the_state_dir_when_trusted(self):
        r = self.startup()
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([PYTHON, "-I", os.path.join(self.root, "bootstrap", "action.py"), "bogus"],
                           env={"HOME": self.tmp, "PATH": "/usr/bin:/bin", "HERDR_PLUGIN_STATE_DIR": self.state,
                                "HERDR_PLUGIN_CONFIG_DIR": self.config}, capture_output=True, timeout=60)
        cached = [n for _, _, names in os.walk(self.pycache) for n in names if n.endswith(".pyc")]
        self.assertTrue(cached)
        self.assertFalse(os.path.exists(os.path.join(self.root, "quotaribbon", "__pycache__")))

    def test_planted_bytecode_in_a_shared_cache_never_runs_on_startup(self):
        os.makedirs(self.pycache, 0o700)
        for module in ("__init__.py", "cli.py"):
            planted = self.plant(module)
            for path in (self.pycache, os.path.dirname(planted), planted):
                with self.subTest(module=module, path=path):
                    old = os.stat(path).st_mode & 0o777
                    os.chmod(path, old | 0o022)
                    r = self.startup()
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertIn("startup:", r.stdout)
                    self.assertFalse(os.path.exists(self.hijack))
                    if os.path.exists(path):
                        os.chmod(path, old)
                    # startup clears the cache: plant again for the next case
                    planted = self.plant(module)

    def test_planted_bytecode_behind_a_cache_symlink_never_runs(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere, 0o700)
        os.symlink(elsewhere, self.pycache)
        self.plant("cli.py")
        r = self.startup()
        self.assertFalse(os.path.exists(self.hijack))
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_planted_bytecode_in_a_cache_with_a_write_acl_never_runs(self):
        os.makedirs(self.pycache, 0o700)
        planted = self.plant("cli.py")
        for path in (self.pycache, os.path.dirname(planted), planted, self.state):
            with self.subTest(path=path):
                chmod_acl(path, "everyone allow write,add_file")
                r = self.startup()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(os.path.exists(self.hijack))
                drop_acls(self.state)
                planted = self.plant("cli.py")


if __name__ == "__main__":
    unittest.main()
