"""Provider bindings: which profile + Herdr config a provider's state belongs to.

Tab-bar tick module: only ``os`` at module level and no ``hashlib`` (the builtin sha256 is
imported lazily); arguments accept dataclasses or their JSON dicts.
"""

from __future__ import annotations

import os

PROFILE_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
DEFAULT_PROFILE_DIRNAME = {"claude": ".claude", "codex": ".codex"}

HERDR_CONFIG_ENV = "HERDR_CONFIG_PATH"
DEFAULT_HERDR_CONFIG = (".config", "herdr", "config.toml")

_sha256_ctor = None


def _sha256():
    global _sha256_ctor
    if _sha256_ctor is None:
        try:
            from _sha2 import sha256 as ctor  # Python >= 3.12 builtin
        except ImportError:  # pragma: no cover - depends on the interpreter build
            try:
                from _sha256 import sha256 as ctor  # Python 3.11
            except ImportError:
                from hashlib import sha256 as ctor
        _sha256_ctor = ctor
    return _sha256_ctor


def sha256_hex(*parts: str | bytes | int) -> str:
    """Hex SHA-256 of ``parts`` joined by NUL bytes, so part boundaries cannot collide."""
    h = _sha256()()
    for i, part in enumerate(parts):
        if i:
            h.update(b"\0")
        if isinstance(part, bytes):
            h.update(part)
        elif isinstance(part, bool) or not isinstance(part, (str, int)):
            raise TypeError(f"unsupported hash part type: {type(part).__name__}")
        else:
            h.update(str(part).encode("utf-8"))
    return h.hexdigest()


def _get(obj: object, key: str) -> object:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _provider_settings(settings: object, provider: str) -> object:
    providers = _get(settings, "providers")
    if providers is None:
        raise ValueError("settings without providers")
    return providers[provider]  # type: ignore[index]


def home_dir(env: object) -> str:
    home = env.get("HOME") if env is not None else None  # type: ignore[union-attr]
    if isinstance(home, str) and os.path.isabs(home):
        return home
    return os.path.expanduser("~")


def resolve_profile(provider: str, ps: object, env: object) -> str:
    """Realpath of the profile dir: settings, then the env variable, then the default.

    Relative env values are ignored: they would resolve against an arbitrary cwd.
    """
    if provider not in PROFILE_ENV:
        raise ValueError(f"unknown provider {provider!r}")
    override = _get(ps, "profile_dir") if ps is not None else None
    if isinstance(override, str) and os.path.isabs(override):
        return os.path.realpath(override)
    value = env.get(PROFILE_ENV[provider]) if env is not None else None  # type: ignore[union-attr]
    if isinstance(value, str) and value and os.path.isabs(value):
        return os.path.realpath(value)
    return os.path.realpath(os.path.join(home_dir(env), DEFAULT_PROFILE_DIRNAME[provider]))


def herdr_config_path(env: object) -> str:
    """Absolute Herdr config path, symlinks resolved so every process computes the same binding."""
    value = env.get(HERDR_CONFIG_ENV) if env is not None else None  # type: ignore[union-attr]
    if isinstance(value, str) and value and os.path.isabs(value):
        return os.path.realpath(value)
    return os.path.realpath(os.path.join(home_dir(env), *DEFAULT_HERDR_CONFIG))


def credential_namespace(provider: str, ps: object, env: object) -> str:
    """Which credential store the provider CLI will use for this profile.

    Claude keys its credentials on the literal ``CLAUDE_CONFIG_DIR`` string ("unset" differs
    from "set to ~/.claude"), so servers whose children see different values never share records.
    """
    if provider not in PROFILE_ENV:
        raise ValueError(f"unknown provider {provider!r}")
    if provider != "claude":
        return ""
    value = env.get(PROFILE_ENV[provider]) if env is not None else None  # type: ignore[union-attr]
    if isinstance(value, str) and value:
        return "env:" + value
    profile = resolve_profile(provider, ps, env)
    if profile == os.path.realpath(os.path.join(home_dir(env), DEFAULT_PROFILE_DIRNAME[provider])):
        return ""
    literal = _get(ps, "profile_dir") if ps is not None else None
    if isinstance(literal, str) and os.path.isabs(literal) and os.path.realpath(literal) == profile:
        return "set:" + literal
    return "set:" + profile


SOCKET_ENV = "HERDR_SOCKET_PATH"


def server_identity(env: object) -> tuple[str, str | None]:
    """``(socket_path, identity)`` of the live Herdr server socket.

    A restarted server recreates its socket, so the identity changes even without the
    ``startup`` hook. A None identity cannot be proven: callers show no numbers.
    """
    path = env.get(SOCKET_ENV) if env is not None else None  # type: ignore[union-attr]
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or len(path) > 1024:
        return ("", None)
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return (path, None)
    return (path, f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}")


def host_identity() -> tuple[str, int]:
    return os.uname().nodename, os.getuid()


class Binding:
    """Immutable, value-equal binding of one provider."""

    __slots__ = ("binding_key", "profile_key", "profile_dir", "herdr_config_path")

    def __init__(self, binding_key: str, profile_key: str, profile_dir: str, herdr_config_path: str) -> None:
        object.__setattr__(self, "binding_key", binding_key)
        object.__setattr__(self, "profile_key", profile_key)
        object.__setattr__(self, "profile_dir", profile_dir)
        object.__setattr__(self, "herdr_config_path", herdr_config_path)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Binding is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Binding is immutable")

    def _key(self) -> tuple[str, str, str, str]:
        return (self.binding_key, self.profile_key, self.profile_dir, self.herdr_config_path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Binding):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return (
            f"Binding(binding_key={self.binding_key!r}, profile_key={self.profile_key!r}, "
            f"profile_dir={self.profile_dir!r}, herdr_config_path={self.herdr_config_path!r})"
        )

    def to_json(self) -> dict:
        return {
            "binding_key": self.binding_key,
            "profile_key": self.profile_key,
            "profile_dir": self.profile_dir,
            "herdr_config_path": self.herdr_config_path,
        }


def make_binding(
    provider: str,
    profile_dir: str,
    herdr_config: str,
    *,
    namespace: str = "",
    host: str | None = None,
    uid: int | None = None,
) -> Binding:
    """Build the binding for an already-resolved profile realpath and Herdr config path."""
    if provider not in PROFILE_ENV:
        raise ValueError(f"unknown provider {provider!r}")
    h, u = host_identity()
    host = h if host is None else host
    uid = u if uid is None else uid
    parts = (host, uid, herdr_config, provider, profile_dir) + ((namespace,) if namespace else ())
    return Binding(
        binding_key=sha256_hex(*parts)[:24],
        profile_key=sha256_hex(provider, profile_dir)[:16],
        profile_dir=profile_dir,
        herdr_config_path=herdr_config,
    )


def binding_for(provider: str, settings: object, env: object) -> Binding:
    ps = _provider_settings(settings, provider)
    return make_binding(provider, resolve_profile(provider, ps, env), herdr_config_path(env),
                        namespace=credential_namespace(provider, ps, env))


def scope_key(provider: str, profile_dir: str, *, host: str | None = None, uid: int | None = None) -> str:
    """Opaque local scope of a snapshot: host, OS user, provider and profile."""
    h, u = host_identity()
    return sha256_hex("scope", h if host is None else host, u if uid is None else uid, provider, profile_dir)[:24]


__all__ = [
    "PROFILE_ENV",
    "DEFAULT_PROFILE_DIRNAME",
    "HERDR_CONFIG_ENV",
    "sha256_hex",
    "home_dir",
    "resolve_profile",
    "herdr_config_path",
    "host_identity",
    "credential_namespace",
    "server_identity",
    "Binding",
    "make_binding",
    "binding_for",
    "scope_key",
]
