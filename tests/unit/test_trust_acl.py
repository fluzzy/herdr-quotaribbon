"""Executable trust on macOS: ACL entries that let another account modify a path."""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from quotaribbon import trust

SCRIPT = "#!/bin/sh\nexit 0\n"
ME = pwd.getpwuid(os.geteuid()).pw_name
ON_MACOS = sys.platform == "darwin" and os.access("/bin/chmod", os.X_OK)


def chmod_acl(path: str, entry: str, *, no_follow: bool = False) -> None:
    args = ["/bin/chmod"] + (["-h"] if no_follow else []) + ["+a", entry, path]
    subprocess.run(args, check=True, capture_output=True)


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        trust._ACL_CACHE.clear()
        trust._GROUP_PRIVATE.clear()
        self.addCleanup(trust._ACL_CACHE.clear)
        self.addCleanup(trust._GROUP_PRIVATE.clear)

    def tearDown(self):
        if ON_MACOS:
            # Drop every ACL so the tree can be removed.
            subprocess.run(["/bin/chmod", "-R", "-N", self.tmp], capture_output=True)
        self._tmp.cleanup()

    def exe(self, directory: str, name: str = "tool") -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(SCRIPT)
        os.chmod(path, 0o755)
        return path


@unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
class MacAclTrustTests(TempTree):
    def test_executable_with_a_write_grant_for_everyone_is_rejected(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        self.assertTrue(trust.trusted_executable(tool))
        chmod_acl(tool, "everyone allow write,delete")
        self.assertFalse(trust.trusted_executable(tool))

    def test_each_modifying_permission_on_the_executable_is_rejected(self):
        for perm in ("write", "append", "writeattr", "writeextattr", "delete",
                     "writesecurity", "chown"):
            with self.subTest(perm=perm):
                tool = self.exe(os.path.join(self.tmp, "bin-" + perm))
                chmod_acl(tool, "group:staff allow " + perm)
                self.assertFalse(trust.trusted_executable(tool))

    def test_ancestor_directory_that_lets_a_group_add_entries_is_rejected(self):
        for perm in ("add_file", "add_subdirectory", "delete_child", "delete", "write"):
            with self.subTest(perm=perm):
                parent = os.path.join(self.tmp, "opt-" + perm)
                tool = self.exe(os.path.join(parent, "bin"))
                chmod_acl(parent, "group:staff allow " + perm)
                self.assertFalse(trust.trusted_executable(tool))
                self.assertFalse(trust.trusted_directory(os.path.join(parent, "bin")))
                self.assertFalse(trust.trusted_directory(parent))

    def test_symlink_with_its_own_delete_grant_is_rejected(self):
        tool = self.exe(os.path.join(self.tmp, "real"))
        link = os.path.join(self.tmp, "link")
        os.symlink(tool, link)
        self.assertTrue(trust.trusted_executable(link))
        chmod_acl(link, "everyone allow delete", no_follow=True)
        self.assertFalse(trust.trusted_executable(link))
        self.assertTrue(trust.trusted_executable(tool))

    def test_link_target_directory_with_a_grant_is_rejected(self):
        target_dir = os.path.join(self.tmp, "cellar")
        tool = self.exe(target_dir)
        link = os.path.join(self.tmp, "bin", "tool")
        os.makedirs(os.path.dirname(link))
        os.symlink(tool, link)
        chmod_acl(target_dir, "group:staff allow add_file")
        self.assertFalse(trust.trusted_executable(link))

    def test_deny_only_entries_are_accepted(self):
        parent = os.path.join(self.tmp, "home")
        tool = self.exe(os.path.join(parent, "bin"))
        chmod_acl(parent, "user:nobody deny delete")
        chmod_acl(tool, "user:nobody deny write,delete,append")
        self.assertTrue(trust.trusted_executable(tool))
        self.assertTrue(trust.trusted_directory(parent))

    def test_grants_to_the_owner_or_root_are_accepted(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        chmod_acl(tool, f"user:{ME} allow write,append,writeattr,writeextattr,writesecurity,chown")
        chmod_acl(tool, "user:root allow write,delete")
        chmod_acl(os.path.join(self.tmp, "bin"), f"user:{ME} allow add_file,delete_child")
        self.assertTrue(trust.trusted_executable(tool))

    def test_read_and_traversal_grants_with_inheritance_flags_are_accepted(self):
        parent = os.path.join(self.tmp, "shared-read")
        tool = self.exe(parent)
        chmod_acl(parent, "group:staff allow list,search,readattr,readextattr,readsecurity,"
                          "file_inherit,directory_inherit")
        chmod_acl(tool, "everyone allow read,execute")
        self.assertTrue(trust.trusted_executable(tool))

    def test_inherited_write_grant_is_rejected(self):
        parent = os.path.join(self.tmp, "inherit")
        os.makedirs(parent)
        entry = "group:staff allow write,only_inherit,file_inherit"
        chmod_acl(parent, entry)
        # Even an entry meant only for future children is judged conservatively.
        self.assertFalse(trust.trusted_directory(parent))
        tool = self.exe(parent)  # created after the ACL: inherits the staff grant
        subprocess.run(["/bin/chmod", "-a", entry, parent], check=True, capture_output=True)
        listing = subprocess.run(["/bin/ls", "-lde", tool], capture_output=True, text=True).stdout
        self.assertIn("inherited allow write", listing)
        self.assertTrue(trust.trusted_directory(parent))
        self.assertFalse(trust.trusted_executable(tool))

    def test_a_grant_added_after_a_trusted_verdict_is_noticed(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        self.assertTrue(trust.trusted_executable(tool))
        chmod_acl(os.path.join(self.tmp, "bin"), "group:staff allow add_file")
        self.assertFalse(trust.trusted_executable(tool))

    def test_an_unreadable_acl_listing_rejects_a_path_that_has_an_acl(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        chmod_acl(tool, "user:nobody deny write")
        self.assertTrue(trust.trusted_executable(tool))
        for broken in (None, "", "-rwxr-xr-x+ 1 me staff 18 Nov  2 12:00 tool\n",
                       "-rwxr-xr-x+ 1 me staff 18 Nov  2 12:00 tool\n 0: user:nobody deny\n",
                       "garbage\n"):
            with self.subTest(listing=broken):
                trust._ACL_CACHE.clear()
                real = trust._ls_acl_listings

                def fake(paths, _real=real, _broken=broken):
                    return [_broken if p == tool else text for p, text in zip(paths, _real(paths))]

                with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
                    self.assertFalse(trust.trusted_executable(tool))

    def test_a_failing_ls_rejects_the_path(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        with mock.patch.object(trust, "_LS", os.path.join(self.tmp, "no-such-ls")):
            self.assertFalse(trust.trusted_executable(tool))

    def test_one_ls_lists_every_component_of_a_walk_and_the_verdicts_are_cached(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        with mock.patch.object(trust, "_run_ls", side_effect=trust._run_ls) as spy:
            self.assertTrue(trust.trusted_executable(tool))
            self.assertEqual(spy.call_count, 1)
            listed = spy.call_args.args[0]
            self.assertEqual(listed[0], "/")
            self.assertEqual(listed[-1], tool)
            self.assertEqual(len(listed), len(set(listed)))
            self.assertTrue(trust.trusted_executable(tool))
            self.assertTrue(trust.trusted_directory(os.path.join(self.tmp, "bin")))
            self.assertEqual(spy.call_count, 1)

    def test_a_batch_that_cannot_be_split_is_listed_path_by_path(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        chmod_acl(tool, "everyone allow write")
        real = trust._run_ls

        def garbled(paths):
            return "nonsense\n" if len(paths) > 1 else real(paths)

        with mock.patch.object(trust, "_run_ls", side_effect=garbled) as spy:
            self.assertFalse(trust.trusted_executable(tool))
        self.assertGreater(spy.call_count, 2)
        self.assertTrue(all(len(c.args[0]) == 1 for c in spy.call_args_list[1:]))
        trust._ACL_CACHE.clear()
        os.chmod(tool, 0o755)
        subprocess.run(["/bin/chmod", "-N", tool], check=True)
        with mock.patch.object(trust, "_run_ls", side_effect=garbled):
            self.assertTrue(trust.trusted_executable(tool))

    def test_a_failed_listing_rejects_now_but_is_retried_later(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        with mock.patch.object(trust, "_run_ls", return_value=None):
            self.assertFalse(trust.trusted_executable(tool))
            self.assertFalse(trust.trusted_directory(os.path.join(self.tmp, "bin")))
        self.assertTrue(trust.trusted_executable(tool))

    def test_concurrent_checks_never_list_the_same_path_twice(self):
        import threading

        tools = [self.exe(os.path.join(self.tmp, "bin"), f"tool{i}") for i in range(4)]
        listed: list[str] = []
        real = trust._run_ls

        def recording(paths):
            listed.extend(paths)
            return real(paths)

        results: list[bool] = []
        start = threading.Barrier(len(tools))

        def check(path):
            start.wait()
            results.append(trust.trusted_executable(path))

        with mock.patch.object(trust, "_run_ls", side_effect=recording):
            threads = [threading.Thread(target=check, args=(t,)) for t in tools]
            for th in threads:
                th.start()
            for th in threads:
                th.join(30)
        self.assertEqual(results, [True] * len(tools))
        self.assertEqual(sorted(listed), sorted(set(listed)))
        self.assertIn("/", listed)

    def test_system_binaries_stay_trusted(self):
        self.assertTrue(trust.trusted_executable("/bin/sh"))
        self.assertTrue(trust.trusted_executable("/usr/bin/env"))


class AclListingParserTests(unittest.TestCase):
    HEAD = "-rwxr-xr-x+ 1 me  staff  18 Nov  2 12:00 /x/tool\n"

    def ok(self, text: str) -> bool:
        return trust._acl_listing_ok(text)

    def test_listing_without_acl_is_accepted(self):
        self.assertTrue(self.ok("-rwxr-xr-x  1 me  staff  18 Nov  2 12:00 /x/tool\n"))
        self.assertTrue(self.ok("drwxr-xr-x@ 4 me  staff  128 Nov  2 12:00 /x\n"))

    def test_marker_without_entries_is_rejected(self):
        self.assertFalse(self.ok(self.HEAD))

    def test_deny_entries_and_harmless_grants_are_accepted(self):
        self.assertTrue(self.ok(self.HEAD + " 0: group:everyone deny delete\n"
                                " 1: group:everyone allow read,execute\n"
                                " 2: user:nobody inherited allow list,search,file_inherit\n"
                                " 10: group:staff allow readattr,readextattr,readsecurity\n"))

    def test_owner_and_root_grants_are_accepted(self):
        self.assertTrue(self.ok(self.HEAD + f" 0: user:{ME} allow write,delete,chown\n"
                                " 1: user:root inherited allow add_file,delete_child\n"))

    def test_modifying_and_unknown_grants_to_others_are_rejected(self):
        for perms in ("write", "append", "writeattr", "writeextattr", "delete", "delete_child",
                      "add_file", "add_subdirectory", "writesecurity", "chown",
                      "read,write", "list,add_file,file_inherit", "something_new"):
            with self.subTest(perms=perms):
                self.assertFalse(self.ok(self.HEAD + f" 0: group:staff allow {perms}\n"))
                self.assertFalse(self.ok(self.HEAD + f" 0: user:other inherited allow {perms}\n"))
                self.assertFalse(self.ok(self.HEAD + f" 0: ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C allow {perms}\n"))

    def test_lines_that_cannot_be_parsed_are_rejected(self):
        for line in ("0 group:staff allow read", " x: group:staff allow read", " 0: group:staff permit read",
                     " 0: group:staff allow", " 0: group:staff allow read,,execute",
                     " 0: group:staff allow read extra", " 0: group:staff inherited", "",
                     " 0: two words allow read"):
            with self.subTest(line=line):
                self.assertFalse(self.ok(self.HEAD + line + "\n"))

    def test_unexpected_mode_strings_are_rejected(self):
        for head in ("", "\n", "-rwx\n", "-rwxr-xr-x* 1 me staff 1 Nov 2 12:00 t\n",
                     "-rwxr-xr-x+x 1 me staff 1 Nov 2 12:00 t\n"):
            with self.subTest(head=head):
                self.assertFalse(self.ok(head))


class ListingSplitTests(unittest.TestCase):
    def test_combined_listing_is_split_per_path_in_order(self):
        d = os.path.join(tempfile.gettempdir(), "me")
        text = ("drwxr-xr-x  22 root  wheel  704 Aug 13 11:51 /\n"
                f"drwxr-x---+ 89 me  staff  2848 Nov  2 23:48 {d}\n"
                " 0: group:everyone deny delete\n"
                f"lrwxr-xr-x  1 me  staff  1 Nov  2 23:32 {d}/l -> t\n"
                f"-rwxr-xr-x@ 1 me  staff  0 Nov  2 23:32 {d}/t a\n")
        blocks = trust._split_listing(text, ["/", d, f"{d}/l", f"{d}/t a"])
        self.assertEqual(len(blocks), 4)
        self.assertEqual(blocks[1], f"drwxr-x---+ 89 me  staff  2848 Nov  2 23:48 {d}\n"
                                    " 0: group:everyone deny delete\n")

    def test_listing_that_does_not_line_up_is_not_split(self):
        one = "drwxr-xr-x  2 me  staff  64 Nov  2 23:32 /a\n"
        self.assertIsNone(trust._split_listing(one, ["/a", "/b"]))
        self.assertIsNone(trust._split_listing(one, ["/b"]))
        self.assertIsNone(trust._split_listing(" 0: user:x allow read\n" + one, ["/a"]))
        two = one + "drwxr-xr-x  2 me  staff  64 Nov  2 23:32 /b\n"
        self.assertIsNone(trust._split_listing(two, ["/b", "/a"]))

    def test_unprintable_names_are_matched_as_ls_prints_them(self):
        # ``ls -q`` in the C locale prints one "?" per unprintable byte.
        text = "drwxr-xr-x  2 me  staff  64 Nov  2 23:32 /a?b??????\n"
        self.assertIsNotNone(trust._split_listing(text, ["/a\nb\ud55c\uae00"]))

    @unittest.skipUnless(ON_MACOS, "macOS ls(1)")
    def test_real_listing_of_an_unusual_name_is_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(os.path.realpath(tmp), "a\nb \ud55c\uae00\tc -> d")
            os.makedirs(odd)
            text = trust._run_ls([odd, "/usr"])
            self.assertIsNotNone(text)
            self.assertIsNotNone(trust._split_listing(text, [odd, "/usr"]))


class AclRunnerTests(TempTree):
    def test_ls_is_run_by_absolute_path_without_a_shell(self):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = (b"-rwxr-xr-x  1 me staff 1 Nov 2 12:00 /p\n", b"")
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            text = trust._run_ls(["/-p"])
        self.assertEqual(popen.call_args.args[0], ["/bin/ls", "-ldeqf", "--", "/-p"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertIn("timeout", proc.communicate.call_args.kwargs)
        self.assertTrue(text.startswith("-rwxr-xr-x"))

    def test_timeout_nonzero_exit_and_oversized_output_are_failures(self):
        slow = mock.Mock()
        slow.communicate.side_effect = subprocess.TimeoutExpired("ls", 5)
        with mock.patch("subprocess.Popen", return_value=slow):
            self.assertIsNone(trust._run_ls(["/p"]))
        slow.kill.assert_called_once()
        failed = mock.Mock(returncode=1)
        failed.communicate.return_value = (b"", b"")
        with mock.patch("subprocess.Popen", return_value=failed):
            self.assertIsNone(trust._run_ls(["/p"]))
        huge = mock.Mock(returncode=0)
        huge.communicate.return_value = (b"x" * (trust._LS_MAX_OUTPUT + 1), b"")
        with mock.patch("subprocess.Popen", return_value=huge):
            self.assertIsNone(trust._run_ls(["/p"]))
        with mock.patch("subprocess.Popen", side_effect=OSError("no ls")):
            self.assertIsNone(trust._run_ls(["/p"]))

    def test_other_systems_never_read_acls(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        with mock.patch.object(trust, "_ACL_CHECKS", False), \
                mock.patch.object(trust, "_run_ls", side_effect=AssertionError("ran ls")):
            self.assertTrue(trust.trusted_executable(tool))
            self.assertTrue(trust.trusted_directory(self.tmp))


def clean_listing(path: str) -> str:
    """What ``ls -ldeq`` prints for an entry without an ACL."""
    return "-rwxr-xr-x  1 me  staff  18 Nov  2 12:00 %s\n" % trust._ls_name(path)


class SwapDuringAclListingTests(TempTree):
    """A component replaced between the walk and the ACL listing is never trusted."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(trust, "_ACL_CHECKS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def listing_that_first_runs(self, action):
        calls = []

        def fake(paths):
            if not calls:
                action()
            calls.append(list(paths))
            return [clean_listing(p) for p in paths]

        return fake, calls

    def test_executable_swapped_for_another_file_while_acls_are_listed_is_rejected(self):
        bin_dir = os.path.join(self.tmp, "bin")
        tool = self.exe(bin_dir)
        other = self.exe(bin_dir, "other")
        fake, _ = self.listing_that_first_runs(lambda: os.replace(other, tool))
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertFalse(trust.trusted_executable(tool))

    def test_ancestor_directory_swapped_while_acls_are_listed_is_rejected(self):
        bin_dir = os.path.join(self.tmp, "bin")
        tool = self.exe(bin_dir)
        self.exe(os.path.join(self.tmp, "decoy"))

        def swap():
            os.rename(bin_dir, os.path.join(self.tmp, "old"))
            os.rename(os.path.join(self.tmp, "decoy"), bin_dir)

        fake, _ = self.listing_that_first_runs(swap)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertFalse(trust.trusted_executable(tool))
        fake, _ = self.listing_that_first_runs(lambda: None)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertTrue(trust.trusted_executable(tool))  # the new entry, judged afresh

    def test_symlink_swapped_while_acls_are_listed_is_rejected(self):
        tool = self.exe(os.path.join(self.tmp, "real"))
        link = os.path.join(self.tmp, "link")
        os.symlink(tool, link)

        def swap():
            os.unlink(link)
            os.symlink(tool, link)  # same target, but a different entry

        fake, _ = self.listing_that_first_runs(swap)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertFalse(trust.trusted_executable(link))

    def test_path_entry_swapped_while_acls_are_listed_is_dropped(self):
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        os.makedirs(os.path.join(self.tmp, "a2"))

        def swap():
            os.rename(a, os.path.join(self.tmp, "a-old"))
            os.rename(os.path.join(self.tmp, "a2"), a)

        fake, _ = self.listing_that_first_runs(swap)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertFalse(trust.trusted_directory(a))

    def test_verdicts_from_a_round_with_a_swap_are_not_cached(self):
        bin_dir = os.path.join(self.tmp, "bin")
        tool = self.exe(bin_dir)
        other = self.exe(bin_dir, "other")
        fake, _ = self.listing_that_first_runs(lambda: os.replace(other, tool))
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertFalse(trust.trusted_executable(tool))
        self.assertEqual(trust._ACL_CACHE, {})
        fake, calls = self.listing_that_first_runs(lambda: None)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertTrue(trust.trusted_executable(tool))
        self.assertEqual(calls[0][0], "/")
        self.assertEqual(calls[0][-1], tool)

    def test_unchanged_components_are_accepted_and_listed_ancestors_first(self):
        tool = self.exe(os.path.join(self.tmp, "bin"))
        fake, calls = self.listing_that_first_runs(lambda: None)
        with mock.patch.object(trust, "_ls_acl_listings", side_effect=fake):
            self.assertTrue(trust.trusted_executable(tool))
        listed = calls[0]
        self.assertEqual(listed[0], "/")
        self.assertEqual(listed[-1], tool)
        self.assertLess(listed.index(os.path.dirname(tool)), listed.index(tool))


class TrustedShebangTests(TempTree):
    """The interpreter a script names on its #! line must be trusted too."""

    def script(self, directory: str, name: str, first_line: bytes, mode: int = 0o755) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as fh:
            fh.write(first_line + b"exit 0\n")
        os.chmod(path, mode)
        return path

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        self.trusted = os.path.join(self.tmp, "trusted")
        os.makedirs(self.trusted)
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(self.shared)
        os.chmod(self.shared, 0o777)

    def ok(self, path: str, search_path=None) -> bool:
        return trust.trusted_shebang(path, [self.trusted] if search_path is None else search_path)

    def test_binary_and_plain_files_are_accepted(self):
        binary = os.path.join(self.bin, "binary")
        os.makedirs(self.bin)
        with open(binary, "wb") as fh:
            fh.write(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"\x00" * 600)
        self.assertTrue(self.ok(binary))
        self.assertTrue(self.ok(self.script(self.bin, "plain", b"echo hi\n")))
        self.assertTrue(self.ok(self.script(self.bin, "empty", b"")))
        self.assertTrue(trust.trusted_shebang("/bin/sh", []))

    def test_trusted_absolute_interpreter_is_accepted(self):
        for line in (b"#!/bin/sh\n", b"#! /bin/sh\n", b"#!\t /bin/sh -e  -u\n", b"#!/bin/sh -e\r\n"):
            with self.subTest(line=line):
                self.assertTrue(self.ok(self.script(self.bin, "s", line)))

    def test_world_writable_interpreter_is_refused(self):
        interp = os.path.join(self.bin, "interp")
        os.makedirs(self.bin)
        with open(interp, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(interp, 0o777)
        self.assertFalse(self.ok(self.script(self.bin, "s", b"#!%s\n" % interp.encode())))
        in_shared = self.exe(self.shared, "interp")
        self.assertFalse(self.ok(self.script(self.bin, "t", b"#!%s\n" % in_shared.encode())))

    def test_relative_missing_and_unparsable_interpreters_are_refused(self):
        for line in (b"#!sh\n", b"#!./sh\n", b"#!\n", b"#!  \n", b"#!/no/such/interp\n",
                     b"#!/bin/sh\r\n", b"#!/bin/s\x00h\n", b"#!/bin/sh " + b"x" * 300):
            with self.subTest(line=line):
                self.assertFalse(self.ok(self.script(self.bin, "s", line)))

    def test_line_ending_at_end_of_file_is_judged(self):
        path = os.path.join(self.tmp, "noeol")
        with open(path, "wb") as fh:
            fh.write(b"#!/bin/sh")
        os.chmod(path, 0o755)
        self.assertTrue(self.ok(path))

    def test_env_command_found_in_a_trusted_search_directory_is_accepted(self):
        self.script(self.trusted, "qrnode", b"#!/bin/sh\n")
        self.assertTrue(self.ok(self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")))
        self.assertTrue(self.ok(self.script(self.bin, "t", b"#! /usr/bin/env  qrnode --flag\n")))

    def test_env_command_only_outside_the_search_path_is_refused(self):
        self.script(self.shared, "qrnode", b"#!/bin/sh\n")
        other = os.path.join(self.tmp, "other")
        self.script(other, "qrnode", b"#!/bin/sh\n")
        s = self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")
        self.assertFalse(self.ok(s))
        self.assertFalse(self.ok(s, []))
        self.assertTrue(self.ok(s, [self.trusted, other]))

    def test_env_with_options_assignments_or_paths_is_refused(self):
        self.script(self.trusted, "qrnode", b"#!/bin/sh\n")
        for line in (b"#!/usr/bin/env -S qrnode\n", b"#!/usr/bin/env -i qrnode\n",
                     b"#!/usr/bin/env - qrnode\n", b"#!/usr/bin/env -- qrnode\n",
                     b"#!/usr/bin/env X=1 qrnode\n", b"#!/usr/bin/env ./qrnode\n",
                     b"#!/usr/bin/env %s/qrnode\n" % self.trusted.encode(),
                     b"#!/usr/bin/env\n", b"#!/usr/bin/env qrnode\r\n"):
            with self.subTest(line=line):
                self.assertFalse(self.ok(self.script(self.bin, "s", line)))

    def test_env_lookup_takes_the_first_regular_executable_and_judges_it(self):
        first = os.path.join(self.tmp, "first")
        os.makedirs(os.path.join(first, "qrnode"))  # a directory: skipped, as by execvp
        second = os.path.join(self.tmp, "second")
        self.script(second, "qrnode", b"#!/bin/sh\n", mode=0o644)  # not executable: skipped
        third = os.path.join(self.tmp, "third")
        found = self.script(third, "qrnode", b"#!/bin/sh\n")
        s = self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")
        self.assertTrue(self.ok(s, [first, second, third]))
        os.chmod(found, 0o777)
        self.assertFalse(self.ok(s, [first, second, third]))

    def test_env_lookup_through_an_untrusted_directory_is_refused(self):
        self.script(self.trusted, "qrnode", b"#!/bin/sh\n")
        s = self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")
        # Someone could add qrnode to the shared directory, which is searched first.
        self.assertFalse(self.ok(s, [self.shared, self.trusted]))
        self.assertFalse(self.ok(s, ["relative", self.trusted]))

    def test_env_command_whose_own_interpreter_is_untrusted_is_refused(self):
        bad = self.exe(self.shared, "interp")
        self.script(self.trusted, "qrnode", b"#!%s\n" % bad.encode())
        self.assertFalse(self.ok(self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")))

    def test_interpreter_chain_is_bounded(self):
        def chain(length: int) -> str:
            prev = b"/bin/sh"
            for i in range(length):
                prev = self.script(os.path.join(self.tmp, "chain%d" % length), "s%d" % i,
                                   b"#!%s\n" % prev).encode()
            return prev.decode()

        self.assertTrue(self.ok(chain(trust.MAX_INTERPRETER_DEPTH)))
        self.assertFalse(self.ok(chain(trust.MAX_INTERPRETER_DEPTH + 1)))
        loop = os.path.join(self.bin, "loop")
        self.script(self.bin, "loop", b"#!%s\n" % loop.encode())
        self.assertFalse(self.ok(loop))

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_unreadable_script_is_refused(self):
        s = self.script(self.bin, "s", b"#!/bin/sh\n", mode=0o311)
        self.assertFalse(self.ok(s))

    def test_arguments_are_one_word_where_the_kernel_does_not_split_them(self):
        self.script(self.trusted, "qrnode", b"#!/bin/sh\n")
        s = self.script(self.bin, "s", b"#!/usr/bin/env qrnode --flag\n")
        with mock.patch.object(trust, "_SHEBANG_SPLITS_ARGS", False):
            # env would look for a command called "qrnode --flag".
            self.assertFalse(self.ok(s))
            self.assertTrue(self.ok(self.script(self.bin, "t", b"#!/usr/bin/env qrnode \n")))
        with mock.patch.object(trust, "_SHEBANG_SPLITS_ARGS", True):
            self.assertTrue(self.ok(s))

    def test_bad_arguments_never_raise(self):
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        for path, search in (("", []), ("relative", []), (None, []), (b"/bin/sh", []),
                             ("/no/such", []), (fifo, []), (self.tmp, [])):
            with self.subTest(path=path):
                self.assertFalse(trust.trusted_shebang(path, search))  # type: ignore[arg-type]
        s = self.script(self.bin, "s", b"#!/usr/bin/env qrnode\n")
        for search in (None, "/usr/bin", [None], [b"/usr/bin"], ["/usr/bin\0x"]):
            with self.subTest(search=search):
                self.assertFalse(trust.trusted_shebang(s, search))  # type: ignore[arg-type]

    def test_real_commands_still_pass(self):
        search = trust.trusted_path_entries(os.environ.get("PATH", ""))
        for path in ("/bin/sh", "/usr/bin/env"):
            self.assertTrue(trust.trusted_executable(path))
            self.assertTrue(trust.trusted_shebang(path, search))


class TrustedPathEntriesTests(TempTree):
    def test_keeps_trusted_absolute_entries_in_order_once(self):
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        value = os.pathsep.join([b, a, b, a + "/", "/usr/bin"])
        self.assertEqual(trust.trusted_path_entries(value), [b, a, a + "/", "/usr/bin"])

    def test_drops_empty_relative_missing_file_and_replaceable_entries(self):
        good = os.path.join(self.tmp, "good")
        os.makedirs(good)
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o777)
        under_shared = os.path.join(shared, "bin")
        os.makedirs(under_shared)
        file_entry = self.exe(self.tmp, "plainfile")
        value = os.pathsep.join(["", ".", "bin", good, "", os.path.join(self.tmp, "missing"),
                                 shared, under_shared, file_entry, "rel/" + good, good])
        self.assertEqual(trust.trusted_path_entries(value), [good])

    def test_empty_and_non_string_values_give_no_entries(self):
        self.assertEqual(trust.trusted_path_entries(""), [])
        self.assertEqual(trust.trusted_path_entries(None), [])
        self.assertEqual(trust.trusted_path_entries(":::"), [])
        self.assertEqual(trust.trusted_path_entries(b"/usr/bin"), [])  # type: ignore[arg-type]

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_drops_a_directory_whose_acl_lets_another_account_add_entries(self):
        good = os.path.join(self.tmp, "good")
        bad = os.path.join(self.tmp, "bad")
        os.makedirs(good)
        os.makedirs(bad)
        chmod_acl(bad, "group:staff allow add_file")
        self.assertEqual(trust.trusted_path_entries(os.pathsep.join([bad, good])), [good])


if __name__ == "__main__":
    unittest.main()
