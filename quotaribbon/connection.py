"""``LiveHostAdapter`` for the ``refresh`` action, plus trusted ``PATH``/executable resolution.

Never starts a provider CLI itself, so a provider without Herdr evidence never gets a process.
"""

from __future__ import annotations

import os
import stat
from typing import Callable, Mapping

from . import PLUGIN_ID, _core
from .herdr_bridge import (
    HerdrClient,
    HerdrError,
    HerdrUnavailable,
    find_plugin,
    integration_status_text,
    parse_integration_states,
    plugin_enabled,
)
from .paths import Paths
from .types import PROVIDERS, Evidence, ProviderSettings

FALLBACK_CHILD_PATH = "/usr/bin:/bin"
# Finds nothing; an empty PATH would mean the cwd.
NOWHERE_PATH = "/nonexistent/quotaribbon-no-trusted-path"

EXECUTABLE_NAMES: Mapping[str, str] = {"claude": "claude", "codex": "codex"}

EVIDENCE_SOCKET_TIMEOUT = 2.0

UNTRUSTED_CWD_VARS = ("HERDR_ACTIVE_PANE_CWD", "PWD")

# The caller's original PATH, saved by the launcher (which gives Python a reduced one).
HOST_PATH_VAR = "QUOTARIBBON_HOST_PATH"


def host_path(env: Mapping[str, str]) -> str:
    value = env.get(HOST_PATH_VAR)
    if value is None:
        value = env.get("PATH")
    return value or ""


def child_env_with_path(env: Mapping[str, str], *, caller_cwd: str | None = None) -> dict:
    """Child env: trusted ``PATH`` only, no saved host PATH, and no exported shell
    functions (``BASH_FUNC_*`` would override builtins in the child's ``sh`` scripts)."""
    out = {k: v for k, v in env.items() if not k.startswith("BASH_FUNC_")}
    out["PATH"] = trusted_child_path(env, caller_cwd=caller_cwd)
    out.pop(HOST_PATH_VAR, None)
    return out


def _within(path: str, root: str) -> bool:
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _is_exec_regular(path: str) -> bool:
    from .trust import trusted_executable

    return trusted_executable(path)


def _world_writable(directory: str) -> bool:
    try:
        st = os.stat(directory)
    except OSError:
        return True  # unreadable: do not trust
    return bool(st.st_mode & stat.S_IWOTH)


def untrusted_dirs(env: Mapping[str, str], caller_cwd: str | None = None) -> list[str]:
    """Pane cwd, ``PWD`` and caller cwd (as given and realpath): never searched for executables."""
    out: list[str] = []
    cands = [env.get(v) for v in UNTRUSTED_CWD_VARS]
    if caller_cwd is None:
        try:
            caller_cwd = os.getcwd()
        except OSError:
            caller_cwd = None
    cands.append(caller_cwd)
    for c in cands:
        if isinstance(c, str) and c and os.path.isabs(c):
            for v in (os.path.normpath(c), os.path.realpath(c)):
                if v not in out:
                    out.append(v)
    return out


def _path_entries(env: Mapping[str, str], caller_cwd: str | None = None) -> list[str]:
    """Trusted ``PATH`` entries, as spelled.

    Drops empty/relative entries, untrusted cwds and anything below them (except below
    ``/`` or ``$HOME``, which would reject ``~/.local/bin``), and dirs others can modify.
    """
    home = env.get("HOME") or os.path.expanduser("~")
    home_variants = {os.path.normpath(home), os.path.realpath(home)} if home and os.path.isabs(home) else set()
    bad = untrusted_dirs(env, caller_cwd)
    out: list[str] = []
    for entry in host_path(env).split(os.pathsep):
        if not entry or not os.path.isabs(entry) or entry in out:
            continue
        norm = os.path.normpath(entry)
        real = os.path.realpath(entry)
        skip = False
        for b in bad:
            if norm == b or real == b:
                skip = True
                break
            if b == os.sep or b in home_variants:
                continue
            if _within(norm, b) or _within(real, b):
                skip = True
                break
        if skip or _world_writable(entry):
            continue
        out.append(entry)
    return _core("trust").trusted_path_entries(os.pathsep.join(out)) if out else []


def trusted_path_lookup(name: str, env: Mapping[str, str], *, caller_cwd: str | None = None) -> str | None:
    """First trusted ``name`` on the trusted ``PATH``, as spelled (symlinks kept for self-updates)."""
    for entry in _path_entries(env, caller_cwd):
        cand = os.path.join(entry, name)
        if _is_exec_regular(cand):
            return cand if _interpreter_ok(cand, env, caller_cwd) else None
    return None


def trusted_executable_ok(path: object, env: Mapping[str, str], *, caller_cwd: str | None = None) -> bool:
    """Absolute, trusted executable whose ``#!`` interpreter (if any) is trusted too."""
    return (isinstance(path, str) and os.path.isabs(path) and "\0" not in path and _is_exec_regular(path)
            and _interpreter_ok(path, env, caller_cwd))


def _interpreter_ok(executable: str, env: Mapping[str, str], caller_cwd: str | None = None) -> bool:
    """``#!/usr/bin/env X`` resolves X only within the exact ``PATH`` the child will get."""
    search = trusted_child_path(env, caller_cwd=caller_cwd).split(os.pathsep)
    return _core("trust").trusted_shebang(executable, search)


def trusted_child_path(env: Mapping[str, str], *, caller_cwd: str | None = None) -> str:
    """Trusted entries only, so a script CLI cannot get its interpreter from an untrusted dir.

    Never empty (that would mean the cwd): falls back to ``/usr/bin:/bin``, then nowhere.
    """
    entries = _path_entries(env, caller_cwd)
    if not entries:
        entries = _core("trust").trusted_path_entries(FALLBACK_CHILD_PATH)
    return os.pathsep.join(entries) if entries else NOWHERE_PATH


class LiveHostAdapter:
    """``types.HostAdapter`` over the action's ``env`` and resolved ``settings``."""

    def __init__(self, paths: Paths, env: Mapping[str, str], settings, *,
                 socket_timeout: float = EVIDENCE_SOCKET_TIMEOUT,
                 integration_text: Callable[[Mapping[str, str], Mapping[str, str]], str | None] | None = None) -> None:
        self.paths = paths
        self.env = dict(env)
        self.settings = settings
        self.socket_timeout = socket_timeout
        self._integration_text = integration_text or (
            lambda env_, extra: integration_status_text(dict(env_), extra_env=dict(extra))
        )

    def _client(self) -> HerdrClient | None:
        sock = self.env.get("HERDR_SOCKET_PATH")
        if not sock:
            return None
        return HerdrClient(sock, self.socket_timeout)

    def plugin_active(self) -> bool:
        """``plugin.list`` says our plugin is registered and enabled. Never raises."""
        try:
            client = self._client()
            if client is None:
                return False
            entry = find_plugin(client.request("plugin.list", {"plugin_id": PLUGIN_ID}), PLUGIN_ID)
            return plugin_enabled(entry)
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def herdr_config_path(self) -> str:
        return _core("binding").herdr_config_path(self.env)

    def resolve_profile(self, provider: str, ps: ProviderSettings) -> str:
        return _core("binding").resolve_profile(provider, ps, self.env)

    def credential_namespace(self, provider: str, ps: ProviderSettings) -> str:
        return _core("binding").credential_namespace(provider, ps, self.env)

    def server_identity(self) -> tuple[str, str | None]:
        return _core("binding").server_identity(self.env)

    def default_profile(self, provider: str) -> str:
        """Realpath of the profile the Herdr server environment uses for ``provider``."""
        return _core("binding").resolve_profile(provider, None, self.env)

    def is_default_profile(self, provider: str, profile_dir: str, ps: ProviderSettings | None = None) -> bool:
        """No ``profile_dir`` override and ``profile_dir`` is the server's own profile."""
        ps = ps if ps is not None else self._provider_settings(provider)
        if ps is not None and ps.profile_dir is not None:
            return False
        return os.path.realpath(profile_dir) == self.default_profile(provider)

    def profile_spelling(self, provider: str, profile_dir: str, ps: ProviderSettings | None = None) -> str | None:
        """The user's literal ``profile_dir`` spelling, or None.

        Claude keys its keychain entry on the literal ``CLAUDE_CONFIG_DIR`` string.
        """
        ps = ps if ps is not None else self._provider_settings(provider)
        literal = ps.profile_dir if ps is not None else None
        if isinstance(literal, str) and os.path.isabs(literal) and os.path.realpath(literal) == os.path.realpath(profile_dir):
            return literal
        return None

    def _provider_settings(self, provider: str) -> ProviderSettings | None:
        s = self.settings
        try:
            return s.providers[provider] if s is not None else None
        except (KeyError, AttributeError, TypeError):
            return None

    def evidence(self, provider: str, profile_dir: str, ps: ProviderSettings | None = None) -> Evidence:
        """Herdr linkage evidence for ``provider`` + ``profile_dir``.

        Default profile: integration state, else a running agent (``inferred``).
        Overridden profile: only ``herdr integration status`` for that profile; agents
        never establish an override.
        """
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        client = self._client()
        if client is None:
            return _unknown("herdr_unreachable")
        try:
            integrations = client.integration_list()
            agents = client.agent_list()
        except HerdrUnavailable:
            return _unknown("herdr_unreachable")
        except HerdrError:
            return _unknown("herdr_error")
        server_state = _integration_state(integrations, provider)
        agent_detected = any(a.get("agent") == provider for a in agents)

        if self.is_default_profile(provider, profile_dir, ps):
            if server_state in ("current", "outdated"):
                return Evidence(True, "integration", server_state, agent_detected, "match", None)
            if agent_detected:
                return Evidence(True, "agent", server_state, True, "inferred", None)
            return Evidence(False, None, server_state, False, "unknown", None)

        extra = {_core("binding").PROFILE_ENV[provider]: self.profile_spelling(provider, profile_dir, ps) or profile_dir}
        try:
            text = self._integration_text(self.env, extra)
        except Exception:  # noqa: BLE001
            text = None
        state = parse_integration_states(text).get(provider) if text is not None else None
        if state in ("current", "outdated"):
            return Evidence(True, "integration", state, agent_detected, "match", None)
        if state == "not_installed":
            corr = "mismatch" if server_state in ("current", "outdated") else "unknown"
            return Evidence(agent_detected, "agent" if agent_detected else None, "not_installed",
                            agent_detected, corr, None)
        return Evidence(agent_detected, "agent" if agent_detected else None, None, agent_detected, "unknown", None)

    def resolve_executable(self, provider: str, ps: ProviderSettings) -> str | None:
        """Trusted CLI path or None; a ``binary`` setting never falls back to ``PATH``."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if ps is not None and ps.binary is not None:
            b = ps.binary
            if trusted_executable_ok(b, self.env):
                # Return the validated spelling: normalising ``link/..`` could pick an unchecked file.
                return b
            return None
        return trusted_path_lookup(EXECUTABLE_NAMES[provider], self.env)

    def child_env(self) -> dict:
        return child_env_with_path(self.env)

    def make_collector(self, provider: str, executable: str, profile_dir: str, *, lock_fd: int | None = None,
                       ps: ProviderSettings | None = None):
        """Build the provider collector.

        ``lock_fd`` stays in the refresh: once registered with ``procutil.register_lock_owner``
        it is withheld from every provider child.
        """
        salt = _core("cache").read_salt(self.paths)
        kwargs = dict(
            executable=executable,
            profile_dir=profile_dir,
            work_dir=self.paths.work_dir(provider),
            env=self.child_env(),
            salt=salt,
            lock_fd=lock_fd,
            profile_spelling=self.profile_spelling(provider, profile_dir, ps),
        )
        if provider == "claude":
            return _core("providers.claude").ClaudeCollector(**kwargs)
        if provider == "codex":
            return _core("providers.codex").CodexCollector(**kwargs)
        raise ValueError(f"unknown provider {provider!r}")


def _integration_state(integrations: list[dict], provider: str) -> str | None:
    for item in integrations:
        if item.get("target") == provider:
            st = item.get("state")
            return st if st in ("current", "outdated", "not_installed") else None
    return None


def _unknown(error: str) -> Evidence:
    return Evidence(False, None, None, False, "unknown", error)


__all__ = [
    "HOST_PATH_VAR",
    "LiveHostAdapter",
    "child_env_with_path",
    "host_path",
    "trusted_executable_ok",
    "trusted_path_lookup",
    "untrusted_dirs",
]
