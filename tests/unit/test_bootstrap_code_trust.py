"""Pre-import trust checks of the tab-bar bootstrap, loaded as a module."""

from __future__ import annotations

import importlib.util
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOOTSTRAP = os.path.join(REPO_ROOT, "bootstrap", "tabbar.py")
ME = pwd.getpwuid(os.geteuid()).pw_name
ON_MACOS = sys.platform == "darwin" and os.access("/bin/chmod", os.X_OK)


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("qr_tabbar_bootstrap_under_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tabbar = load_bootstrap()


def chmod_acl(path: str, entry: str, *, no_follow: bool = False) -> None:
    args = ["/bin/chmod"] + (["-h"] if no_follow else []) + ["+a", entry, path]
    subprocess.run(args, check=True, capture_output=True)


class AclListingTests(unittest.TestCase):
    owners = frozenset({"user:root", "user:" + ME})

    def ok(self, text: str) -> bool:
        return tabbar._acl_listing_ok(text, self.owners)

    def test_plain_and_harmless_listings_pass(self):
        self.assertTrue(self.ok("-rw-r--r--  1 me  staff  0 Jan  1 00:00 /x\n"))
        self.assertTrue(self.ok("drwxr-x---+ 3 me staff 96 Jan 1 00:00 /x\n"
                                " 0: group:everyone deny delete\n"
                                " 1: group:staff allow list,search,readattr\n"
                                " 2: user:" + ME + " inherited allow write,delete\n"))

    def test_foreign_modifying_grants_are_rejected(self):
        for perms in ("write", "append", "add_file", "add_subdirectory", "delete_child", "delete",
                      "writeattr", "writeextattr", "writesecurity", "chown", "read,write"):
            with self.subTest(perms=perms):
                self.assertFalse(self.ok("-rw-r--r--+ 1 me staff 0 Jan 1 00:00 /x\n"
                                         f" 0: group:everyone allow {perms}\n"))

    def test_unparsable_listings_are_rejected(self):
        for text in ("", "garbage\n", "-rw-r--r--+ 1 me staff 0 Jan 1 00:00 /x\n",
                     "-rw-r--r--+ 1 me staff 0 Jan 1 00:00 /x\n 0: group:everyone permit read\n",
                     "-rw-r--r--+ 1 me staff 0 Jan 1 00:00 /x\n x: group:everyone allow read\n",
                     "-rw-r--r--+ 1 me staff 0 Jan 1 00:00 /x\n 0: group:everyone allow\n"):
            with self.subTest(text=text):
                self.assertFalse(self.ok(text))


class TreeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.root = os.path.join(self.tmp, "root")
        self.pkg = os.path.join(self.root, "quotaribbon")
        os.makedirs(os.path.join(self.pkg, "providers"))
        for rel in ("__init__.py", "tabbar_entry.py", os.path.join("providers", "__init__.py"),
                    os.path.join("providers", "claude.py")):
            with open(os.path.join(self.pkg, rel), "w") as fh:
                fh.write("X = 1\n")
        with open(os.path.join(self.root, "herdr-plugin.toml"), "w") as fh:
            fh.write('id = "x"\n')

    def tearDown(self):
        if ON_MACOS:
            subprocess.run(["/bin/chmod", "-R", "-N", self.tmp], capture_output=True)
        self._tmp.cleanup()


class TrustedRootTests(TreeBase):
    def test_private_tree_is_trusted(self):
        self.assertEqual(tabbar._trusted_root(self.root), self.root)

    def test_shared_write_anywhere_in_the_package_is_rejected(self):
        for rel in ("providers/claude.py", "providers", "tabbar_entry.py", ""):
            with self.subTest(rel=rel):
                path = os.path.join(self.pkg, rel) if rel else self.pkg
                old = os.stat(path).st_mode & 0o7777
                os.chmod(path, old | 0o020)
                try:
                    self.assertIsNone(tabbar._trusted_root(self.root))
                finally:
                    os.chmod(path, old)

    def test_sourceless_bytecode_and_symlinks_in_the_package_count(self):
        pyc = os.path.join(self.pkg, "providers", "extra.pyc")
        with open(pyc, "wb") as fh:
            fh.write(b"\0")
        os.chmod(pyc, 0o666)
        self.assertIsNone(tabbar._trusted_root(self.root))
        os.chmod(pyc, 0o644)
        self.assertEqual(tabbar._trusted_root(self.root), self.root)
        os.symlink(pyc, os.path.join(self.pkg, "linked.py"))
        self.assertIsNone(tabbar._trusted_root(self.root))

    def test_package_pycache_dir_is_not_walked(self):
        cache = os.path.join(self.pkg, "__pycache__")
        os.makedirs(cache)
        os.chmod(cache, 0o777)  # never read: a pycache prefix is always set before importing
        self.assertEqual(tabbar._trusted_root(self.root), self.root)

    @unittest.skipUnless(ON_MACOS, "ACL verification runs on macOS only")
    def test_unavailable_or_unparsable_acl_listing_fails_closed(self):
        with mock.patch.object(tabbar, "_LS", os.path.join(self.tmp, "no-such-ls")):
            self.assertIsNone(tabbar._trusted_root(self.root))
        with mock.patch.object(tabbar, "_run_ls", return_value="not a listing\n"):
            self.assertIsNone(tabbar._trusted_root(self.root))
        with mock.patch.object(tabbar, "_LS_TIMEOUT", 0.0):
            self.assertIsNone(tabbar._trusted_root(self.root))
        self.assertEqual(tabbar._trusted_root(self.root), self.root)

    @unittest.skipUnless(ON_MACOS, "ACL verification runs on macOS only")
    def test_a_failed_acl_check_has_its_own_reason(self):
        with mock.patch.object(tabbar, "_LS", os.path.join(self.tmp, "no-such-ls")):
            self.assertEqual(tabbar._check_root(self.root), (None, "acl_check_failed"))
        with mock.patch.object(tabbar, "_LS_TIMEOUT", 0.0):
            self.assertEqual(tabbar._check_root(self.root), (None, "acl_check_failed"))
        with mock.patch.object(tabbar, "_run_ls", return_value="not a listing\n"):
            self.assertEqual(tabbar._check_root(self.root), (None, "untrusted_plugin_root"))
        self.assertEqual(tabbar._check_root(self.root), (self.root, ""))
        self.assertLess(tabbar._LS_TIMEOUT, tabbar.SELF_TIMEOUT)


@unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
class TrustedRootAclTests(TreeBase):
    def test_write_grant_on_any_imported_file_is_rejected(self):
        for rel in ("__init__.py", "tabbar_entry.py", "providers/claude.py"):
            with self.subTest(rel=rel):
                path = os.path.join(self.pkg, rel)
                chmod_acl(path, "everyone allow write,append")
                self.assertIsNone(tabbar._trusted_root(self.root))
                subprocess.run(["/bin/chmod", "-N", path], check=True)
                self.assertEqual(tabbar._trusted_root(self.root), self.root)

    def test_add_grant_on_root_package_subpackage_or_ancestor_is_rejected(self):
        for path in (self.tmp, self.root, self.pkg, os.path.join(self.pkg, "providers")):
            with self.subTest(path=path):
                chmod_acl(path, "group:staff allow add_file,delete_child")
                self.assertIsNone(tabbar._trusted_root(self.root))
                subprocess.run(["/bin/chmod", "-N", path], check=True)
        self.assertEqual(tabbar._trusted_root(self.root), self.root)

    def test_deny_and_owner_entries_are_accepted(self):
        chmod_acl(self.pkg, "everyone deny delete")
        chmod_acl(os.path.join(self.pkg, "__init__.py"), f"user:{ME} allow write")
        chmod_acl(self.root, "everyone allow list,search,readattr")
        self.assertEqual(tabbar._trusted_root(self.root), self.root)


class BytecodePrefixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "pycache", "a", "b"), 0o700)
        with open(os.path.join(self.state, "pycache", "a", "b", "m.pyc"), "wb") as fh:
            fh.write(b"\0")

    def tearDown(self):
        if ON_MACOS:
            subprocess.run(["/bin/chmod", "-R", "-N", self.tmp], capture_output=True)
        self._tmp.cleanup()

    def prefix(self):
        return tabbar._bytecode_prefix(self.state)

    def untrusted(self):
        return (tabbar._NO_BYTECODE_PREFIX, False)

    def test_private_or_missing_cache_is_used(self):
        self.assertEqual(self.prefix(), (os.path.join(self.state, "pycache"), True))
        subprocess.run(["/bin/rm", "-rf", os.path.join(self.state, "pycache")], check=True)
        self.assertEqual(self.prefix(), (os.path.join(self.state, "pycache"), True))

    def test_shared_write_in_the_cache_or_state_dir_disables_it(self):
        for rel in ("", "pycache", "pycache/a", "pycache/a/b/m.pyc"):
            with self.subTest(rel=rel):
                path = os.path.join(self.state, rel) if rel else self.state
                old = os.stat(path).st_mode & 0o7777
                os.chmod(path, old | 0o002)
                try:
                    self.assertEqual(self.prefix(), self.untrusted())
                finally:
                    os.chmod(path, old)
        self.assertTrue(self.prefix()[1])

    def test_symlink_in_the_cache_disables_it(self):
        os.symlink(self.tmp, os.path.join(self.state, "pycache", "a", "link"))
        self.assertEqual(self.prefix(), self.untrusted())

    def test_no_bytecode_prefix_can_never_exist(self):
        self.assertFalse(os.path.lexists(tabbar._NO_BYTECODE_PREFIX))
        with self.assertRaises(OSError):
            os.makedirs(tabbar._NO_BYTECODE_PREFIX)

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_acl_grant_in_the_cache_disables_it(self):
        for rel in ("pycache", "pycache/a/b", "pycache/a/b/m.pyc"):
            with self.subTest(rel=rel):
                path = os.path.join(self.state, rel)
                chmod_acl(path, "everyone allow write,add_file")
                self.assertEqual(self.prefix(), self.untrusted())
                subprocess.run(["/bin/chmod", "-N", path], check=True)
        self.assertTrue(self.prefix()[1])


if __name__ == "__main__":
    unittest.main()
