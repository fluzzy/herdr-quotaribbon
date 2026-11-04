"""Executable trust: only files nobody but the user (or root) can replace are run."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from quotaribbon import trust
from tests.helpers.fake_herdr import short_tempdir

SCRIPT = "#!/bin/sh\nexit 0\n"


class TrustTests(unittest.TestCase):
    def setUp(self):
        self._tmp = short_tempdir()
        self.tmp = os.path.realpath(self._tmp.name)
        trust._GROUP_PRIVATE.clear()
        self.addCleanup(trust._GROUP_PRIVATE.clear)

    def tearDown(self):
        # Restore modes so the temp dir can be removed.
        for root, dirs, _files in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        self._tmp.cleanup()

    def exe(self, directory: str, name: str = "tool", mode: int = 0o755) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(SCRIPT)
        os.chmod(path, mode)
        return path

    def test_private_executable_is_trusted(self):
        self.assertTrue(trust.trusted_executable(self.exe(os.path.join(self.tmp, "bin"))))

    def test_system_binaries_owned_by_root_are_trusted(self):
        self.assertTrue(trust.trusted_executable("/bin/sh"))

    def test_world_writable_executable_is_rejected(self):
        self.assertFalse(trust.trusted_executable(self.exe(os.path.join(self.tmp, "bin"), mode=0o757)))

    def test_group_writable_executable_depends_on_who_is_in_the_group(self):
        path = self.exe(os.path.join(self.tmp, "bin"), mode=0o775)
        with mock.patch.object(trust, "_group_is_private", return_value=False):
            self.assertFalse(trust.trusted_executable(path))
        with mock.patch.object(trust, "_group_is_private", return_value=True):
            self.assertTrue(trust.trusted_executable(path))

    def test_non_executable_missing_relative_and_directory_paths_are_rejected(self):
        bindir = os.path.join(self.tmp, "bin")
        self.assertFalse(trust.trusted_executable(self.exe(bindir, "plain", mode=0o644)))
        self.assertFalse(trust.trusted_executable(os.path.join(bindir, "missing")))
        self.assertFalse(trust.trusted_executable("bin/tool"))
        self.assertFalse(trust.trusted_executable(bindir))
        self.assertFalse(trust.trusted_executable(""))

    def test_writable_ancestor_without_sticky_bit_is_rejected(self):
        shared = os.path.join(self.tmp, "shared")
        path = self.exe(os.path.join(shared, "opt", "bin"))
        os.chmod(shared, 0o777)
        self.assertFalse(trust.trusted_executable(path))
        os.chmod(shared, 0o1777)  # sticky: others cannot rename our entries
        self.assertTrue(trust.trusted_executable(path))
        os.chmod(shared, 0o775)
        with mock.patch.object(trust, "_group_is_private", return_value=False):
            self.assertFalse(trust.trusted_executable(path))

    def test_symlink_into_a_replaceable_location_is_rejected(self):
        shared = os.path.join(self.tmp, "shared")
        target = self.exe(os.path.join(shared, "pkg"))
        os.chmod(shared, 0o777)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        link = os.path.join(bindir, "tool")
        os.symlink(target, link)
        self.assertFalse(trust.trusted_executable(link))
        os.chmod(shared, 0o755)
        self.assertTrue(trust.trusted_executable(link))

    def test_symlink_held_in_a_replaceable_directory_is_rejected(self):
        target = self.exe(os.path.join(self.tmp, "pkg"))
        hop = os.path.join(self.tmp, "hop")
        os.makedirs(hop)
        os.symlink(target, os.path.join(hop, "tool"))
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        os.symlink(os.path.join("..", "hop", "tool"), os.path.join(bindir, "tool"))
        self.assertTrue(trust.trusted_executable(os.path.join(bindir, "tool")))
        os.chmod(hop, 0o777)
        self.assertFalse(trust.trusted_executable(os.path.join(bindir, "tool")))

    def _owned_by_someone_else(self, *victims: str):
        """Patch ``lstat`` so the given paths look owned by another account."""
        real_lstat = os.lstat
        others = os.geteuid() + 4242

        def fake_lstat(p, *a, **kw):
            st = real_lstat(p, *a, **kw)
            if os.fspath(p) in victims:
                fields = list(st)
                fields[4] = others  # st_uid
                return os.stat_result(fields)
            return st

        return mock.patch("quotaribbon.trust.os.lstat", side_effect=fake_lstat)

    def install(self) -> str:
        """A trusted install: <tmp>/inst/bin/tool."""
        return self.exe(os.path.join(self.tmp, "inst", "bin"))

    def test_intermediate_symlink_owned_by_another_account_is_rejected(self):
        self.install()
        links = os.path.join(self.tmp, "links")
        os.makedirs(links)
        dirlink = os.path.join(links, "inst")
        os.symlink(os.path.join("..", "inst"), dirlink)
        path = os.path.join(dirlink, "bin", "tool")
        self.assertTrue(trust.trusted_executable(path))
        with self._owned_by_someone_else(dirlink):
            self.assertFalse(trust.trusted_executable(path))
            self.assertFalse(trust.trusted_directory(os.path.join(dirlink, "bin")))
        # The resolved location itself stays trusted: only the swappable route is refused.
        self.assertTrue(trust.trusted_executable(os.path.join(self.tmp, "inst", "bin", "tool")))

    def test_intermediate_symlink_in_a_world_writable_directory_is_rejected(self):
        self.install()
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        dirlink = os.path.join(shared, "inst")
        os.symlink(os.path.join(self.tmp, "inst"), dirlink)
        path = os.path.join(dirlink, "bin", "tool")
        os.chmod(shared, 0o777)
        self.assertFalse(trust.trusted_executable(path))
        self.assertFalse(trust.trusted_directory(os.path.join(dirlink, "bin")))
        os.chmod(shared, 0o1777)  # sticky: our link cannot be swapped by others
        self.assertTrue(trust.trusted_executable(path))
        self.assertTrue(trust.trusted_directory(os.path.join(dirlink, "bin")))

    def test_link_target_that_passes_through_a_replaceable_symlink_is_rejected(self):
        self.install()
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.symlink(os.path.join(self.tmp, "inst"), os.path.join(shared, "inst"))
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        link = os.path.join(bindir, "tool")
        os.symlink(os.path.join("..", "shared", "inst", "bin", "tool"), link)
        self.assertTrue(trust.trusted_executable(link))
        os.chmod(shared, 0o777)
        self.assertFalse(trust.trusted_executable(link))

    def test_user_local_launcher_symlink_to_a_versioned_binary_is_trusted(self):
        # Like ~/.local/bin/claude -> ~/.local/share/claude/versions/<version>.
        target = self.exe(os.path.join(self.tmp, ".local", "share", "claude", "versions"), "2.1.0")
        bindir = os.path.join(self.tmp, ".local", "bin")
        os.makedirs(bindir)
        os.symlink(target, os.path.join(bindir, "claude"))
        self.assertTrue(trust.trusted_executable(os.path.join(bindir, "claude")))

    def test_package_manager_bin_symlink_into_its_cellar_is_trusted(self):
        # Like /opt/homebrew/bin/tool -> ../Cellar/tool/1.0/bin/tool, plus a versioned
        # directory link (opt/tool -> ../Cellar/tool/1.0) on the way.
        prefix = os.path.join(self.tmp, "brew")
        self.exe(os.path.join(prefix, "Cellar", "tool", "1.0", "bin"))
        os.makedirs(os.path.join(prefix, "bin"))
        os.makedirs(os.path.join(prefix, "opt"))
        os.symlink(os.path.join("..", "Cellar", "tool", "1.0"), os.path.join(prefix, "opt", "tool"))
        os.symlink(os.path.join("..", "opt", "tool", "bin", "tool"), os.path.join(prefix, "bin", "tool"))
        self.assertTrue(trust.trusted_executable(os.path.join(prefix, "bin", "tool")))
        # Group-writable prefix directories are judged by who is in the group.
        os.chmod(os.path.join(prefix, "Cellar"), 0o775)
        with mock.patch.object(trust, "_group_is_private", return_value=True):
            self.assertTrue(trust.trusted_executable(os.path.join(prefix, "bin", "tool")))
        with mock.patch.object(trust, "_group_is_private", return_value=False):
            self.assertFalse(trust.trusted_executable(os.path.join(prefix, "bin", "tool")))

    def test_dot_dot_after_a_directory_symlink_follows_the_link_target(self):
        self.exe(os.path.join(self.tmp, "real"), "tool")
        os.makedirs(os.path.join(self.tmp, "real", "sub"))
        os.makedirs(os.path.join(self.tmp, "front"))
        os.symlink(os.path.join(self.tmp, "real", "sub"), os.path.join(self.tmp, "front", "sub"))
        # The kernel resolves ".." against the link target: front/sub/.. is real/.
        self.assertTrue(trust.trusted_executable(os.path.join(self.tmp, "front", "sub", "..", "tool")))

    def test_symlink_loops_and_overlong_chains_are_rejected(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        os.symlink(b, a)
        os.symlink(a, b)
        self.assertFalse(trust.trusted_executable(a))
        prev = self.exe(os.path.join(self.tmp, "chain"), "l0")
        for i in range(1, trust.MAX_SYMLINK_HOPS + 2):
            cur = os.path.join(self.tmp, "chain", f"l{i}")
            os.symlink(prev, cur)
            prev = cur
        self.assertTrue(trust.trusted_executable(os.path.join(self.tmp, "chain", f"l{trust.MAX_SYMLINK_HOPS}")))
        self.assertFalse(trust.trusted_executable(prev))

    def test_file_used_as_a_directory_and_trailing_slash_are_rejected(self):
        path = self.install()
        self.assertFalse(trust.trusted_executable(os.path.join(path, "x")))
        self.assertFalse(trust.trusted_executable(path + "/"))
        self.assertFalse(trust.trusted_executable(path + "\0"))

    def test_trusted_directory_checks_the_path_as_given(self):
        self.install()
        self.assertTrue(trust.trusted_directory(os.path.join(self.tmp, "inst", "bin")))
        self.assertFalse(trust.trusted_directory("inst/bin"))
        self.assertFalse(trust.trusted_directory(os.path.join(self.tmp, "inst", "bin", "tool")))
        os.chmod(os.path.join(self.tmp, "inst"), 0o777)
        self.assertFalse(trust.trusted_directory(os.path.join(self.tmp, "inst", "bin")))

    def test_files_owned_by_another_account_are_rejected(self):
        path = self.exe(os.path.join(self.tmp, "bin"))
        with mock.patch("quotaribbon.trust.os.geteuid", return_value=os.geteuid() + 4242):
            self.assertFalse(trust.trusted_executable(path))
            self.assertTrue(trust.trusted_executable("/bin/sh"))  # root-owned stays trusted

    def test_group_privacy_uses_the_group_membership(self):
        import grp
        import pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        fake_group = mock.Mock(gr_mem=[me, "root"])
        with mock.patch.object(grp, "getgrgid", return_value=fake_group), \
                mock.patch.object(pwd, "getpwall", return_value=[]):
            self.assertTrue(trust._group_is_private(4242))
        trust._GROUP_PRIVATE.clear()
        fake_group = mock.Mock(gr_mem=[me, "someone-else"])
        with mock.patch.object(grp, "getgrgid", return_value=fake_group), \
                mock.patch.object(pwd, "getpwall", return_value=[]):
            self.assertFalse(trust._group_is_private(4242))
        trust._GROUP_PRIVATE.clear()
        with mock.patch.object(grp, "getgrgid", side_effect=KeyError(4242)):
            self.assertFalse(trust._group_is_private(4242))
        trust._GROUP_PRIVATE.clear()
        # No member listed at all (macOS "everyone"): implicitly everybody, not private.
        with mock.patch.object(grp, "getgrgid", return_value=mock.Mock(gr_mem=[])), \
                mock.patch.object(pwd, "getpwall", return_value=[]):
            self.assertFalse(trust._group_is_private(4242))


if __name__ == "__main__":
    unittest.main()
