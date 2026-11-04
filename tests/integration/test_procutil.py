"""Bounded JSON-lines child sessions, one-shot capture, env scrubbing and group cleanup."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from quotaribbon import procutil
from tests.helpers.fake_herdr import link_executable
from quotaribbon.procutil import (
    JsonlSession,
    ProfileConflict,
    SessionBudgetExceeded,
    SessionExited,
    SessionLimits,
    SessionProtocolError,
    SessionSpawnError,
    SessionTimeout,
    child_env,
    present_vars,
    profile_env,
    run_capture,
)

PY = sys.executable


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ask ps for its state.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def script(code: str) -> list[str]:
    return [PY, "-c", textwrap.dedent(code)]


ECHO = """
import json, sys
print(json.dumps({"type": "system", "subtype": "hello", "secret": "CANARY-PAYLOAD"}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    print(json.dumps({"type": "other"}), flush=True)
    print(json.dumps({"type": "reply", "echo": msg}), flush=True)
"""

GRANDCHILD = """
import os, signal, subprocess, sys, time
if {ignore_term}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
gc = subprocess.Popen([sys.executable, "-c", "import time\\nwhile True: time.sleep(0.2)"])
with open({pidfile!r}, "w") as fh:
    fh.write("%d %d" % (os.getpid(), gc.pid))
while True:
    time.sleep(0.2)
"""


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.env = dict(os.environ)

    def tearDown(self):
        self._tmp.cleanup()

    def limits(self, timeout=10.0, grace=1.0, **kw) -> SessionLimits:
        return SessionLimits(timeout_s=timeout, grace_s=grace, **kw)


class ChildEnvTests(unittest.TestCase):
    def test_removes_herdr_pane_and_plugin_identity_but_keeps_auth_vars(self):
        base = {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_TAB_ID": "w1:t1",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_PLUGIN_ID": "x",
            "HERDR_PLUGIN_STATE_DIR": "/s",
            "HERDR_SOCKET_PATH": "/sock",
            "ANTHROPIC_API_KEY": "sk-keep",
            "OPENAI_API_KEY": "sk-keep2",
            "PATH": "/usr/bin",
        }
        out = child_env(base, extra={"CODEX_HOME": "/p"})
        self.assertEqual(
            out,
            {"HERDR_SOCKET_PATH": "/sock", "ANTHROPIC_API_KEY": "sk-keep", "OPENAI_API_KEY": "sk-keep2", "PATH": "/usr/bin", "CODEX_HOME": "/p"},
        )
        self.assertIn("HERDR_ENV", base)  # input untouched

    def test_present_vars_reports_names_of_non_empty_values(self):
        self.assertEqual(present_vars({"A": "1", "B": "", "C": "x"}, ("A", "B", "C", "D")), ("A", "C"))

    def test_profile_env_sets_passes_through_or_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = os.path.join(tmp, "prof")
            os.mkdir(prof)
            self.assertEqual(profile_env({}, "CODEX_HOME", prof, cwd=tmp), {"CODEX_HOME": prof})
            self.assertEqual(profile_env({"CODEX_HOME": ""}, "CODEX_HOME", prof, cwd=tmp), {"CODEX_HOME": prof})
            self.assertEqual(profile_env({}, "CODEX_HOME", prof, cwd=tmp, set_when_unset=False), {})
            # Same directory spelled differently: the user's literal value is kept.
            self.assertEqual(profile_env({"CODEX_HOME": prof + "/"}, "CODEX_HOME", prof, cwd=tmp), {})
            self.assertEqual(profile_env({"CODEX_HOME": "prof"}, "CODEX_HOME", prof, cwd=tmp), {})
            with self.assertRaises(ProfileConflict):
                profile_env({"CODEX_HOME": os.path.join(tmp, "other")}, "CODEX_HOME", prof, cwd=tmp)


class SessionTests(Tmp):
    def test_send_and_read_until_skips_other_messages(self):
        with JsonlSession(script(ECHO), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            s.send({"q": 1})
            got = s.read_until(lambda m: m.get("type") == "reply", time.monotonic() + 10)
            self.assertEqual(got["echo"], {"q": 1})
        self.assertEqual(s.returncode, 0)

    def test_predicate_exceptions_propagate_and_child_is_reaped(self):
        class Stop(Exception):
            pass

        def pred(m):
            raise Stop

        s = JsonlSession(script(ECHO), cwd=self.tmp, env=self.env, limits=self.limits())
        with self.assertRaises(Stop):
            with s:
                s.read_until(pred, time.monotonic() + 10)
        self.assertFalse(pid_alive(s.pid))

    def test_timeout_kills_child_and_grandchild(self):
        pidfile = os.path.join(self.tmp, "pids")
        s = JsonlSession(script(GRANDCHILD.format(ignore_term=False, pidfile=pidfile)), cwd=self.tmp, env=self.env, limits=self.limits(grace=0.3))
        while not os.path.exists(pidfile) or not read_text(pidfile).strip():
            time.sleep(0.01)
        with self.assertRaises(SessionTimeout):
            with s:
                s.read_until(lambda m: True, time.monotonic() + 0.2)
        child, grandchild = map(int, read_text(pidfile).split())
        self.assertTrue(wait_gone(child))
        self.assertTrue(wait_gone(grandchild), "grandchild orphaned")
        self.assertEqual(procutil.live_session_count(), 0)

    def test_sigterm_ignoring_child_is_sigkilled_after_grace(self):
        pidfile = os.path.join(self.tmp, "pids")
        s = JsonlSession(script(GRANDCHILD.format(ignore_term=True, pidfile=pidfile)), cwd=self.tmp, env=self.env, limits=self.limits(grace=0.3))
        while not os.path.exists(pidfile) or not read_text(pidfile).strip():
            time.sleep(0.02)
        t0 = time.monotonic()
        rc = s.close(terminate=True)
        self.assertEqual(rc, -signal.SIGKILL)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        child, grandchild = map(int, read_text(pidfile).split())
        self.assertTrue(wait_gone(child))
        self.assertTrue(wait_gone(grandchild))

    def test_normal_close_waits_for_child_to_exit_on_stdin_eof(self):
        code = "import sys, time\nsys.stdin.read()\ntime.sleep(0.3)\nsys.exit(7)\n"
        s = JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits(grace=5))
        self.assertEqual(s.close(), 7)

    def test_stdout_budget_exceeded_by_a_huge_line(self):
        code = "import sys\nsys.stdout.write('{\"a\":\"' + 'x' * (2 << 20) + '\"}\\n')\nsys.stdout.flush()\nsys.stdin.read()\n"
        with self.assertRaises(SessionBudgetExceeded):
            with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                s.read_until(lambda m: True, time.monotonic() + 10)

    def test_malformed_lines_are_protocol_errors_without_payload(self):
        bad_lines = [
            '{"type": "x", "v": ',
            '{"v": NaN}',
            '{"v": Infinity}',
            '{"text": "\x1b[31mred"}',
            '[1, 2, 3]',
            '"just a string"',
            '{"a": 1, "a": 2}',
        ]
        for line in bad_lines:
            code = f"import sys\nsys.stdout.write({line + chr(10)!r})\nsys.stdout.flush()\nsys.stdin.read()\n"
            with self.assertRaises(SessionProtocolError) as cm:
                with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                    s.read_until(lambda m: True, time.monotonic() + 10)
            self.assertNotIn("red", str(cm.exception))
            self.assertNotIn("NaN", str(cm.exception))

    def test_invalid_utf8_is_protocol_error(self):
        code = "import sys\nsys.stdout.buffer.write(b'{\"a\": \"\\xff\"}\\n')\nsys.stdout.flush()\nsys.stdin.read()\n"
        with self.assertRaises(SessionProtocolError):
            with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                s.read_until(lambda m: True, time.monotonic() + 10)

    def test_escaped_control_characters_are_valid_data(self):
        code = "import json\nprint(json.dumps({'type': 'note', 'text': '\\x1b[31m\\nx'}), flush=True)\n"
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            got = s.read_until(lambda m: True, time.monotonic() + 10)
        self.assertEqual(got["text"], "\x1b[31m\nx")

    def test_last_line_without_newline_is_parsed(self):
        code = "import sys\nsys.stdout.write('{\"type\": \"tail\"}')\n"
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            self.assertEqual(s.read_until(lambda m: True, time.monotonic() + 10), {"type": "tail"})

    def test_child_exit_before_answer_is_session_exited(self):
        with self.assertRaises(SessionExited):
            with JsonlSession(script("print('{\"type\": \"bye\"}')"), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                s.read_until(lambda m: m.get("type") == "never", time.monotonic() + 10)

    def test_send_after_child_closed_stdin_raises_session_exited(self):
        code = "import os, time\nos.close(0)\ntime.sleep(5)\n"
        s = JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits(grace=0.2))
        try:
            time.sleep(0.2)
            with self.assertRaises(SessionExited):
                for _ in range(100):
                    s.send({"x": "y" * 1024})
        finally:
            s.close(terminate=True)

    def test_stderr_within_budget_is_drained_and_discarded(self):
        code = "import sys\nsys.stderr.write('E' * (32 << 10))\nsys.stderr.flush()\nprint('{\"type\": \"ok\"}', flush=True)\n"
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits(stderr_budget=64 << 10)) as s:
            self.assertEqual(s.read_until(lambda m: True, time.monotonic() + 10), {"type": "ok"})
        self.assertEqual(s._stderr_bytes, 32 << 10)

    def test_spawn_failure(self):
        with self.assertRaises(SessionSpawnError) as cm:
            JsonlSession([os.path.join(self.tmp, "missing")], cwd=self.tmp, env=self.env, limits=self.limits())
        self.assertNotIn(self.tmp, str(cm.exception))


class SendDeadlineTests(Tmp):
    def test_send_to_a_child_that_never_reads_times_out_instead_of_blocking(self):
        s = JsonlSession(script("import time\ntime.sleep(30)\n"), cwd=self.tmp, env=self.env, limits=self.limits(grace=0.2))
        try:
            t0 = time.monotonic()
            with self.assertRaises(SessionTimeout):
                s.send({"blob": "x" * (4 << 20)}, deadline=time.monotonic() + 0.25)
            self.assertLess(time.monotonic() - t0, 1.5)
        finally:
            s.close(terminate=True)
        self.assertFalse(pid_alive(s.pid))
        self.assertEqual(procutil.live_session_count(), 0)

    def test_default_send_deadline_is_the_session_timeout(self):
        s = JsonlSession(script("import time\ntime.sleep(30)\n"), cwd=self.tmp, env=self.env, limits=self.limits(timeout=0.25, grace=0.1))
        try:
            t0 = time.monotonic()
            with self.assertRaises(SessionTimeout):
                s.send({"blob": "x" * (4 << 20)})
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            s.close(terminate=True)

    def test_output_is_drained_while_a_large_write_waits(self):
        # The child fills both output pipes before it reads stdin: without draining
        # while writing, both sides would block forever.
        code = """
import json, sys
sys.stderr.write("E" * (256 << 10)); sys.stderr.flush()
for i in range(200):
    print(json.dumps({"type": "progress", "pad": "p" * 1000}), flush=True)
line = sys.stdin.readline()
print(json.dumps({"type": "done", "n": len(line)}), flush=True)
"""
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits(stderr_budget=1 << 20)) as s:
            s.send({"blob": "x" * (2 << 20)}, deadline=time.monotonic() + 10)
            got = s.read_until(lambda m: m.get("type") == "done", time.monotonic() + 10)
        self.assertGreater(got["n"], 2 << 20)
        self.assertGreaterEqual(s._stderr_bytes, 256 << 10)

    def test_send_refuses_to_write_after_invalid_output(self):
        code = "import sys, time\nsys.stdout.write('not json\\n'); sys.stdout.flush()\ntime.sleep(5)\n"
        s = JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits(grace=0.2))
        try:
            with self.assertRaises(SessionProtocolError):
                s.read_until(lambda m: False, time.monotonic() + 4)
            with self.assertRaises(SessionProtocolError):
                s.send({"q": 1})
        finally:
            s.close(terminate=True)


class PoisonAndTrailingOutputTests(Tmp):
    def test_poisoned_stream_wins_over_a_matching_message_queued_before_it(self):
        code = "import sys\nsys.stdout.write('{\"type\": \"answer\"}\\n{\"broken\": \\n')\nsys.stdout.flush()\nsys.stdin.read()\n"
        with self.assertRaises(SessionProtocolError):
            with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                time.sleep(0.15)  # both lines arrive in one read
                s.read_until(lambda m: m.get("type") == "answer", time.monotonic() + 10)

    def test_finish_reports_malformed_output_that_arrives_at_shutdown(self):
        code = "import sys\nprint('{\"type\": \"answer\"}', flush=True)\nsys.stdin.read()\nsys.stdout.write('garbage\\n')\n"
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            s.read_until(lambda m: m.get("type") == "answer", time.monotonic() + 10)
            with self.assertRaises(SessionProtocolError):
                s.finish(lambda m: None)
        self.assertFalse(pid_alive(s.pid))
        self.assertEqual(procutil.live_session_count(), 0)

    def test_finish_passes_every_unread_message_to_the_check_in_order(self):
        code = (
            "import json, sys\n"
            "print(json.dumps({'type': 'answer'}), flush=True)\n"
            "print(json.dumps({'type': 'queued'}), flush=True)\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'after_eof'}), flush=True)\n"
            "sys.stdout.write(json.dumps({'type': 'tail_without_newline'}))\n"
        )
        seen = []
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            s.read_until(lambda m: m.get("type") == "answer", time.monotonic() + 10)
            rc = s.finish(lambda m: seen.append(m["type"]))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["queued", "after_eof", "tail_without_newline"])

    def test_finish_propagates_a_rejecting_check(self):
        class Reject(Exception):
            pass

        def check(m):
            raise Reject

        code = "import sys\nprint('{\"type\": \"answer\"}', flush=True)\nsys.stdin.read()\nprint('{\"type\": \"late\"}', flush=True)\n"
        with self.assertRaises(Reject):
            with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
                s.read_until(lambda m: m.get("type") == "answer", time.monotonic() + 10)
                s.finish(check)
        self.assertEqual(procutil.live_session_count(), 0)

    def test_last_received_at_is_the_wall_clock_arrival_of_the_returned_message(self):
        code = "import sys, time\ntime.sleep(0.2)\nprint('{\"type\": \"answer\"}', flush=True)\nsys.stdin.read()\ntime.sleep(0.3)\n"
        t0 = time.time()
        with JsonlSession(script(code), cwd=self.tmp, env=self.env, limits=self.limits()) as s:
            self.assertIsNone(s.last_received_at)
            s.read_until(lambda m: True, time.monotonic() + 10)
            got = s.last_received_at
            s.finish()
        t1 = time.time()
        self.assertTrue(t0 + 0.15 <= got <= t1 - 0.25, (t0, got, t1))


class SetupFailureTests(Tmp):
    def _spy_popen(self, created):
        real = subprocess.Popen

        def spy(*a, **kw):
            p = real(*a, **kw)
            created.append(p)
            return p

        return mock.patch.object(procutil.subprocess, "Popen", side_effect=spy)

    def test_session_setup_failure_after_spawn_kills_reaps_and_unregisters_the_child(self):
        created = []
        with self._spy_popen(created), mock.patch.object(procutil.selectors, "DefaultSelector", side_effect=OSError("boom")):
            with self.assertRaises(SessionSpawnError) as cm:
                JsonlSession(script("import time\ntime.sleep(30)\n"), cwd=self.tmp, env=self.env, limits=self.limits())
        self.assertNotIn("boom", str(cm.exception))
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll(), "child left running")
        self.assertFalse(pid_alive(created[0].pid))
        self.assertEqual(procutil.live_session_count(), 0)

    def test_capture_setup_failure_after_spawn_kills_reaps_and_unregisters_the_child(self):
        created = []
        with self._spy_popen(created), mock.patch.object(procutil.selectors, "DefaultSelector", side_effect=OSError("boom")):
            r = run_capture(script("import time\ntime.sleep(30)\n"), cwd=self.tmp, env=self.env, timeout_s=10)
        self.assertEqual(r.spawn_error, "OSError")
        self.assertFalse(r.ok)
        self.assertIsNotNone(created[0].poll())
        self.assertEqual(procutil.live_session_count(), 0)

    def test_capture_children_are_registered_while_running(self):
        seen = []
        code = "import time\ntime.sleep(0.3)\n"
        t = threading.Thread(target=lambda: run_capture(script(code), cwd=self.tmp, env=self.env, timeout_s=10))
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and not seen:
            if procutil.live_session_count():
                seen.append(1)
            time.sleep(0.02)
        t.join()
        self.assertEqual(seen, [1])
        self.assertEqual(procutil.live_session_count(), 0)


class LockInheritanceTests(Tmp):
    def test_an_unregistered_pass_fd_outlives_a_sigkilled_parent_until_the_child_exits(self):
        lock_path = os.path.join(self.tmp, "provider.lock")
        pidfile = os.path.join(self.tmp, "child.pid")
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        parent = textwrap.dedent(
            f"""
            import fcntl, os, sys, time
            sys.path.insert(0, {repo!r})
            from quotaribbon.procutil import JsonlSession, SessionLimits
            fd = os.open({lock_path!r}, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = "import os, sys, time\\nopen({pidfile!r}, 'w').write(str(os.getpid()))\\nsys.stdin.read()\\ntime.sleep(0.3)\\n"
            s = JsonlSession([sys.executable, "-c", child], cwd={self.tmp!r}, env=dict(os.environ),
                             limits=SessionLimits(timeout_s=30), pass_fds=(fd,))
            print("ready", flush=True)
            time.sleep(60)
            """
        )
        p = subprocess.Popen([PY, "-c", parent], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")
            while not os.path.exists(pidfile) or not read_text(pidfile).strip():
                time.sleep(0.02)
            child_pid = int(read_text(pidfile))
            p.kill()  # SIGKILL: no cleanup code runs in the parent
            p.wait()
            fd = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the orphaned child still holds it
                self.assertTrue(wait_gone(child_pid), "child must exit on stdin EOF")
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.05)
            finally:
                os.close(fd)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()


class RunCaptureTests(Tmp):
    def test_captures_stdout_and_exit_code_discarding_stderr(self):
        r = run_capture(script("import sys\nprint('out')\nsys.stderr.write('err')\nsys.exit(3)"), cwd=self.tmp, env=self.env, timeout_s=10)
        self.assertEqual((r.returncode, r.stdout, r.stderr_bytes, r.ok), (3, b"out\n", 3, True))

    def test_timeout_kills_the_whole_group(self):
        pidfile = os.path.join(self.tmp, "pids")
        t0 = time.monotonic()
        r = run_capture(script(GRANDCHILD.format(ignore_term=False, pidfile=pidfile)), cwd=self.tmp, env=self.env, timeout_s=0.5, grace_s=0.2)
        self.assertTrue(r.timed_out)
        self.assertLess(time.monotonic() - t0, 5)
        child, grandchild = map(int, read_text(pidfile).split())
        self.assertTrue(wait_gone(child))
        self.assertTrue(wait_gone(grandchild))

    def test_overflow_is_reported_and_output_dropped(self):
        r = run_capture(script("import sys\nsys.stdout.write('x' * 100000)"), cwd=self.tmp, env=self.env, timeout_s=10, stdout_budget=1000)
        self.assertTrue(r.overflow)
        self.assertEqual(r.stdout, b"")

    def test_spawn_error_is_returned_not_raised(self):
        r = run_capture([os.path.join(self.tmp, "nope")], cwd=self.tmp, env=self.env, timeout_s=1)
        self.assertEqual(r.spawn_error, "FileNotFoundError")
        self.assertFalse(r.ok)


class VersionTests(Tmp):
    def test_parse_version_output(self):
        self.assertEqual(procutil.parse_version_output(b"2.1.316 (Claude Code)\n"), "2.1.316")
        self.assertEqual(procutil.parse_version_output(b"codex-cli 0.166.0\n"), "0.166.0")
        self.assertEqual(procutil.parse_version_output(b"tool 1.2.3-beta.1\n"), "1.2.3-beta.1")
        self.assertIsNone(procutil.parse_version_output(b"no version here"))

    def test_cli_version_is_cached_until_the_binary_changes(self):
        procutil.clear_version_cache()
        exe = os.path.join(self.tmp, "tool")
        counter = os.path.join(self.tmp, "count")

        def write(version: str) -> None:
            with open(exe, "w") as fh:
                fh.write(f"#!/bin/sh\necho x >> '{counter}'\necho 'tool {version}'\n")
            os.chmod(exe, 0o755)

        write("1.0.0")
        self.assertEqual(procutil.cli_version(exe, cwd=self.tmp, env=self.env), "1.0.0")
        self.assertEqual(procutil.cli_version(exe, cwd=self.tmp, env=self.env), "1.0.0")
        self.assertEqual(len(read_text(counter).split()), 1)
        time.sleep(0.01)
        write("1.0.10")
        self.assertEqual(procutil.cli_version(exe, cwd=self.tmp, env=self.env), "1.0.10")
        self.assertIsNone(procutil.cli_version(os.path.join(self.tmp, "missing"), cwd=self.tmp, env=self.env))

    def test_version_probe_inherits_the_given_fds(self):
        procutil.clear_version_cache()
        lock_fd = os.open(os.path.join(self.tmp, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        content = (f"#!{os.path.realpath(PY)} -S\nimport os\ntry:\n    os.fstat(int(os.environ['QR_CHECK_FD']))\n"
                   "    print('tool 1.0.0')\nexcept OSError:\n    print('tool 9.9.9')\n")
        exe = link_executable(self.tmp, "tool", content)
        env = dict(self.env, QR_CHECK_FD=str(lock_fd))
        try:
            self.assertEqual(procutil.cli_version(exe, cwd=self.tmp, env=env, pass_fds=(lock_fd,)), "1.0.0")
            procutil.clear_version_cache()
            self.assertEqual(procutil.cli_version(exe, cwd=self.tmp, env=env), "9.9.9")
        finally:
            os.close(lock_fd)
            procutil.clear_version_cache()


if __name__ == "__main__":
    unittest.main()


class CancellationTests(Tmp):
    """Cancellation and child registration are synchronized; shutdown leaves nothing."""

    def tearDown(self):
        procutil.reset_cancellation()
        super().tearDown()

    def pidfile_session_argv(self, pidfile: str, ignore_term: bool = False) -> list[str]:
        trap = "signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else ""
        code = f"import os, signal, time; {trap}open({pidfile!r}, 'w').write(str(os.getpid())); time.sleep(60)"
        return [PY, "-c", code]

    def test_a_session_spawned_after_cancellation_is_killed_at_registration(self):
        procutil.cancel_all()
        pidfile = os.path.join(self.tmp, "pid")
        with self.assertRaises(procutil.SessionAdmissionRevoked):
            JsonlSession([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp, env=self.env, limits=self.limits())
        self.assertEqual(procutil.live_session_count(), 0)
        self.assertFalse(os.path.exists(pidfile))

    def test_a_capture_spawned_after_cancellation_is_killed_and_reported_revoked(self):
        procutil.cancel_all()
        t0 = time.monotonic()
        res = run_capture([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp, env=self.env, timeout_s=30,
                          grace_s=1.0)
        self.assertEqual(res.spawn_error, procutil.ADMISSION_REVOKED)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(procutil.live_session_count(), 0)

    def test_shutdown_escalates_to_kill_and_includes_late_registrations(self):
        first = os.path.join(self.tmp, "first")
        stubborn = JsonlSession(self.pidfile_session_argv(first, ignore_term=True), cwd=self.tmp, env=self.env,
                                limits=self.limits(grace=0.2))
        deadline = time.monotonic() + 10
        while not os.path.exists(first) and time.monotonic() < deadline:
            time.sleep(0.02)
        late = {}

        def spawn_late():
            time.sleep(0.05)
            try:
                late["s"] = JsonlSession([PY, "-c", "import time; time.sleep(60)"], cwd=self.tmp, env=self.env,
                                         limits=self.limits())
            except procutil.SessionAdmissionRevoked:
                late["revoked"] = True

        th = threading.Thread(target=spawn_late)
        th.start()
        t0 = time.monotonic()
        self.assertTrue(procutil.shutdown_all(0.3, 10))
        th.join(10)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertTrue(late.get("revoked") or late.get("s") is not None)
        stubborn.close()  # owner reaps; cancelled sessions skip the polite wait
        if late.get("s") is not None:
            late["s"].close()
        with open(first) as fh:
            self.assertFalse(pid_alive(int(fh.read())))
        self.assertEqual(procutil.live_session_count(), 0)
