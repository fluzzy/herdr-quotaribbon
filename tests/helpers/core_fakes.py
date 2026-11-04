"""Recording fakes of the host adapter and collector protocols, plus record builders."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import time
from typing import Callable

from quotaribbon import binding
from quotaribbon.procutil import admission_section
from quotaribbon import types as t
from quotaribbon.paths import Paths

ACCT_A = "acct:" + "a" * 24
ACCT_B = "acct:" + "b" * 24
T0 = 1_800_000_000.0  # a fixed "now" (2027-01-15)


class Clock:
    """Mutable fake wall clock usable as ``now_fn``."""

    def __init__(self, now: float = T0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_paths(root: str) -> Paths:
    return Paths(config_dir=os.path.join(root, "config"), state_dir=os.path.join(root, "state"))


def temp_paths(testcase) -> Paths:
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    return make_paths(os.path.realpath(tmp.name))


def window(wid: str, duration: int | None, used: float | None, resets_at: str | None = None) -> t.Window:
    return t.Window(id=wid, scope="all", duration_seconds=duration, used_percent=used, raw_used_percent=used, resets_at=resets_at)


CLAUDE_WINDOWS = (window("five_hour", 18000, 32.0), window("seven_day", 604800, 68.0))
CODEX_WINDOWS = (window("primary", 18000, 54.0), window("secondary", 604800, 21.0))
DEFAULT_WINDOWS = {"claude": CLAUDE_WINDOWS, "codex": CODEX_WINDOWS}


def auth(state: str = "subscription", key: str | None = ACCT_A, *, retryable: bool = False, detail: str | None = None) -> t.AuthResult:
    return t.AuthResult(state=state, account_key=key if state == "subscription" else None, config_dir=None,
                        detail=detail, retryable=retryable)


def result(provider: str, outcome: str = "ok", windows=None, *, before: t.AuthResult | None | str = "default",
           after: t.AuthResult | None | str = "default", error_code: str | None = None, retry_not_before: float | None = None,
           cli_version: str | None = "1.0.0", notes=()) -> t.CollectResult:
    if windows is None:
        windows = DEFAULT_WINDOWS[provider] if outcome == "ok" else ()
    if before == "default":
        before = auth()
    if after == "default":
        after = before if outcome == "ok" else None  # a successful collection proves the account twice
    return t.CollectResult(outcome=outcome, windows=tuple(windows), auth_before=before, auth_after=after,
                           source=t.PROVIDER_SOURCES[provider], source_observed_at=None, error_code=error_code,
                           retry_not_before=retry_not_before, cli_version=cli_version, notes=tuple(notes))


def evidence(linked: bool = True, source: str | None = "integration", integration_state: str | None = "current",
             agent: bool = False, correspondence: str = "match", error: str | None = None) -> t.Evidence:
    return t.Evidence(linked=linked, source=source, integration_state=integration_state, agent_detected=agent,
                      profile_correspondence=correspondence, error=error)


NO_EVIDENCE = t.Evidence(linked=False, source=None, integration_state="not_installed", agent_detected=False,
                         profile_correspondence="unknown", error=None)


def _next(value, *args):
    if callable(value):
        return value(*args)
    if isinstance(value, list):
        return value.pop(0) if len(value) > 1 else value[0]
    return value


class FakeCollector:
    """Scripted collector. ``auth`` / ``collect_result`` may be a value, a list consumed in
    order (the last item repeats), or a callable. ``during_collect`` runs mid-collection."""

    def __init__(self, host: "FakeHost", provider: str) -> None:
        self.host = host
        self.provider = provider

    def cli_version(self) -> str | None:
        self.host.record(self.provider, "cli_version")
        return _next(self.host.versions[self.provider])

    def check_auth(self, timeout_s: float, admit: Callable[[], bool]) -> t.AuthResult:
        with admission_section(admit) as ok:  # stands for the guarded process spawn
            if not ok:
                return t.AuthResult(state="error", account_key=None, config_dir=None, detail=t.ADMISSION_REVOKED,
                                    retryable=True)
            self.host.record(self.provider, "check_auth")
        return _next(self.host.auth[self.provider])

    def collect(self, timeout_s: float, admit: Callable[[], bool]) -> t.CollectResult:
        with admission_section(admit) as ok:
            if not ok:
                return result(self.provider, "transient_error", before=None, error_code=t.ADMISSION_REVOKED)
            self.host.record(self.provider, "collect")
            pre = self.host.inside_admission.get(self.provider)
            if pre is not None:
                pre()
        hook = self.host.during_collect.get(self.provider)
        if hook is not None:
            hook()
        if self.host.collect_sleep:
            time.sleep(self.host.collect_sleep)
        if not admit():
            return result(self.provider, "transient_error", before=None, error_code=t.ADMISSION_REVOKED)
        res = _next(self.host.collect_results[self.provider])
        if res.outcome == "ok" and res.usage_received_at is None and self.host.stamp_usage_time:
            clock = self.host.clock or time.time
            res = dataclasses.replace(res, usage_received_at=float(clock()))
        return res


class FakeHost:
    """Host adapter fake: per-provider evidence/executable/auth/collect scripts + call log."""

    def __init__(self, root: str, *, config_path: str | None = None) -> None:
        self.root = root
        self.config_path = config_path or os.path.join(root, "herdr", "config.toml")
        # Stands in for the Herdr socket: only its stat identity matters.
        self.socket_path = os.path.join(root, "herdr.sock")
        if not os.path.exists(self.socket_path):
            self.restart_server()
        self.profiles = {p: os.path.join(root, "profiles", p) for p in t.PROVIDERS}
        self.evidence_results: dict = {p: evidence() for p in t.PROVIDERS}
        self.executables: dict = {p: f"/opt/fake/bin/{p}" for p in t.PROVIDERS}
        self.versions: dict = {p: "1.0.0" for p in t.PROVIDERS}
        self.auth: dict = {p: auth() for p in t.PROVIDERS}
        self.collect_results: dict = {p: result(p) for p in t.PROVIDERS}
        self.during_collect: dict = {}
        self.inside_admission: dict = {}  # runs while the admission section is held
        self.collect_sleep = 0.0
        self.clock = None  # wall clock used to stamp usage_received_at (default time.time)
        self.stamp_usage_time = True
        self.active = True
        self.plugin_check_hook = None  # runs during the (slow) plugin check
        self.calls: list[tuple[str, str]] = []

    def restart_server(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = f"{self.socket_path}.{os.getpid()}.{os.urandom(4).hex()}"
        with open(tmp, "w") as fh:
            fh.write(os.urandom(4).hex())
        os.replace(tmp, self.socket_path)

    def record(self, provider: str, what: str) -> None:
        self.calls.append((provider, what))

    def calls_for(self, provider: str) -> list[str]:
        return [w for p, w in self.calls if p == provider]

    # -- HostAdapter protocol --
    def herdr_config_path(self) -> str:
        return self.config_path

    def evidence(self, provider: str, profile_dir: str, ps: t.ProviderSettings | None = None) -> t.Evidence:
        self.record(provider, "evidence")
        return _next(self.evidence_results[provider])

    def resolve_profile(self, provider: str, ps: t.ProviderSettings) -> str:
        return os.path.realpath(ps.profile_dir or self.profiles[provider])

    def credential_namespace(self, provider: str, ps: t.ProviderSettings) -> str:
        return binding.credential_namespace(provider, ps, self.env())

    def server_identity(self) -> tuple[str, str | None]:
        return binding.server_identity(self.env())

    def resolve_executable(self, provider: str, ps: t.ProviderSettings) -> str | None:
        self.record(provider, "resolve_executable")
        return _next(self.executables[provider])

    def make_collector(self, provider: str, executable: str, profile_dir: str, *, lock_fd: int | None = None,
                       ps: t.ProviderSettings | None = None) -> FakeCollector:
        self.record(provider, "make_collector")
        self.last_lock_fd = lock_fd
        self.last_ps = ps
        return FakeCollector(self, provider)

    def plugin_active(self) -> bool:
        if self.plugin_check_hook is not None:
            self.plugin_check_hook()
        return self.active

    # -- environment matching this host, for tick() --
    def env(self) -> dict:
        return {
            "HOME": self.root,
            "CLAUDE_CONFIG_DIR": self.profiles["claude"],
            "CODEX_HOME": self.profiles["codex"],
            "HERDR_CONFIG_PATH": self.config_path,
            "HERDR_SOCKET_PATH": self.socket_path,
        }


PROCESS_ACTIVITY = {"cli_version", "check_auth", "collect"}
"""FakeCollector calls that stand for a provider process being started."""
