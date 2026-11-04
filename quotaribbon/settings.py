"""User settings: strict load/validate, last-good fallback, ON/OFF toggles.

Last-good adoption and toggles run under ``locks/settings.lock`` on a fresh re-read, so a
stale read never overwrites a newer toggle. Module imports stay lean for the tab-bar tick:
the dataclass layer is created lazily on first attribute access.
"""

from __future__ import annotations

import json
import os

from . import binding as _binding
from . import cache as _cache
from . import constants as _C

PROVIDERS = ("claude", "codex")
SCHEMA_VERSION = 1
DEFAULT_INTERVAL = _C.DEFAULT_INTERVAL
DEFAULT_STALE_AFTER = _C.DEFAULT_STALE_AFTER
DEFAULT_HIDE_AFTER = _C.DEFAULT_HIDE_AFTER
INTERVAL_MIN = _C.INTERVAL_MIN
INTERVAL_MAX = _C.INTERVAL_MAX
HIDE_AFTER_MAX = _C.HIDE_AFTER_MAX
DISPLAY_MODES = ("used", "remaining")
DISPLAY_FORMATS = ("full", "compact")

SOURCES = ("file", "default", "last_good", "none")


def settings_path(config_dir: str) -> str:
    return os.path.join(config_dir, "settings.json")


def last_good_path(state_dir: str) -> str:
    return os.path.join(state_dir, "settings.last_good.json")


# Lean dict validation: accepts exactly what types.Settings.from_json accepts.


def _err(msg: str) -> None:
    raise ValueError(msg)


def _check_obj(v: object, name: str) -> dict:
    if not isinstance(v, dict):
        _err(f"{name}: expected an object")
    for k in v:  # type: ignore[union-attr]
        if not isinstance(k, str):
            _err(f"{name}: keys must be strings")
    return v  # type: ignore[return-value]


def _check_keys(d: dict, name: str, required: tuple, optional: tuple = ()) -> None:
    missing = [k for k in required if k not in d]
    if missing:
        _err(f"{name}: missing key {missing[0]}")
    allowed = set(required) | set(optional)
    # Never echo the user's key names or values: messages are fixed reason strings.
    if any(k not in allowed for k in d):
        _err(f"{name}: unknown key")


def _check_int(v: object, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not -(1 << 53) <= v <= (1 << 53):
        _err(f"{name}: expected an integer")
    return v  # type: ignore[return-value]


def _check_abs(v: object, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        _err(f"{name}: expected a string")
    if not v.startswith("/"):  # type: ignore[union-attr]
        _err(f"{name}: must be an absolute path")
    return v  # type: ignore[return-value]


def validate_settings_obj(obj: object) -> dict:
    """Validate parsed ``settings.json``; return the normalised layout or raise ``ValueError``."""
    o = _check_obj(obj, "settings")
    _check_keys(o, "settings", ("schema_version", "providers"), ("display", "refresh"))
    sv = o["schema_version"]
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
        _err(f"settings.schema_version: unsupported (expected {SCHEMA_VERSION})")
    provs = _check_obj(o["providers"], "settings.providers")
    _check_keys(provs, "settings.providers", PROVIDERS)
    out_provs: dict = {}
    for p in PROVIDERS:
        name = f"settings.providers.{p}"
        ps = _check_obj(provs[p], name)
        _check_keys(ps, name, ("enabled",), ("binary", "profile_dir"))
        if not isinstance(ps["enabled"], bool):
            _err(f"{name}.enabled: expected true or false")
        entry: dict = {"enabled": ps["enabled"]}
        binary = _check_abs(ps.get("binary"), f"{name}.binary")
        profile = _check_abs(ps.get("profile_dir"), f"{name}.profile_dir")
        if binary is not None:
            entry["binary"] = binary
        if profile is not None:
            entry["profile_dir"] = profile
        out_provs[p] = entry
    display = _check_obj(o.get("display", {}), "settings.display")
    _check_keys(display, "settings.display", (), ("mode", "format", "show_reset", "window_labels"))
    mode = display.get("mode", "used")
    if not isinstance(mode, str) or mode not in DISPLAY_MODES:
        _err('settings.display.mode: must be "used" or "remaining"')
    fmt = display.get("format", "full")
    if not isinstance(fmt, str) or fmt not in DISPLAY_FORMATS:
        _err('settings.display.format: must be "full" or "compact"')
    show_reset = display.get("show_reset", True)
    if not isinstance(show_reset, bool):
        _err("settings.display.show_reset: expected true or false")
    window_labels = display.get("window_labels", True)
    if not isinstance(window_labels, bool):
        _err("settings.display.window_labels: expected true or false")
    refresh = _check_obj(o.get("refresh", {}), "settings.refresh")
    _check_keys(refresh, "settings.refresh", (), ("interval_seconds", "stale_after_seconds", "hide_values_after_seconds"))
    interval = _check_int(refresh.get("interval_seconds", DEFAULT_INTERVAL), "settings.refresh.interval_seconds")
    raw_stale = refresh.get("stale_after_seconds", _C.MISSING)
    raw_hide = refresh.get("hide_values_after_seconds", _C.MISSING)
    if raw_stale is not _C.MISSING:
        _check_int(raw_stale, "settings.refresh.stale_after_seconds")
    if raw_hide is not _C.MISSING:
        _check_int(raw_hide, "settings.refresh.hide_values_after_seconds")
    stale, hide = _C.resolve_refresh(interval, raw_stale, raw_hide)
    assert isinstance(stale, int) and isinstance(hide, int)
    if not INTERVAL_MIN <= interval <= INTERVAL_MAX:
        _err(f"settings.refresh.interval_seconds: must be {INTERVAL_MIN}..{INTERVAL_MAX}")
    if stale < 2 * interval:
        _err("settings.refresh.stale_after_seconds: must be >= 2 * interval_seconds")
    if not interval < stale < hide:
        _err("settings.refresh: require interval_seconds < stale_after_seconds < hide_values_after_seconds")
    if hide > HIDE_AFTER_MAX:
        _err(f"settings.refresh.hide_values_after_seconds: must be <= {HIDE_AFTER_MAX}")
    return {
        "schema_version": SCHEMA_VERSION,
        "providers": out_provs,
        "display": {"mode": mode, "format": fmt, "show_reset": show_reset, "window_labels": window_labels},
        "refresh": {
            "interval_seconds": interval,
            "stale_after_seconds": stale,
            "hide_values_after_seconds": hide,
        },
    }


def default_settings_dict() -> dict:
    return validate_settings_obj({"schema_version": 1, "providers": {p: {"enabled": True} for p in PROVIDERS}})


def provider_settings_dict(ps: object) -> dict:
    """``ProviderSettings`` or its dict → normalised dict."""
    if isinstance(ps, dict):
        get = ps.get
    else:
        def get(k, _ps=ps):  # noqa: E306
            return getattr(_ps, k, None)
    out: dict = {"enabled": get("enabled")}
    for k in ("binary", "profile_dir"):
        if get(k) is not None:
            out[k] = get(k)
    return out


def provider_fingerprint(ps: object) -> str:
    """``settings_fingerprint`` of one provider's effective settings.

    The binding key is deliberately left out: two Herdr servers sharing the global
    ``control.json`` would otherwise bump each other's generation on every refresh.
    """
    text = json.dumps(provider_settings_dict(ps), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _binding.sha256_hex("settings-fingerprint-v1", text)


def settings_fingerprints(settings: object) -> dict:
    provs = settings["providers"] if isinstance(settings, dict) else settings.providers  # type: ignore[union-attr]
    return {p: provider_fingerprint(provs[p]) for p in PROVIDERS}


def load_tick(config_dir: str, state_dir: str) -> tuple[dict | None, str]:
    """Read-only settings for the tab-bar tick: ``(settings_dict | None, source)``. Never raises."""
    path = settings_path(config_dir)
    status, obj = _cache.read_json_status(path)
    if status == _cache.MISSING:
        return default_settings_dict(), "default"
    if status == _cache.OK:
        try:
            return validate_settings_obj(obj), "file"
        except ValueError as exc:
            _cache.report_bad_file(state_dir, path, f"invalid settings: {exc}")
    else:
        _cache.report_bad_file(state_dir, path, str(obj))
    status, obj = _cache.read_json_status(last_good_path(state_dir))
    if status == _cache.OK:
        try:
            return validate_settings_obj(obj), "last_good"
        except ValueError:
            pass
    return None, "none"


_HEAVY_NAMES = ("LoadedSettings", "ToggleResult", "IntervalResult", "DEFAULT_SETTINGS")


def _define_heavy() -> None:
    g = globals()
    if "LoadedSettings" in g:
        return
    from dataclasses import dataclass

    from . import types as t

    @dataclass(frozen=True)
    class LoadedSettings:

        settings: t.Settings | None
        valid: bool
        source: str
        errors: tuple[str, ...] = ()

    @dataclass(frozen=True)
    class ToggleResult:

        changed: bool
        enabled: bool
        generation: int
        error: str | None = None

    @dataclass(frozen=True)
    class IntervalResult:

        changed: bool
        interval_seconds: int
        stale_after_seconds: int
        hide_values_after_seconds: int
        timings_adjusted: bool = False
        error: str | None = None

    for cls in (LoadedSettings, ToggleResult, IntervalResult):
        cls.__module__ = __name__
        cls.__qualname__ = cls.__name__
    g["LoadedSettings"] = LoadedSettings
    g["ToggleResult"] = ToggleResult
    g["IntervalResult"] = IntervalResult
    g["DEFAULT_SETTINGS"] = t.Settings.default()


def __getattr__(name: str) -> object:
    if name in _HEAVY_NAMES:
        _define_heavy()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_settings(obj: object):
    """Strictly parse ``settings.json`` content into ``types.Settings``."""
    validate_settings_obj(obj)  # readable messages first
    from .types import Settings

    return Settings.from_json(obj)


def settings_to_json(s) -> dict:
    return s.to_json()


def settings_to_file_json(s) -> dict:
    """What actions write to ``settings.json`` (derived stale/hide values left out)."""
    return s.to_file_json()


def _read_last_good(paths):
    status, obj = _cache.read_json_status(paths.settings_last_good)
    if status != _cache.OK:
        return None
    try:
        return parse_settings(obj)
    except ValueError:
        return None


def adopt_last_good(paths, s) -> None:
    """Store ``s`` as last good if it differs. Never raises.

    The caller must hold ``locks/settings.lock`` and ``s`` must come from a read made under
    it; otherwise a stale ON could overwrite an OFF a toggle saved meanwhile.
    """
    try:
        data = settings_to_file_json(s)  # same form as settings.json: defaults left out
        status, current = _cache.read_json_status(paths.settings_last_good)
        if status == _cache.OK and current == data:
            return
        from . import fsutil

        fsutil.ensure_private_dir(paths.state_dir)
        fsutil.atomic_write_json(paths.settings_last_good, data)
    except Exception:  # noqa: BLE001 - adoption is best effort
        pass


def _parse_current(paths):
    status, obj = _cache.read_json_status(paths.settings)
    if status == _cache.MISSING:
        return status, None, None
    if status != _cache.OK:
        return status, None, f"settings.json: {obj}"
    try:
        return status, parse_settings(obj), None
    except ValueError as exc:
        return _cache.CORRUPT, None, f"settings.json: {exc}"


def adopt_current_locked(paths) -> None:
    """Re-read ``settings.json`` and adopt it when valid; caller holds ``locks/settings.lock``."""
    try:
        status, s, _msg = _parse_current(paths)
        if status == _cache.OK and s is not None:
            adopt_last_good(paths, s)
    except Exception:  # noqa: BLE001 - adoption is best effort
        pass


def _adopt_under_lock(paths) -> None:
    try:
        from . import fsutil

        fsutil.ensure_private_dir(paths.state_dir)
        with fsutil.file_lock(paths.settings_lock, blocking=True):
            adopt_current_locked(paths)
    except Exception:  # noqa: BLE001 - adoption is best effort
        pass


def load(paths, *, update_last_good: bool):
    """Load settings for an action; an invalid file falls back to last good. Never raises for bad files."""
    _define_heavy()
    from . import types as t

    status, obj = _cache.read_json_status(paths.settings)
    if status == _cache.MISSING:
        return LoadedSettings(t.Settings.default(), True, "default", ())  # noqa: F821
    errors: list[str] = []
    if status == _cache.OK:
        try:
            s = parse_settings(obj)
        except ValueError as exc:
            errors.append(f"settings.json: {exc}")
        else:
            if update_last_good:
                # Never adopt this possibly stale read: adopt what is on disk under the lock.
                # Must not be called while holding the lock.
                _adopt_under_lock(paths)
            return LoadedSettings(s, True, "file", ())  # noqa: F821
    else:
        errors.append(f"settings.json: {obj}")
    last = _read_last_good(paths)
    if last is not None:
        return LoadedSettings(last, False, "last_good", tuple(errors))  # noqa: F821
    return LoadedSettings(None, False, "none", tuple(errors))  # noqa: F821


def _log(paths, msg: str, *args: object) -> None:
    try:
        from .log import get_logger

        get_logger(paths).warning(msg, *args)
    except Exception:  # noqa: BLE001
        pass


def set_enabled(paths, provider: str, enabled: bool):
    """Set ``providers.<provider>.enabled`` under ``locks/settings.lock``.

    An invalid file is refused untouched. ``control.json`` is written before ``settings.json``.
    """
    _define_heavy()
    from dataclasses import replace

    from . import fsutil
    from . import types as t

    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    fsutil.ensure_private_dir(paths.state_dir)
    with fsutil.file_lock(paths.settings_lock, blocking=True):
        status, obj = _cache.read_json_status(paths.settings)
        error = None
        current = None
        if status == _cache.MISSING:
            current = t.Settings.default()
        elif status == _cache.OK:
            try:
                current = parse_settings(obj)
            except ValueError as exc:
                error = f"settings.json is invalid; nothing changed ({exc})"
        else:
            error = f"settings.json is unreadable; nothing changed ({obj})"
        if current is None:
            _log(paths, "toggle %s-%s refused: %s", provider, "on" if enabled else "off", error)
            last = _read_last_good(paths)
            effective = bool(last is not None and last.providers[provider].enabled)
            gen = _cache.read_control(paths)[provider]
            return ToggleResult(False, effective, gen, error)  # noqa: F821
        if status == _cache.OK:
            adopt_last_good(paths, current)
        old = current.providers[provider].enabled
        providers = dict(current.providers)
        providers[provider] = replace(providers[provider], enabled=enabled)
        updated = replace(current, providers=providers)
        updated.validate()
        gens = _cache.sync_fingerprints(paths, settings_fingerprints(updated))
        if old != enabled or status == _cache.MISSING:
            fsutil.ensure_private_dir(paths.config_dir)
            fsutil.atomic_write_json(paths.settings, settings_to_file_json(updated))
        adopt_last_good(paths, updated)
        return ToggleResult(old != enabled, enabled, gens[provider], None)  # noqa: F821


def set_interval(paths, seconds: int):
    """Set ``refresh.interval_seconds`` under ``locks/settings.lock``.

    Custom stale/hide values are kept when they still fit the new interval; otherwise both are
    derived from it again. An invalid file is refused untouched.
    """
    _define_heavy()
    from dataclasses import replace

    from . import fsutil
    from . import types as t

    if isinstance(seconds, bool) or not isinstance(seconds, int) or not INTERVAL_MIN <= seconds <= INTERVAL_MAX:
        raise ValueError(f"interval must be an integer {INTERVAL_MIN}..{INTERVAL_MAX}")
    fsutil.ensure_private_dir(paths.state_dir)
    with fsutil.file_lock(paths.settings_lock, blocking=True):
        status, obj = _cache.read_json_status(paths.settings)
        error = None
        current = None
        if status == _cache.MISSING:
            current = t.Settings.default()
        elif status == _cache.OK:
            try:
                current = parse_settings(obj)
            except ValueError as exc:
                error = f"settings.json is invalid; nothing changed ({exc})"
        else:
            error = f"settings.json is unreadable; nothing changed ({obj})"
        if current is None:
            _log(paths, "set interval %s refused: %s", seconds, error)
            last = _read_last_good(paths)
            base = last or t.Settings.default()
            return IntervalResult(False, base.interval_seconds, base.stale_after_seconds,  # noqa: F821
                                  base.hide_values_after_seconds, False, error)
        if status == _cache.OK:
            adopt_last_good(paths, current)
        old_timings = (current.stale_after_seconds, current.hide_values_after_seconds)
        custom = old_timings != _C.derived_refresh(current.interval_seconds)
        updated = replace(current, interval_seconds=seconds)
        if custom:
            try:
                updated.validate()
            except ValueError:
                custom = False
        if not custom:
            stale, hide = _C.derived_refresh(seconds)
            updated = replace(updated, stale_after_seconds=stale, hide_values_after_seconds=hide)
        updated.validate()
        changed = updated != current
        if changed or status == _cache.MISSING:
            fsutil.ensure_private_dir(paths.config_dir)
            fsutil.atomic_write_json(paths.settings, settings_to_file_json(updated))
        adopt_last_good(paths, updated)
        new_timings = (updated.stale_after_seconds, updated.hide_values_after_seconds)
        adjusted = old_timings != new_timings and old_timings != _C.derived_refresh(current.interval_seconds)
        return IntervalResult(changed, seconds, new_timings[0], new_timings[1], adjusted, None)  # noqa: F821


__all__ = [
    "SOURCES",
    "validate_settings_obj",
    "default_settings_dict",
    "provider_settings_dict",
    "provider_fingerprint",
    "settings_fingerprints",
    "load_tick",
    "parse_settings",
    "settings_to_json",
    "settings_to_file_json",
    "adopt_last_good",
    "load",
    "set_enabled",
    "set_interval",
    "LoadedSettings",
    "ToggleResult",
    "IntervalResult",
    "DEFAULT_SETTINGS",
]
