"""The provider lock stays in the refresh process, and no process it did not spawn is signalled.

A refresh killed mid-run leaves its CLI child behind. The child exits on its own when its
stdin/stdout pipes close, and the next refresh proceeds without signalling anything it reads
from disk or finds by scanning processes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from quotaribbon import cache, procutil, scheduler
from quotaribbon.binding import make_binding
from quotaribbon.procutil import JsonlSession, SessionLimits, run_capture
from tests.integration import test_refresh
from tests.helpers.core_fakes import NO_EVIDENCE, T0

PY = sys.executable
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURE_BIN = os.path.join(REPO, "tests", "fixtures", "bin")
FAKE = {"claude": os.path.join(FIXTURE_BIN, "fake_claude.py"), "codex": os.path.join(FIXTURE_BIN, "fake_codex.py")}
# Session argv and a scenario that never answers and exits only when stdin closes.
SESSION = {
    "claude": (["-p", "--output-format", "stream-json", "--input-format", "stream-json"], "hold_until_eof"),
    "codex": (["app-server"], "slow"),
}

SLEEPER = "import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def wait_file(path: str, timeout: float = 10.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            with open(path) as fh:
                text = fh.read().strip()
            if text:
                return text
        time.sleep(0.02)
    raise AssertionError(f"{path} never written")


def session_pid(log: str, timeout: float = 10.0) -> int:
    """Pid of the first session the fake CLI logged."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(log):
            with open(log) as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec.get("mode") == "invoke" and rec.get("kind") in ("session", "app_server"):
                        return int(rec["pid"])
        time.sleep(0.02)
    raise AssertionError("no session was started")


def reap(p: subprocess.Popen) -> None:
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    p.wait()


def lock_free(path: str) -> bool:
    fd = cache.lock_fd(path, blocking=False)
    if fd is None:
        return False
    os.close(fd)
    return True


KILLED_REFRESH = """
import os, sys, time
sys.path.insert(0, {repo!r})
from quotaribbon import cache, procutil
from quotaribbon.procutil import JsonlSession, SessionLimits
fd = cache.lock_fd({lock!r}, blocking=False)
assert fd is not None
procutil.register_lock_owner(fd)
env = dict(os.environ, QR_FAKE_SCENARIO={scenario!r}, QR_FAKE_LOG={log!r})
s = JsonlSession([sys.executable, {fake!r}, *{args!r}], cwd={cwd!r}, env=env,
                 limits=SessionLimits(timeout_s=30), pass_fds=(fd,))
s.send({{"id": 1, "method": "ping"}})
print("ready", flush=True)
time.sleep(60)
"""


def killed_refresh(test: unittest.TestCase, tmp: str, provider: str, lock: str) -> int:
    """Run a refresh-like holder of ``lock`` with a live fake CLI session, SIGKILL it,
    and return the session child's pid (still running at that moment)."""
    log = os.path.join(tmp, f"{provider}-{os.urandom(3).hex()}.jsonl")
    args, scenario = SESSION[provider]
    script = KILLED_REFRESH.format(repo=REPO, lock=lock, scenario=scenario, log=log, fake=FAKE[provider],
                                   args=args, cwd=tmp)
    p = subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        test.assertEqual(p.stdout.readline().strip(), "ready")
        child = session_pid(log)
        test.addCleanup(_kill_quietly, child)
        test.assertTrue(pid_alive(child))
        p.kill()  # no cleanup runs in the refresh
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
    return child


def _kill_quietly(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.lock = os.path.join(self.tmp, "claude-x.lock")
        self.env = {"PATH": "/usr/bin:/bin"}
        self.groups: list[int] = []
        self.addCleanup(self._kill_groups)

    def _kill_groups(self):
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    def killed_refresh(self, provider: str, lock: str) -> int:
        child = killed_refresh(self, self.tmp, provider, lock)
        self.groups.append(child)
        return child


class LockNotInheritedTests(Tmp):
    def test_a_registered_lock_fd_is_withheld_from_children_and_nothing_is_recorded(self):
        fd = cache.lock_fd(self.lock, blocking=False)
        self.addCleanup(os.close, fd)
        procutil.register_lock_owner(fd)
        self.addCleanup(procutil.unregister_lock_owner, fd)
        probe = f"import os\ntry:\n    os.fstat({fd})\n    print('inherited')\nexcept OSError:\n    print('absent')\n"
        res = run_capture([PY, "-c", probe], cwd=self.tmp, env=self.env, timeout_s=10, pass_fds=(fd,))
        self.assertEqual(res.stdout.strip(), b"absent")
        self.assertEqual(sorted(os.listdir(self.tmp)), [os.path.basename(self.lock)])

    def test_an_unregistered_fd_is_still_inherited(self):
        fd = os.open(os.path.join(self.tmp, "other"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        probe = f"import os\nos.fstat({fd})\nprint('inherited')\n"
        res = run_capture([PY, "-c", probe], cwd=self.tmp, env=self.env, timeout_s=10, pass_fds=(fd,))
        self.assertEqual(res.stdout.strip(), b"inherited")

    def test_a_detached_descendant_never_holds_the_lock(self):
        pidfile = os.path.join(self.tmp, "escaped.pid")
        escaper = textwrap.dedent(f"""
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                for f in (0, 1, 2):
                    os.close(f)
                open({pidfile!r}, "w").write(str(os.getpid()))
                time.sleep(60)
                os._exit(0)
            print("1.2.3")
        """)
        fd = cache.lock_fd(self.lock, blocking=False)
        procutil.register_lock_owner(fd)
        try:
            res = run_capture([PY, "-c", escaper], cwd=self.tmp, env=self.env, timeout_s=10, pass_fds=(fd,))
        finally:
            procutil.unregister_lock_owner(fd)
            os.close(fd)
        escaped = int(wait_file(pidfile))
        self.groups.append(escaped)
        self.assertTrue(pid_alive(escaped))
        self.assertEqual((res.returncode, res.timed_out), (0, False))
        self.assertTrue(lock_free(self.lock))

    def test_no_reclaim_entry_points_remain(self):
        for name in ("reclaim_stale_groups", "reclaim_orphaned_lock_holder", "forget_finished_groups"):
            self.assertFalse(hasattr(procutil, name), name)


class KilledRefreshTests(Tmp):
    def test_the_cli_child_of_a_sigkilled_refresh_exits_when_its_pipes_close(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                lock = os.path.join(self.tmp, f"{provider}.lock")
                child = self.killed_refresh(provider, lock)
                self.assertTrue(lock_free(lock), "the lock must not outlive the refresh")
                self.assertTrue(wait_gone(child, 5.0), "the fake CLI must exit on stdin EOF")


class _ProviderLock:
    def provider_lock(self, provider: str) -> str:
        pk = make_binding(provider, self.host.profiles[provider], self.host.config_path).profile_key
        return self.paths.provider_lock(provider, pk)


class BusyBackoffTests(_ProviderLock, test_refresh.RefreshTestCase):
    def test_a_busy_provider_leaves_the_dispatch_lease_to_expire(self):
        self.host.evidence_results["codex"] = NO_EVIDENCE
        self.refresh()
        fd = cache.lock_fd(self.provider_lock("claude"), blocking=False)
        try:
            self.clock.advance(1000)
            lease = scheduler.acquire_dispatch_at(self.paths.state_dir, self.clock.now)
            rep = self.refresh(lease_id=lease)
        finally:
            os.close(fd)
        self.assertEqual(self.action(rep, "claude"), "skipped_busy")
        disp = cache.read_dispatch_at(self.paths.state_dir)
        self.assertEqual((disp["id"], disp["finished_at"]), (lease, None))
        self.assertFalse(scheduler.lease_free_dict(disp, self.clock.now + 5))
        self.assertTrue(scheduler.lease_free_dict(disp, self.clock.now + 1000))

    def test_a_run_without_busy_providers_still_finishes_its_lease(self):
        lease = scheduler.acquire_dispatch_at(self.paths.state_dir, T0)
        self.refresh(lease_id=lease)
        self.assertIsNotNone(cache.read_dispatch_at(self.paths.state_dir)["finished_at"])


class NoForeignSignalTests(_ProviderLock, test_refresh.RefreshTestCase):
    """Across a refresh that follows a SIGKILLed one, only this run's own children are signalled."""

    def test_a_refresh_after_a_sigkilled_one_signals_only_its_own_children(self):
        self.host.evidence_results["codex"] = NO_EVIDENCE
        lock = self.provider_lock("claude")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        leftover = killed_refresh(self, tmp.name, "claude", lock)

        # Records an older build would have left: none of these may ever be signalled.
        bystander = subprocess.Popen([PY, "-c", SLEEPER], start_new_session=True)
        self.addCleanup(reap, bystander)
        now = time.time()
        for pgid in (leftover, bystander.pid):
            cache.write_json_atomic(lock + ".owner", {
                "pgid": pgid, "started_at": now, "parent": 999999,
                "groups": [{"pgid": leftover, "started_at": now}, {"pgid": bystander.pid, "started_at": now}],
            })

        spawned: list[int] = []
        real_popen = subprocess.Popen

        class RecordingPopen(real_popen):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                spawned.append(self.pid)

        signalled: list[tuple[str, int, int]] = []
        real_killpg, real_kill = os.killpg, os.kill

        def killpg(pgid, sig):
            signalled.append(("killpg", pgid, sig))
            return real_killpg(pgid, sig)

        def kill(pid, sig):
            signalled.append(("kill", pid, sig))
            return real_kill(pid, sig)

        def own_session():
            # A CLI that ignores EOF and SIGTERM, so this run must signal its own group.
            env = dict(os.environ, QR_FAKE_SCENARIO="slow_ignore_term")
            s = JsonlSession([PY, FAKE["claude"], *SESSION["claude"][0]], cwd=tmp.name, env=env,
                             limits=SessionLimits(timeout_s=10, grace_s=0.2))
            s.read_until(lambda obj: True, time.monotonic() + 10)  # SIGTERM is ignored from here on
            s.close(terminate=True)

        self.host.during_collect["claude"] = own_session
        with mock.patch.object(subprocess, "Popen", RecordingPopen), \
                mock.patch.object(os, "killpg", killpg), mock.patch.object(os, "kill", kill):
            rep = self.refresh()
        self.assertEqual(self.action(rep, "claude"), "published", rep.summary())
        self.assertTrue(spawned)
        self.assertTrue(any(sig == signal.SIGKILL for _, _, sig in signalled), signalled)
        foreign = [s for s in signalled if s[1] not in spawned]
        self.assertEqual(foreign, [])
        self.assertNotIn(bystander.pid, spawned)
        self.assertIsNone(bystander.poll())
        # The leftover of the killed refresh went away on its own (stdin/stdout EOF).
        self.assertTrue(wait_gone(leftover, 5.0))


class BusyBackoffPolicyTests(unittest.TestCase):
    def test_only_a_busy_outcome_holds_the_lease(self):
        from quotaribbon.refresh import ProviderOutcome

        self.assertTrue(scheduler.busy_backoff([ProviderOutcome("claude", "skipped_busy"),
                                                ProviderOutcome("codex", "published")]))
        self.assertFalse(scheduler.busy_backoff([ProviderOutcome("claude", "not_due")]))
        self.assertFalse(scheduler.busy_backoff([]))
        self.assertFalse(scheduler.busy_backoff(None))


if __name__ == "__main__":
    unittest.main()
