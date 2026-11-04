"""bin/quotaribbon: PATH filtering for the action and ACL-aware interpreter trust."""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO_ROOT, "bin", "quotaribbon")
REAL_PY_DIR = os.path.dirname(os.path.realpath(sys.executable))
ME = pwd.getpwuid(os.geteuid()).pw_name
ON_MACOS = sys.platform == "darwin" and os.access("/bin/chmod", os.X_OK)
PYTHON_NAMES = ("python3.13", "python3.12", "python3.11", "python3")

# Passes the launcher's version probe, then reports what the action would see.
REPORTING_PYTHON = """#!/bin/sh
case "$2" in -c) exit 0 ;; esac
printf 'PATH=%s\\n' "$PATH"
printf 'KEEP=%s\\n' "${QR_TEST_KEEP-unset}"
exit 7
"""


def chmod_acl(path: str, entry: str, *, no_follow: bool = False) -> None:
    args = ["/bin/chmod"] + (["-h"] if no_follow else []) + ["+a", entry, path]
    subprocess.run(args, check=True, capture_output=True)


def launcher_function(name: str) -> str:
    """The text of shell function ``name`` as defined in the launcher."""
    with open(LAUNCHER) as fh:
        text = fh.read()
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), text, re.S | re.M)
    assert m, name
    return m.group(0)


class LauncherPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.marker_dir = os.path.join(self.tmp, "markers")
        os.makedirs(self.marker_dir)
        self.real_python = os.path.realpath(sys.executable)

    def tearDown(self):
        if ON_MACOS:
            subprocess.run(["/bin/chmod", "-R", "-N", self.tmp], capture_output=True)
        for root, dirs, _files in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        self._tmp.cleanup()

    def write_exe(self, directory: str, name: str, body: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def fake(self, directory: str, name: str) -> str:
        """An executable that records that it ran and claims success."""
        marker = os.path.join(self.marker_dir, name + "@" + os.path.basename(directory))
        return self.write_exe(directory, name, "#!/bin/sh\n/usr/bin/touch '%s'\nexit 0\n" % marker)

    def fake_pythons(self, directory: str) -> None:
        for name in PYTHON_NAMES:
            self.fake(directory, name)

    def reporter(self, directory: str, name: str = "python3") -> str:
        return self.write_exe(directory, name, REPORTING_PYTHON)

    def ran(self) -> list[str]:
        return sorted(os.listdir(self.marker_dir))

    def run_launcher(self, env: dict[str, str]):
        env = {"HOME": os.environ.get("HOME", "/tmp"), "LANG": "C", **env}
        # PATH entries in or below the cwd are never searched: run from a neutral cwd.
        return subprocess.run(["/bin/sh", LAUNCHER, "bogus"], cwd="/", env=env,
                              capture_output=True, text=True, timeout=60)

    def reported(self, r) -> dict[str, str]:
        self.assertEqual(r.returncode, 7, r.stderr)
        return dict(line.split("=", 1) for line in r.stdout.splitlines())

    # -- PATH handed to the action ------------------------------------------------

    def test_action_gets_only_trusted_path_entries_in_order_without_duplicates(self):
        py_dir = os.path.join(self.tmp, "py")
        self.reporter(py_dir)
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o777)
        not_a_dir = os.path.join(self.tmp, "file")
        open(not_a_dir, "w").close()
        path = ":".join([".", "", "relative/bin", b, shared, os.path.join(self.tmp, "missing"),
                         a, b, not_a_dir, py_dir, a])
        r = self.run_launcher({"PATH": path, "QUOTARIBBON_PYTHON": os.path.join(py_dir, "python3"),
                               "QR_TEST_KEEP": "kept value"})
        seen = self.reported(r)
        self.assertEqual(seen["PATH"], ":".join([b, a, py_dir]))
        self.assertEqual(seen["KEEP"], "kept value")

    def test_python_found_on_path_also_runs_with_the_filtered_path(self):
        shared = os.path.join(self.tmp, "shared")
        self.fake_pythons(shared)
        os.chmod(shared, 0o777)
        good = os.path.join(self.tmp, "good")
        self.reporter(good)
        r = self.run_launcher({"PATH": f"{shared}:{good}"})
        self.assertEqual(self.reported(r)["PATH"], good)
        self.assertEqual(self.ran(), [])

    def test_without_any_trusted_entry_the_action_gets_the_system_directories(self):
        py_dir = os.path.join(self.tmp, "py")
        self.reporter(py_dir)
        r = self.run_launcher({"PATH": ".:relative:" + os.path.join(self.tmp, "missing"),
                               "QUOTARIBBON_PYTHON": os.path.join(py_dir, "python3")})
        self.assertEqual(self.reported(r)["PATH"], "/usr/bin:/bin")

    def test_untrusted_path_entries_are_not_searched_for_python(self):
        shared = os.path.join(self.tmp, "shared")
        self.fake_pythons(shared)
        os.chmod(shared, 0o777)
        r = self.run_launcher({"PATH": shared})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 or newer was not found", r.stderr)
        self.assertEqual(self.ran(), [])

    def test_real_interpreter_through_the_filtered_path_still_runs_the_action(self):
        r = self.run_launcher({"PATH": f".:{REAL_PY_DIR}:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("unknown subcommand", r.stderr)

    def test_platform_probe_is_never_looked_up_in_path(self):
        tools = os.path.join(self.tmp, "tools")
        for name in ("uname", "ls", "chmod", "readlink", "id"):
            self.fake(tools, name)
        r = self.run_launcher({"PATH": f"{tools}:{REAL_PY_DIR}"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.ran(), [])

    # -- #! interpreters of a candidate -------------------------------------------

    def interpreter_reporter(self, directory: str, name: str = "qrpy") -> str:
        """Stands in for a Python found by ``env``: passes the probe (its arguments
        then contain ``-c``), otherwise reports the PATH the action would get."""
        return self.write_exe(directory, name, """#!/bin/sh
for a; do [ "$a" = -c ] && exit 0; done
printf 'PATH=%s\\n' "$PATH"
exit 7
""")

    def env_script(self, directory: str, line: str, name: str = "python3") -> str:
        return self.write_exe(directory, name, line + "\n")

    def shared_dir(self) -> str:
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared, exist_ok=True)
        os.chmod(shared, 0o777)
        return shared

    def test_version_probe_already_runs_with_the_trusted_path(self):
        shared = self.shared_dir()
        self.fake(shared, "qrhelper")
        cand = os.path.join(self.tmp, "cand")
        py = self.write_exe(cand, "python3", """#!/bin/sh
qrhelper 2>/dev/null
for a; do [ "$a" = -c ] && exit 0; done
printf 'PATH=%s\\n' "$PATH"
exit 7
""")
        r = self.run_launcher({"PATH": f"{shared}:{cand}", "QUOTARIBBON_PYTHON": py})
        self.assertEqual(self.reported(r)["PATH"], cand)
        self.assertEqual(self.ran(), [])

    def test_env_script_never_runs_a_python_from_an_untrusted_directory(self):
        shared = self.shared_dir()
        self.fake(shared, "qrpy")
        cand = os.path.join(self.tmp, "cand")
        for name in PYTHON_NAMES:
            self.env_script(cand, "#!/usr/bin/env qrpy", name)
        r = self.run_launcher({"PATH": f"{shared}:{cand}",
                               "QUOTARIBBON_PYTHON": os.path.join(cand, "python3")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        # Found on PATH: skipped, the real interpreter after it is used instead.
        r = self.run_launcher({"PATH": f"{shared}:{cand}:{REAL_PY_DIR}"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.ran(), [])

    def test_env_script_whose_python_is_in_a_trusted_directory_runs(self):
        good = os.path.join(self.tmp, "good")
        self.interpreter_reporter(good)
        cand = os.path.join(self.tmp, "cand")
        py = self.env_script(cand, "#!/usr/bin/env qrpy")
        r = self.run_launcher({"PATH": good, "QUOTARIBBON_PYTHON": py})
        self.assertEqual(self.reported(r)["PATH"], good)
        r = self.run_launcher({"PATH": f"{cand}:{good}"})  # also when found on PATH
        self.assertEqual(self.reported(r)["PATH"], f"{cand}:{good}")

    def test_env_script_whose_python_is_replaceable_is_refused(self):
        good = os.path.join(self.tmp, "good")
        os.chmod(self.interpreter_reporter(good), 0o777)
        py = self.env_script(os.path.join(self.tmp, "cand"), "#!/usr/bin/env qrpy")
        r = self.run_launcher({"PATH": good, "QUOTARIBBON_PYTHON": py})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)

    def test_untrusted_absolute_interpreter_is_refused_without_running(self):
        shared = self.shared_dir()
        in_shared = self.fake(shared, "qrinterp")
        writable = self.fake(os.path.join(self.tmp, "own"), "qrinterp")
        os.chmod(writable, 0o777)
        cand = os.path.join(self.tmp, "cand")
        for i, interp in enumerate((in_shared, writable)):
            with self.subTest(interp=interp):
                py = self.env_script(cand, "#!" + interp, "python%d" % i)
                r = self.run_launcher({"PATH": REAL_PY_DIR, "QUOTARIBBON_PYTHON": py})
                self.assertEqual(r.returncode, 1)
                self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        self.assertEqual(self.ran(), [])

    def test_trusted_absolute_interpreter_is_accepted(self):
        cand = os.path.join(self.tmp, "cand")
        for i, line in enumerate(("#!/bin/sh", "#! \t/bin/sh -u")):
            with self.subTest(line=line):
                py = self.write_exe(cand, "python%d" % i, line + "\n" + REPORTING_PYTHON)
                r = self.run_launcher({"PATH": cand, "QUOTARIBBON_PYTHON": py})
                self.assertEqual(self.reported(r)["PATH"], cand)

    def test_env_options_relative_and_unparsable_interpreters_are_refused(self):
        good = os.path.join(self.tmp, "good")
        self.interpreter_reporter(good)
        cand = os.path.join(self.tmp, "cand")
        lines = ("#!/usr/bin/env -S qrpy", "#!/usr/bin/env -i qrpy", "#!/usr/bin/env - qrpy",
                 "#!/usr/bin/env X=1 qrpy", "#!/usr/bin/env ./qrpy", "#!/usr/bin/env",
                 f"#!/usr/bin/env {good}/qrpy", "#!/usr/bin/env qrpy\r", "#!/usr/bin/env nosuchpy",
                 "#!qrpy", "#!./qrpy", "#!", "#!/bin/sh\r", "#!/bin/s\0h",
                 "#!/bin/sh " + "x" * 300)
        for i, line in enumerate(lines):
            with self.subTest(line=line):
                py = self.env_script(cand, line, "python%d" % i)
                r = self.run_launcher({"PATH": good, "QUOTARIBBON_PYTHON": py})
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("QUOTARIBBON_PYTHON", r.stderr)

    def test_interpreter_chain_is_bounded(self):
        def chain(length: int) -> str:
            directory = os.path.join(self.tmp, "chain%d" % length)
            prev = self.interpreter_reporter(directory, "s0")
            for i in range(1, length):
                prev = self.write_exe(directory, "s%d" % i, "#!%s\n" % prev)
            return prev

        # Within the bound the candidate is accepted (whether the kernel then runs a
        # script as an interpreter differs between systems; macOS does not).
        r = self.run_launcher({"PATH": REAL_PY_DIR, "QUOTARIBBON_PYTHON": chain(2)})
        self.assertNotIn("QUOTARIBBON_PYTHON", r.stderr)
        self.assertIn(r.returncode, (0, 7), r.stderr)
        r = self.run_launcher({"PATH": REAL_PY_DIR, "QUOTARIBBON_PYTHON": chain(6)})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        loop = os.path.join(self.tmp, "loop", "python3")
        self.write_exe(os.path.dirname(loop), "python3", "#!%s\n" % loop)
        r = self.run_launcher({"PATH": REAL_PY_DIR, "QUOTARIBBON_PYTHON": loop})
        self.assertEqual(r.returncode, 1)

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_unreadable_candidate_is_refused(self):
        py = self.write_exe(os.path.join(self.tmp, "cand"), "python3", REPORTING_PYTHON)
        os.chmod(py, 0o311)
        r = self.run_launcher({"PATH": REAL_PY_DIR, "QUOTARIBBON_PYTHON": py})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)

    # -- macOS ACLs ---------------------------------------------------------------

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_python_with_an_acl_write_grant_is_refused_without_running(self):
        bin_dir = os.path.join(self.tmp, "bin")
        self.fake_pythons(bin_dir)
        for name in PYTHON_NAMES:
            chmod_acl(os.path.join(bin_dir, name), "everyone allow write,delete")
        r = self.run_launcher({"PATH": f"{bin_dir}:{REAL_PY_DIR}"})
        self.assertEqual(r.returncode, 2, r.stderr)  # fell through to the real interpreter
        self.assertEqual(self.ran(), [])
        r = self.run_launcher({"PATH": REAL_PY_DIR,
                               "QUOTARIBBON_PYTHON": os.path.join(bin_dir, "python3")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)
        self.assertEqual(self.ran(), [])

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_path_entry_whose_ancestor_lets_a_group_add_files_is_dropped(self):
        prefix = os.path.join(self.tmp, "prefix")
        bin_dir = os.path.join(prefix, "bin")
        self.fake_pythons(bin_dir)
        chmod_acl(prefix, "group:staff allow add_file,add_subdirectory")
        py_dir = os.path.join(self.tmp, "py")
        self.reporter(py_dir)
        r = self.run_launcher({"PATH": f"{bin_dir}:{py_dir}"})
        self.assertEqual(self.reported(r)["PATH"], py_dir)
        self.assertEqual(self.ran(), [])

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_symlink_to_python_with_its_own_delete_grant_is_refused(self):
        mine = os.path.join(self.tmp, "mine")
        os.makedirs(mine)
        link = os.path.join(mine, "python3")
        os.symlink(self.real_python, link)
        chmod_acl(link, "everyone allow delete", no_follow=True)
        r = self.run_launcher({"PATH": os.path.join(self.tmp, "empty"), "QUOTARIBBON_PYTHON": link})
        self.assertEqual(r.returncode, 1)
        self.assertIn("QUOTARIBBON_PYTHON", r.stderr)

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_deny_only_and_owner_only_entries_are_accepted(self):
        home = os.path.join(self.tmp, "home")
        py_dir = os.path.join(home, "bin")
        py = self.reporter(py_dir)
        chmod_acl(home, "group:everyone deny delete")
        chmod_acl(py_dir, f"user:{ME} allow add_file,delete_child,file_inherit")
        chmod_acl(py, "user:root allow write")
        chmod_acl(py, "user:nobody deny write,delete")
        chmod_acl(py, "group:staff allow read,execute")
        r = self.run_launcher({"PATH": py_dir, "QUOTARIBBON_PYTHON": py})
        self.assertEqual(self.reported(r)["PATH"], py_dir)

    @unittest.skipUnless(ON_MACOS, "ACL entries are created with macOS chmod +a")
    def test_bootstrap_json_with_an_acl_grant_is_ignored(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        bootstrap = os.path.join(cfg, "bootstrap.json")
        with open(bootstrap, "w") as fh:
            fh.write('{"python": "%s"}\n' % self.real_python)
        chmod_acl(bootstrap, "group:staff allow write")
        r = self.run_launcher({"PATH": os.path.join(self.tmp, "empty"), "HERDR_PLUGIN_CONFIG_DIR": cfg})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.11 or newer was not found", r.stderr)


class LauncherAclParserTests(unittest.TestCase):
    """The launcher's ACL parsing, run in /bin/sh with a stand-in for ls(1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.funcs = launcher_function("qr_acl_entry_ok") + launcher_function("qr_acl_ok")

    def tearDown(self):
        self._tmp.cleanup()

    def acl_ok(self, listing: str, *, user: str = ME) -> bool:
        listing_file = os.path.join(self.tmp, "listing")
        with open(listing_file, "w") as fh:
            fh.write(listing)
        fake_ls = os.path.join(self.tmp, "ls")
        with open(fake_ls, "w") as fh:
            fh.write("#!/bin/sh\n/bin/cat '%s'\n" % listing_file)
        os.chmod(fake_ls, 0o755)
        script = ("set -u\nset -f\nqr_nl='\n'\n" + self.funcs +
                  "qr_ls=$1\nqr_user=$2\nqr_has_acl=1\nqr_acl_ok /some/path\n")
        r = subprocess.run(["/bin/sh", "-c", script, "sh", fake_ls, user],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stderr, "")
        return r.returncode == 0

    HEAD = "-rwxr-xr-x+ 1 me  staff  18 Nov  2 12:00 /some/path\n"

    def test_deny_harmless_owner_and_root_entries_are_accepted(self):
        self.assertTrue(self.acl_ok(self.HEAD + " 0: group:everyone deny delete\n"
                                    " 1: group:everyone allow read,execute,readattr\n"
                                    " 2: user:nobody inherited allow list,search,file_inherit\n"
                                    f" 3: user:{ME} allow write,delete,chown\n"
                                    " 10: user:root inherited allow add_file,delete_child\n"))

    def test_modifying_or_unknown_grants_to_others_are_rejected(self):
        for perms in ("write", "append", "writeattr", "writeextattr", "delete", "delete_child",
                      "add_file", "add_subdirectory", "writesecurity", "chown",
                      "read,write", "list,add_file", "something_new"):
            with self.subTest(perms=perms):
                self.assertFalse(self.acl_ok(self.HEAD + f" 0: group:staff allow {perms}\n"))
                self.assertFalse(self.acl_ok(self.HEAD + f" 0: user:other inherited allow {perms}\n"))

    def test_owner_grant_is_not_trusted_when_the_user_is_unknown(self):
        self.assertFalse(self.acl_ok(self.HEAD + f" 0: user:{ME} allow write\n", user=""))

    def test_listing_that_cannot_be_parsed_is_rejected(self):
        for line in ("0 group:staff allow read", " x: group:staff allow read",
                     " 0: group:staff permit read", " 0: group:staff allow",
                     " 0: group:staff allow read,,execute", " 0: group:staff allow read extra",
                     " 0: group:staff inherited", " 0:", "", " 0: two words allow read"):
            with self.subTest(line=line):
                self.assertFalse(self.acl_ok(self.HEAD + line + "\n"))

    def test_acl_marker_without_listed_entries_is_rejected(self):
        self.assertFalse(self.acl_ok(self.HEAD))

    def test_failing_ls_is_rejected(self):
        script = ("set -u\nset -f\nqr_nl='\n'\n" + self.funcs +
                  "qr_ls=/nonexistent/ls\nqr_user=me\nqr_has_acl=1\nqr_acl_ok /some/path\n")
        r = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
