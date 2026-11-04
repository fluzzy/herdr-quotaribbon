"""Entry ownership, config backups, special files, bootstrap checks and CLI error messages."""

from __future__ import annotations

import errno
import importlib.util
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from quotaribbon import cli, connection, fsutil
from quotaribbon import setup_config as sc
from quotaribbon.paths import Paths

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("qr_tabbar_bootstrap_under_test",
                                                  os.path.join(REPO_ROOT, "bootstrap", "tabbar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class EntryOwnershipTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.tabbar = os.path.join(self.tmp, "plugin cfg", "tabbar.py")
        self.wrapper = sc.wrapper_path(self.tabbar)
        self.entry = sc.build_entry("/usr/bin/python3", self.tabbar)
        self.config = os.path.join(self.tmp, "config.toml")

    def user_element(self, command: str) -> dict:
        return {"type": "command", "command": command, "interval_seconds": 5, "timeout_seconds": 2}

    def write_config(self, *elements: dict) -> None:
        with open(self.config, "w") as fh:
            fh.write("[ui]\ntab_bar_right = [" + ", ".join(sc.entry_text(e) for e in elements) + "]\n")

    def mentioning_commands(self) -> list[str]:
        q = sc.shlex.quote
        return [f"wc -c {q(self.wrapper + '.bak')}", f"md5 {q(self.wrapper)}", f"cat {q(self.tabbar)}",
                sc.build_command("", self.tabbar) + "; echo extra", f"/bin/sh {q(self.wrapper)}",
                f"/usr/bin/python3 -I {q(self.tabbar)} --flag"]

    def test_commands_that_only_mention_our_paths_are_not_ours(self):
        for cmd in self.mentioning_commands():
            with self.subTest(cmd=cmd):
                self.assertFalse(sc.is_our_entry(self.user_element(cmd), self.tabbar))
        self.assertTrue(sc.is_our_entry(self.entry, self.tabbar))
        self.assertTrue(sc.is_our_entry(dict(self.entry, interval_seconds=9), self.tabbar))
        legacy = self.user_element(sc.legacy_command("/opt/py/bin/python3", self.tabbar))
        self.assertTrue(sc.is_our_entry(legacy, self.tabbar))

    def test_setup_appends_next_to_a_user_element_that_mentions_our_path(self):
        for cmd in self.mentioning_commands():
            with self.subTest(cmd=cmd):
                self.write_config(self.user_element(cmd))
                plan = sc.plan_setup(self.config, self.entry, tabbar_path=self.tabbar)
                self.assertIsNone(plan.refusal, plan.reason)
                self.assertEqual(plan.shape, "d")

    def test_detach_never_removes_a_user_element_that_mentions_our_path(self):
        for cmd in self.mentioning_commands():
            with self.subTest(cmd=cmd):
                self.write_config(self.user_element(cmd))
                for force in (False, True):
                    plan = sc.plan_detach(self.config, None, tabbar_path=self.tabbar,
                                          expected_entry=self.entry, force=force)
                    self.assertTrue(plan.nothing_to_do, plan.reason)
                    self.assertIsNone(plan.refusal)

    def test_detach_removes_only_our_entry_next_to_such_an_element(self):
        user = self.user_element(f"md5 {sc.shlex.quote(self.wrapper)}")
        self.write_config(user, self.entry)
        plan = sc.plan_detach(self.config, None, tabbar_path=self.tabbar, expected_entry=self.entry)
        self.assertIsNone(plan.refusal, plan.reason)
        doc = sc.tomllib.loads(plan.after_text)
        self.assertEqual(doc["ui"]["tab_bar_right"], [user])


class WrapperScriptTests(unittest.TestCase):
    def test_regular_builtins_are_unset_first_and_brackets_are_not_used(self):
        script = sc.wrapper_script("/usr/bin/python3", "/opt/cfg/tabbar.py")
        lines = script.splitlines()
        code = [ln for ln in lines if not ln.startswith("#")]
        self.assertEqual(code[0], "exec 2>/dev/null </dev/null")
        self.assertEqual(code[1], "unset -f cd command kill wait")
        body = "\n".join(code[2:])
        for name in ("cd", "command", "kill", "wait"):
            self.assertRegex(body, rf"\b{name}\b")
        self.assertNotIn("[", body)
        self.assertNotIn("test ", body)


class BackupTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.backups = os.path.join(self.tmp, "backups")
        self.tabbar = os.path.join(self.tmp, "cfg", "tabbar.py")
        self.entry = sc.build_entry("/usr/bin/python3", self.tabbar)
        self.config = os.path.join(self.tmp, "config.toml")

    def backup_names(self) -> list[str]:
        return sorted(os.listdir(self.backups)) if os.path.isdir(self.backups) else []

    def test_failed_backup_write_leaves_no_file(self):
        def enospc(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(sc.os, "write", enospc):
            with self.assertRaises(OSError):
                sc._backup(self.backups, b"x" * 10)
        self.assertEqual(self.backup_names(), [])

    def test_failed_config_write_removes_its_backup(self):
        with open(self.config, "w") as fh:
            fh.write('theme = "dark"\n')
        plan = sc.plan_setup(self.config, self.entry, tabbar_path=self.tabbar)
        self.assertIsNone(plan.refusal, plan.reason)

        def fail(*a, **k):
            raise OSError(errno.EACCES, "Permission denied", self.config)

        with mock.patch.object(sc, "_write_checked", fail):
            with self.assertRaises(OSError):
                sc.apply_plan(plan, self.backups)
        self.assertEqual(self.backup_names(), [])
        with open(self.config) as fh:
            self.assertEqual(fh.read(), 'theme = "dark"\n')

    def test_only_the_newest_backups_are_kept(self):
        os.makedirs(self.backups)
        old = [f"config.toml.2020010{i}T000000.000000Z" for i in range(1, 9)]
        for name in old + ["unrelated.txt"]:
            with open(os.path.join(self.backups, name), "w") as fh:
                fh.write("old")
        for i in range(2):
            with open(self.config, "w") as fh:
                fh.write(f'theme = "t{i}"\n')
            plan = sc.plan_setup(self.config, self.entry, tabbar_path=self.tabbar)
            result = sc.apply_plan(plan, self.backups)
            self.assertTrue(result.written)
            self.assertTrue(os.path.exists(result.backup_path))
            dp = sc.plan_detach(self.config, None, tabbar_path=self.tabbar, expected_entry=self.entry)
            sc.apply_detach(dp, self.backups)
        names = self.backup_names()
        backups = [n for n in names if n.startswith("config.toml.")]
        self.assertEqual(len(backups), sc.MAX_BACKUPS)
        self.assertIn("unrelated.txt", names)
        self.assertNotIn(old[0], names)
        self.assertEqual(len([n for n in backups if n.startswith("config.toml.2020")]), 1)

    def test_the_backup_just_taken_is_kept_even_when_older_names_sort_later(self):
        os.makedirs(self.backups)
        future = [f"config.toml.2999010{i}T000000.000000Z" for i in range(1, 8)]
        for name in future:
            with open(os.path.join(self.backups, name), "w") as fh:
                fh.write("x")
        just = sc._backup(self.backups, b"data")
        sc._prune_backups(self.backups, just)
        names = self.backup_names()
        self.assertIn(os.path.basename(just), names)
        self.assertEqual(len(names), sc.MAX_BACKUPS)


class SpecialFileTests(TempDirCase):
    def fifo(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        os.mkfifo(path, 0o600)
        return path

    def test_read_json_on_a_fifo_fails_at_once(self):
        path = self.fifo("state.json")
        t0 = time.monotonic()
        with self.assertRaises(fsutil.CorruptFile):
            fsutil.read_json(path)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(fsutil.special_file(path))

    def test_read_json_of_a_regular_file_and_a_link_to_it_still_works(self):
        path = os.path.join(self.tmp, "a.json")
        fsutil.atomic_write_json(path, {"a": 1})
        link = os.path.join(self.tmp, "link.json")
        os.symlink(path, link)
        self.assertEqual(fsutil.read_json(link), {"a": 1})
        self.assertFalse(fsutil.special_file(link))
        self.assertIsNone(fsutil.read_json(os.path.join(self.tmp, "missing.json")))

    def test_lock_on_a_fifo_raises_instead_of_blocking(self):
        path = self.fifo("x.lock")
        with self.assertRaises(OSError):
            with fsutil.file_lock(path):
                pass
        regular = os.path.join(self.tmp, "ok.lock")
        with fsutil.file_lock(regular, blocking=True):
            fd = os.open(regular, os.O_RDONLY)
            os.close(fd)

    def test_lock_fd_is_left_blocking(self):
        path = os.path.join(self.tmp, "b.lock")
        fd = fsutil.open_regular(path, os.O_RDWR | os.O_CREAT)
        try:
            self.assertFalse(fsutil.fcntl.fcntl(fd, fsutil.fcntl.F_GETFL) & os.O_NONBLOCK)
        finally:
            os.close(fd)


class BootstrapUnitTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.boot = load_bootstrap()

    def make_root(self) -> str:
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "quotaribbon"))
        for name in ("__init__.py", "tabbar_entry.py"):
            with open(os.path.join(root, "quotaribbon", name), "w") as fh:
                fh.write("")
        return root

    def test_sanitize_drops_line_and_paragraph_separators(self):
        self.assertEqual(self.boot._sanitize("a\u2028b\u2029c\x1b[31m"), "abc[31m")
        with open(os.path.join(REPO_ROOT, "bootstrap", "tabbar.py"), "rb") as fh:
            self.assertNotIn(b"\xe2\x80\xa8", fh.read())  # written as escapes, not raw bytes

    def test_trusted_root_rules(self):
        root = self.make_root()
        self.assertEqual(self.boot._trusted_root(root), root)
        link = os.path.join(self.tmp, "link")
        os.symlink(root, link)
        self.assertEqual(self.boot._trusted_root(link), root)
        pkg_file = os.path.join(root, "quotaribbon", "tabbar_entry.py")
        for path, mode in ((root, 0o770), (os.path.join(root, "quotaribbon"), 0o757), (pkg_file, 0o664)):
            with self.subTest(path=path, mode=oct(mode)):
                old = os.stat(path).st_mode & 0o777
                os.chmod(path, mode)
                try:
                    self.assertIsNone(self.boot._trusted_root(root))
                finally:
                    os.chmod(path, old)
        # A shared parent is fine only with the sticky bit.
        os.chmod(self.tmp, 0o777)
        try:
            self.assertIsNone(self.boot._trusted_root(root))
            os.chmod(self.tmp, 0o1777)
            self.assertEqual(self.boot._trusted_root(root), root)
        finally:
            os.chmod(self.tmp, 0o700)
        elsewhere = os.path.join(self.tmp, "elsewhere.py")
        open(elsewhere, "w").close()
        os.symlink(elsewhere, os.path.join(root, "quotaribbon", "evil.py"))
        self.assertIsNone(self.boot._trusted_root(root))

    def test_load_config_and_log_never_block_on_a_fifo(self):
        os.mkfifo(os.path.join(self.tmp, "bootstrap.json"), 0o600)
        t0 = time.monotonic()
        self.assertIsNone(self.boot._load_config(self.tmp))
        os.makedirs(os.path.join(self.tmp, "logs"))
        os.mkfifo(os.path.join(self.tmp, "logs", "bootstrap.log"), 0o600)
        self.boot._log(self.tmp, "reason")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_log_still_appends_to_a_regular_file(self):
        self.boot._log(self.tmp, "first_reason", "d1")
        self.boot._log(self.tmp, "first_reason", "d2")  # rate-limited
        self.boot._log(self.tmp, "second_reason")
        with open(os.path.join(self.tmp, "logs", "bootstrap.log")) as fh:
            lines = fh.read().splitlines()
        self.assertEqual([ln.split(" ")[1] for ln in lines], ["first_reason", "second_reason"])

    def test_raw_socket_module_is_used(self):
        self.assertEqual(self.boot.socket.__name__, "_socket")


class HostPathTests(unittest.TestCase):
    def test_launcher_host_path_wins_over_path(self):
        self.assertEqual(connection.host_path({"PATH": "/usr/bin:/bin", "QUOTARIBBON_HOST_PATH": "/a:/b"}), "/a:/b")
        self.assertEqual(connection.host_path({"PATH": "/usr/bin"}), "/usr/bin")
        self.assertEqual(connection.host_path({"PATH": "/usr/bin", "QUOTARIBBON_HOST_PATH": ""}), "")
        self.assertEqual(connection.host_path({}), "")

    def test_child_env_drops_the_saved_path_and_exported_functions(self):
        env = {"PATH": "/usr/bin:/bin", "QUOTARIBBON_HOST_PATH": "/usr/bin", "BASH_FUNC_cd%%": "() { :; }",
               "KEEP": "1", "HOME": "/"}
        child = connection.child_env_with_path(env, caller_cwd="/")
        self.assertEqual(child["PATH"], "/usr/bin")
        self.assertEqual(child["KEEP"], "1")
        self.assertNotIn("QUOTARIBBON_HOST_PATH", child)
        self.assertNotIn("BASH_FUNC_cd%%", child)


class CliMessageTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.env = {"HERDR_PLUGIN_CONFIG_DIR": os.path.join(self.tmp, "cfg"),
                    "HERDR_PLUGIN_STATE_DIR": os.path.join(self.tmp, "state"), "HOME": self.tmp,
                    "PATH": "/usr/bin:/bin"}
        self.paths = Paths(config_dir=self.env["HERDR_PLUGIN_CONFIG_DIR"], state_dir=self.env["HERDR_PLUGIN_STATE_DIR"])

    def run_main(self, argv, stdin_text: str = ""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(cli.sys, "stdin", io.StringIO(stdin_text)):
            rc = cli.main(argv, self.env)
        return rc, out.getvalue(), err.getvalue()

    def test_describe_error(self):
        self.assertEqual(cli.describe_error(PermissionError(errno.EACCES, "x", "/p/settings.json")),
                         "could not access /p/settings.json (EACCES)")
        self.assertEqual(cli.describe_error(OSError(errno.ENOSPC, "x")), "a file operation failed (ENOSPC)")
        self.assertEqual(cli.describe_error(KeyError("secret-value")), "unexpected error (KeyError)")

    def test_os_error_in_an_action_names_the_path_and_errno(self):
        def boom(*a, **k):
            raise PermissionError(errno.EPERM, "Operation not permitted", "/locked/control.json")

        with mock.patch.object(Paths, "ensure_private_dirs", boom):
            rc, out, err = self.run_main(["claude-off"])
        self.assertEqual(rc, 1)
        self.assertIn("could not access /locked/control.json (EPERM)", err)
        self.assertIn("try again", err)

    def test_ctrl_c_prints_one_line_and_no_traceback(self):
        with mock.patch.object(cli, "cmd_setup_ui", side_effect=KeyboardInterrupt):
            rc, out, err = self.run_main(["setup-ui"])
        self.assertEqual(rc, 130)
        self.assertIn("cancelled; nothing further was changed", out)
        self.assertEqual(err, "")

    def test_toggle_refuses_quickly_when_a_state_file_is_a_fifo(self):
        self.paths.ensure_private_dirs()
        os.mkfifo(self.paths.control, 0o600)
        t0 = time.monotonic()
        rc, out, _ = self.run_main(["claude-off"])
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(rc, 1)
        self.assertIn("control.json is not a regular file; delete it", out)
        rc, out, _ = self.run_main(["refresh"])
        self.assertEqual(rc, 1)
        self.assertIn("is not a regular file", out)

    def test_refused_off_explains_how_to_recover(self):
        os.makedirs(self.env["HERDR_PLUGIN_CONFIG_DIR"])
        with open(self.paths.settings, "w") as fh:
            fh.write("{not json")
        rc, out, _ = self.run_main(["claude-off"])
        self.assertEqual(rc, 1)
        self.assertIn(f"fix or delete {self.paths.settings}", out)
        self.assertIn("run this action again", out)

    def test_setup_popup_reports_an_unexpected_failure_and_waits_for_enter(self):
        def boom(env):
            raise PermissionError(errno.EACCES, "Permission denied", "/cfg/tabbar.py")

        asked = []
        with mock.patch.object(cli, "_setup_ui", boom), \
                mock.patch.object(cli, "_press_enter", lambda: asked.append(True)):
            rc, out, err = self.run_main(["setup-ui"])
        self.assertEqual(rc, 1)
        self.assertIn("setup failed: could not access /cfg/tabbar.py (EACCES)", out)
        self.assertEqual(asked, [True])
        self.assertEqual(err, "")

    def test_apply_setup_os_error_says_whether_the_config_changed(self):
        tabbar = self.paths.tabbar_py
        config = os.path.join(self.tmp, "config.toml")
        with open(config, "w") as fh:
            fh.write('theme = "x"\n')
        entry = sc.build_entry("/usr/bin/python3", tabbar)
        plan = sc.plan_setup(config, entry, tabbar_path=tabbar)
        ctx = mock.Mock(plan=plan, paths=self.paths, cli={})
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, "_install_bootstrap",
                                                     side_effect=OSError(errno.ENOSPC, "full", tabbar)):
            rc = cli._apply_setup(ctx, pin=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"setup failed: could not access {tabbar} (ENOSPC); {config} was not changed", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, "_install_bootstrap"), \
                mock.patch.object(cli, "_write_default_settings", return_value=False), \
                mock.patch.object(cli, "atomic_write_json",
                                  side_effect=OSError(errno.EACCES, "denied", self.paths.setup_record)):
            rc = cli._apply_setup(ctx, pin=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"{config} was updated, but the setup record could not be saved", out.getvalue())
        self.assertIn("Run detach", out.getvalue())

    def test_startup_clears_the_bytecode_cache(self):
        self.paths.ensure_private_dirs()
        cached = os.path.join(self.paths.pycache_dir, "old-root", "x.pyc")
        os.makedirs(os.path.dirname(cached))
        with open(cached, "w") as fh:
            fh.write("x")
        rc, out, _ = self.run_main(["startup"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(cached))
        self.assertEqual([n for n in os.listdir(self.paths.state_dir) if n.startswith("pycache.old-")], [])


if __name__ == "__main__":
    unittest.main()
