"""On-disk layout of the config and state directories (dirs 0700, files 0600)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

from .fsutil import ensure_private_dir
from .types import PROVIDERS

ENV_CONFIG_DIR = "HERDR_PLUGIN_CONFIG_DIR"
ENV_STATE_DIR = "HERDR_PLUGIN_STATE_DIR"

_KEY_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
BINDING_KEY_RE = re.compile(r"[0-9a-f]{24}\Z")


class PathsError(ValueError):
    """The config/state directories are missing, relative, or otherwise unusable."""


def _check_dir_value(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise PathsError(f"{what} is not set")
    if "\0" in value or not os.path.isabs(value):
        raise PathsError(f"{what} must be an absolute path")
    return os.path.normpath(value)


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise PathsError(f"unknown provider {provider!r}")
    return provider


def _check_binding_key(binding_key: str) -> str:
    if not isinstance(binding_key, str) or not BINDING_KEY_RE.match(binding_key):
        raise PathsError("invalid binding_key (expected 24 lowercase hex chars)")
    return binding_key


@dataclass(frozen=True)
class Paths:
    """Every QuotaRibbon path; provider paths validate their components so they cannot
    escape the state dir."""

    config_dir: str
    state_dir: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_dir", _check_dir_value(self.config_dir, "config_dir"))
        object.__setattr__(self, "state_dir", _check_dir_value(self.state_dir, "state_dir"))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Paths:
        env = os.environ if env is None else env
        return cls(
            config_dir=_check_dir_value(env.get(ENV_CONFIG_DIR), ENV_CONFIG_DIR),
            state_dir=_check_dir_value(env.get(ENV_STATE_DIR), ENV_STATE_DIR),
        )

    @property
    def settings(self) -> str:
        return os.path.join(self.config_dir, "settings.json")

    @property
    def tabbar_py(self) -> str:
        """Stable copy of ``bootstrap/tabbar.py`` written by setup."""
        return os.path.join(self.config_dir, "tabbar.py")

    @property
    def bootstrap_json(self) -> str:
        return os.path.join(self.config_dir, "bootstrap.json")

    @property
    def control(self) -> str:
        return os.path.join(self.state_dir, "control.json")

    @property
    def server_epoch(self) -> str:
        return os.path.join(self.state_dir, "server_epoch.json")

    @property
    def clock(self) -> str:
        return os.path.join(self.state_dir, "clock.json")

    @property
    def settings_last_good(self) -> str:
        return os.path.join(self.state_dir, "settings.last_good.json")

    @property
    def dispatch(self) -> str:
        return os.path.join(self.state_dir, "dispatch.json")

    @property
    def salt(self) -> str:
        """Random salt for ``account_key`` hashing."""
        return os.path.join(self.state_dir, "salt")

    @property
    def setup_record(self) -> str:
        """Record of the tab-bar entry setup added."""
        return os.path.join(self.state_dir, "setup.json")

    @property
    def providers_dir(self) -> str:
        return os.path.join(self.state_dir, "providers")

    def provider_dir(self, provider: str) -> str:
        return os.path.join(self.providers_dir, _check_provider(provider))

    def binding_dir(self, provider: str, binding_key: str) -> str:
        """Per-binding records, so different bindings never see each other's data."""
        return os.path.join(self.provider_dir(provider), _check_binding_key(binding_key))

    def connection(self, provider: str, binding_key: str) -> str:
        return os.path.join(self.binding_dir(provider, binding_key), "connection.json")

    def status(self, provider: str, binding_key: str) -> str:
        return os.path.join(self.binding_dir(provider, binding_key), "status.json")

    def snapshot(self, provider: str, binding_key: str) -> str:
        return os.path.join(self.binding_dir(provider, binding_key), "snapshot.json")

    def ensure_binding_dir(self, provider: str, binding_key: str) -> str:
        """Create ``binding_dir`` (0700) and return it; refuses symlinked state directories."""
        d = self.binding_dir(provider, binding_key)
        for path in (self.providers_dir, self.provider_dir(provider), d):
            if os.path.islink(path):
                raise PathsError(f"refusing symlinked state directory: {path}")
        ensure_private_dir(d)
        return d

    def work_dir(self, provider: str) -> str:
        """Empty private cwd for provider CLI processes."""
        return os.path.join(self.state_dir, "work", _check_provider(provider))

    @property
    def locks_dir(self) -> str:
        return os.path.join(self.state_dir, "locks")

    @property
    def dispatch_lock(self) -> str:
        return os.path.join(self.locks_dir, "dispatch.lock")

    @property
    def settings_lock(self) -> str:
        return os.path.join(self.locks_dir, "settings.lock")

    @property
    def setup_lock(self) -> str:
        return os.path.join(self.locks_dir, "setup.lock")

    def provider_lock(self, provider: str, profile_key: str) -> str:
        if not isinstance(profile_key, str) or not _KEY_RE.match(profile_key):
            raise PathsError("invalid profile_key for lock path")
        return os.path.join(self.locks_dir, f"{_check_provider(provider)}-{profile_key}.lock")

    @property
    def backups_dir(self) -> str:
        return os.path.join(self.state_dir, "backups")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.state_dir, "logs")

    @property
    def log_file(self) -> str:
        return os.path.join(self.logs_dir, "quotaribbon.log")

    @property
    def pycache_dir(self) -> str:
        return os.path.join(self.state_dir, "pycache")

    def private_dirs(self) -> list[str]:
        """Directories :meth:`ensure_private_dirs` creates, parents first."""
        dirs = [self.config_dir, self.state_dir, self.providers_dir]
        dirs += [self.provider_dir(p) for p in PROVIDERS]
        dirs += [self.locks_dir, os.path.join(self.state_dir, "work")]
        dirs += [self.work_dir(p) for p in PROVIDERS]
        dirs += [self.backups_dir, self.logs_dir, self.pycache_dir]
        return dirs

    def ensure_private_dirs(self) -> None:
        """Create every private directory (0700); only the config/state roots may be symlinks."""
        for d in self.private_dirs():
            if d not in (self.config_dir, self.state_dir) and os.path.islink(d):
                raise PathsError(f"refusing symlinked state directory: {d}")
            ensure_private_dir(d)


__all__ = ["ENV_CONFIG_DIR", "ENV_STATE_DIR", "Paths", "PathsError"]
