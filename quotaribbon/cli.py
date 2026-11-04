"""Subcommand dispatcher for manifest actions, panes and the startup hook.

Exit codes: 0 ok, 1 refused/failed, 2 usage. Output lands in Herdr's log or a pane: never print secrets.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import select
import shlex
import sys
import time
from typing import Mapping, Sequence

from . import BOOTSTRAP_API, PLUGIN_ID, _core
from .fsutil import (
    CorruptFile,
    LockBusy,
    atomic_write_bytes,
    atomic_write_json,
    file_lock,
    open_regular,
    read_json,
    read_regular,
    special_file,
)
from .paths import Paths, PathsError
from .types import DISPLAY_NAMES, PROVIDERS

SUBCOMMANDS: tuple[str, ...] = (
    "claude-on",
    "claude-off",
    "codex-on",
    "codex-off",
    "refresh",
    "diagnostics",
    "setup",
    "detach",
    "interval",
    "startup",
    "setup-ui",
    "diagnostics-ui",
    "detach-ui",
    "interval-ui",
    "tabbar-preview",
)

# A headless popup must not wait forever.
PROMPT_TIMEOUT = 600.0

SHIM_MARKERS = ("/shims/", "/.asdf/", "/.pyenv/shims")
MIN_PYTHON = (3, 11)


class UsageError(Exception):
    pass


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _paths(env: Mapping[str, str]) -> Paths:
    """Plugin config/state dirs from the Herdr plugin env, else Herdr's standard locations."""
    try:
        return Paths.from_env(env)
    except PathsError:
        pass
    home = env.get("HOME") or os.path.expanduser("~")
    cfg = env.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(home, ".config", "herdr", "plugins", "config", PLUGIN_ID)
    state = env.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(home, ".local", "state", "herdr", "plugins", PLUGIN_ID)
    return Paths(config_dir=cfg, state_dir=state)


def _plugin_root() -> str:
    """The checkout this code runs from (never the cwd)."""
    return os.path.dirname(os.path.dirname(os.path.realpath(os.path.abspath(__file__))))


def _logger(paths: Paths):
    try:
        return _core("log").get_logger(paths)
    except Exception:  # noqa: BLE001
        return None


def _ask(question: str, *, timeout: float = PROMPT_TIMEOUT, stream=None) -> str | None:
    """Prompt and read one line; None on EOF/timeout (treated as "no")."""
    stream = sys.stdin if stream is None else stream
    sys.stdout.write(question)
    sys.stdout.flush()
    try:
        ready, _, _ = select.select([stream], [], [], timeout)
    except (OSError, ValueError, TypeError):
        ready = [stream]
    if not ready:
        _out("\n(no answer; treating it as no)")
        return None
    line = stream.readline()
    if not line:
        return None
    return line.strip()


def _yes(question: str) -> bool:
    ans = _ask(question + " [y/N] ")
    return bool(ans) and ans.lower() in ("y", "yes")


def _press_enter() -> None:
    _ask("Press Enter to close ")


def _context(env: Mapping[str, str]) -> dict:
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _in_herdr(env: Mapping[str, str]) -> bool:
    return bool(env.get("HERDR_PLUGIN_ID") or env.get("HERDR_PLUGIN_ROOT"))


def describe_error(exc: BaseException) -> str:
    """One secret-free line: an OSError's path and errno name, else the exception class (never its message)."""
    if isinstance(exc, OSError):
        code = errno.errorcode.get(exc.errno or 0) or exc.__class__.__name__
        name = exc.filename if isinstance(exc.filename, str) else None
        return f"could not access {name} ({code})" if name else f"a file operation failed ({code})"
    return f"unexpected error ({exc.__class__.__name__})"


def _special_state_files(paths: Paths) -> list[str]:
    """State files that are not regular files; opening one could block, so actions refuse."""
    cands = [paths.settings, paths.settings_last_good, paths.control, paths.clock, paths.server_epoch,
             paths.dispatch, paths.salt, paths.setup_record, paths.log_file]
    dirs = [paths.locks_dir]
    for p in PROVIDERS:
        try:
            dirs += [os.path.join(paths.provider_dir(p), b) for b in os.listdir(paths.provider_dir(p))]
        except OSError:
            pass
    for d in dirs:
        try:
            cands += [os.path.join(d, n) for n in os.listdir(d)]
        except OSError:
            pass
    return [c for c in cands if special_file(c)]


def _clear_pycache(paths: Paths) -> None:
    """Best-effort clear of the bytecode cache once per server start, to keep it bounded."""
    import shutil

    src = paths.pycache_dir
    if not os.path.isdir(src) or os.path.islink(src):
        return
    trash = f"{src}.old-{os.getpid()}"
    try:
        os.rename(src, trash)  # running ticks see either the whole tree or none
    except OSError:
        return
    shutil.rmtree(trash, ignore_errors=True)


def cmd_toggle(provider: str, enabled: bool, env: Mapping[str, str]) -> int:
    paths = _paths(env)
    name = DISPLAY_NAMES[provider]
    special = _special_state_files(paths)
    if special:
        _out(f"QuotaRibbon: {name} not changed: {special[0]} is not a regular file; "
             "delete it and run this action again")
        return 1
    paths.ensure_private_dirs()
    res = _core("settings").set_enabled(paths, provider, enabled)
    if res.error:
        _out(f"QuotaRibbon: {name} not changed: {res.error}")
        if "settings.json" in res.error:
            hint = ("new usage requests stay stopped until it is fixed" if not enabled else "")
            _out(f"To recover: fix or delete {paths.settings} (deleting it restores the defaults), "
                 f"then run this action again{'; ' + hint if hint else ''}.")
        return 1
    state = "ON" if res.enabled else "OFF"
    note = "" if res.changed else " (unchanged)"
    _out(f"QuotaRibbon: {name} {state}{note}")
    return 0


def _fmt_seconds(n: int) -> str:
    return f"{n // 60} min" if n % 60 == 0 else f"{n} s"


def cmd_interval(seconds: int, env: Mapping[str, str]) -> int:
    paths = _paths(env)
    special = _special_state_files(paths)
    if special:
        _out(f"QuotaRibbon: refresh interval not changed: {special[0]} is not a regular file; "
             "delete it and run this action again")
        return 1
    paths.ensure_private_dirs()
    settings_mod = _core("settings")
    try:
        res = settings_mod.set_interval(paths, seconds)
    except ValueError as exc:
        raise UsageError(f"quotaribbon interval: {exc}") from None
    if res.error:
        _out(f"QuotaRibbon: refresh interval not changed: {res.error}")
        _out(f"To recover: fix or delete {paths.settings} (deleting it restores the defaults), "
             "then run this action again.")
        return 1
    note = "" if res.changed else " (unchanged)"
    _out(f"QuotaRibbon: refresh every {_fmt_seconds(res.interval_seconds)}{note}; values turn stale after "
         f"{_fmt_seconds(res.stale_after_seconds)} and are hidden after {_fmt_seconds(res.hide_values_after_seconds)}")
    if res.timings_adjusted:
        _out("Your custom stale/hide times did not fit the new interval, so they were reset to match it.")
    return 0


def _parse_interval_arg(args: Sequence[str]) -> int:
    if len(args) != 1:
        raise UsageError("quotaribbon interval: expected one argument, the interval in seconds")
    from .constants import INTERVAL_MAX, INTERVAL_MIN

    raw = args[0]
    if not (raw.isascii() and raw.isdigit()):
        raise UsageError("quotaribbon interval: the interval must be a whole number of seconds")
    seconds = int(raw, 10)
    if not INTERVAL_MIN <= seconds <= INTERVAL_MAX:
        raise UsageError(f"quotaribbon interval: the interval must be {INTERVAL_MIN}..{INTERVAL_MAX} seconds")
    return seconds


def cmd_interval_ui(env: Mapping[str, str]) -> int:
    return _popup("set refresh interval", lambda: _interval_ui(env))


def _interval_ui(env: Mapping[str, str]) -> int:
    from .constants import DEFAULT_INTERVAL, INTERVAL_CHOICES

    paths = _paths(env)
    loaded = _core("settings").load(paths, update_last_good=False)
    _out("QuotaRibbon refresh interval")
    _out("============================")
    if loaded.settings is not None:
        _out(f"Current: every {_fmt_seconds(loaded.settings.interval_seconds)}")
    for err in loaded.errors:
        _out(f"note: {err}")
    _out("")
    for i, n in enumerate(INTERVAL_CHOICES, 1):
        default = " (default)" if n == DEFAULT_INTERVAL else ""
        _out(f"  {i}) every {_fmt_seconds(n)}{default}")
    _out("")
    _out("Shorter intervals use a little more CPU.")
    ans = _ask(f"Choose 1-{len(INTERVAL_CHOICES)} (Enter to cancel): ")
    if not ans:
        _out("Nothing changed.")
        _press_enter()
        return 0
    if not (ans.isascii() and ans.isdigit()) or not 1 <= int(ans) <= len(INTERVAL_CHOICES):
        _out(f"{ans[:20]!r} is not one of the choices. Nothing changed.")
        _press_enter()
        return 1
    rc = cmd_interval(INTERVAL_CHOICES[int(ans) - 1], env)
    _press_enter()
    return rc


def cmd_refresh(env: Mapping[str, str], *, now_fn=time.time) -> int:
    paths = _paths(env)
    special = _special_state_files(paths)
    if special:
        _out(f"refresh: nothing collected: {special[0]} is not a regular file; delete it and refresh again")
        return 1
    paths.ensure_private_dirs()
    settings_mod = _core("settings")
    loaded = settings_mod.load(paths, update_last_good=True)
    settings = loaded.settings
    if settings is None:
        _out("refresh: settings.json is invalid and no last good copy exists; nothing collected")
        return 1
    binding = _core("binding")
    bindings = {p: binding.binding_for(p, settings, env) for p in PROVIDERS}
    view = _core("cache").load_view(paths, bindings)
    ctx_raw = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    manual = bool(_core("scheduler").is_manual_invocation(ctx_raw, view, now_fn()))
    lease_id = None
    if not manual:
        cid = _context(env).get("correlation_id")
        lease_id = cid if isinstance(cid, str) else None
    from .connection import LiveHostAdapter

    host = LiveHostAdapter(paths, env, settings)
    # refresh.run installs the SIGTERM/SIGINT handling (terminate provider groups, exit).
    report = _core("refresh").run(paths, host, manual=manual, lease_id=lease_id, now_fn=now_fn)
    parts = []
    for o in report.outcomes:
        detail = f"({o.detail})" if o.detail else ""
        parts.append(f"{o.provider}={o.action}{detail}")
    _out(f"refresh ({'manual' if manual else 'scheduled'}): " + (" ".join(parts) if parts else "nothing to do"))
    return 0


def cmd_startup(env: Mapping[str, str]) -> int:
    """Rotate the server epoch and adopt valid settings; restart detection does not rely on this hook."""
    paths = _paths(env)
    special = _special_state_files(paths)
    if special:
        _out(f"startup: skipped: {special[0]} is not a regular file; delete it")
        return 1
    paths.ensure_private_dirs()
    _clear_pycache(paths)
    cache = _core("cache")
    settings_mod = _core("settings")
    cache.rotate_server_epoch(paths)
    try:
        loaded = settings_mod.load(paths, update_last_good=True)
        if loaded.settings is not None and loaded.valid:
            with file_lock(paths.settings_lock, blocking=True):
                cache.sync_fingerprints(paths, settings_mod.settings_fingerprints(loaded.settings))
    except Exception:  # noqa: BLE001
        pass
    _out("startup: server epoch rotated")
    return 0


def cmd_tabbar_preview(env: Mapping[str, str]) -> int:
    paths = _paths(env)
    cfg = {"plugin_id": PLUGIN_ID, "config_dir": paths.config_dir, "state_dir": paths.state_dir}
    line = _core("tabbar_entry").render_line(cfg, env)
    _out(line)
    return 0


def cmd_diagnostics(env: Mapping[str, str], *, open_pane: bool = True) -> int:
    diagnostics = _core("diagnostics")
    paths = _paths(env)
    sys.stdout.write(diagnostics.format_report(diagnostics.build_report(paths, env)))
    sys.stdout.flush()
    if open_pane and _in_herdr(env):
        ok, err = _core("herdr_bridge").open_plugin_pane("diagnostics-ui", dict(env))
        _out("diagnostics: opened the diagnostics pane" if ok else f"diagnostics: pane not opened ({err})")
    return 0


def cmd_diagnostics_ui(env: Mapping[str, str]) -> int:
    cmd_diagnostics(env, open_pane=False)
    _press_enter()
    return 0


def check_python(executable: str | None = None, version: tuple[int, ...] | None = None) -> tuple[str | None, str | None]:
    """``(path, None)`` for a usable tab-bar interpreter, else ``(None, reason)``.

    ``path`` is the realpath, or a stable Homebrew ``opt`` path to the same interpreter
    (see ``stable_python_path``). The tab bar runs in the pane's cwd, so a shim (which picks the version from it) is refused.
    """
    exe = executable or sys.executable
    version = version or tuple(sys.version_info[:3])
    if not exe or not os.path.isabs(exe):
        return None, "the running Python has no absolute executable path"
    real = os.path.realpath(exe)
    if tuple(version[:2]) < MIN_PYTHON:
        return None, f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required"
    if any(m in exe or m in real for m in SHIM_MARKERS):
        return None, "the interpreter is a version-manager shim; run setup with the real interpreter path"
    try:
        if read_regular(real, 2)[:2] == b"#!":
            return None, "the interpreter path is a script (a shim?), not a Python binary"
    except OSError:
        return None, "the interpreter cannot be read"
    if not os.access(real, os.X_OK):
        return None, "the interpreter is not executable"
    if not _core("trust").trusted_executable(real):
        return None, ("the interpreter (or a directory above it) can be modified by another user; "
                      "the launcher would refuse it")
    return stable_python_path(real), None


# A Homebrew interpreter inside its versioned keg, e.g.
# <prefix>/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin/python3.14
_HOMEBREW_KEG_PYTHON_RE = re.compile(
    r"(?P<prefix>/.+)/Cellar/(?P<formula>python@3\.\d+)/[^/]+/"
    r"(?:Frameworks/Python\.framework/Versions/3\.\d+/)?bin/(?P<name>python3(?:\.\d+)?)"
)


def stable_python_path(real: str) -> str:
    """A path to interpreter ``real`` that survives a Homebrew upgrade, else ``real``.

    A keg path (``Cellar/python@3.x/<version>/...``) disappears after ``brew upgrade`` and
    ``brew cleanup``; Homebrew's ``opt/python@3.x/bin/python3.x`` symlink follows the formula.
    It is used only when it resolves to ``real`` and passes the same trust checks.
    """
    m = _HOMEBREW_KEG_PYTHON_RE.fullmatch(real)
    if not m:
        return real
    candidate = f"{m['prefix']}/opt/{m['formula']}/bin/{m['name']}"
    try:
        if os.path.realpath(candidate) != real:
            return real
    except (OSError, ValueError):
        return real
    if any(marker in candidate for marker in SHIM_MARKERS):
        return real
    if not _core("trust").trusted_executable(candidate):
        return real
    return candidate


def _cli_version(provider: str, profile: str, paths: Paths, settings, env: Mapping[str, str]) -> str | None:
    """Version the last refresh recorded for this binding, or None.

    Never probe the CLI here: it would bypass refresh's admission and process cleanup.
    """
    binding = _core("binding")
    cache = _core("cache")
    try:
        ps = settings.providers[provider] if settings is not None else None
    except (KeyError, AttributeError):
        ps = None
    try:
        b = binding.make_binding(provider, profile, binding.herdr_config_path(env),
                                 namespace=binding.credential_namespace(provider, ps, env))
        rec = cache.read_connection(paths, provider, b.binding_key)
    except Exception:  # noqa: BLE001
        return None
    return rec.cli_version if rec is not None else None


class SetupContext:

    def __init__(self, paths: Paths, env: Mapping[str, str], config_path: str) -> None:
        sc = _core("setup_config")
        self.paths = paths
        self.env = env
        self.config_path = config_path
        self.plugin_root = _plugin_root()
        self.python, self.python_problem = check_python()
        self.tabbar_path = paths.tabbar_py
        self.entry = sc.build_entry(self.python or os.path.realpath(sys.executable), self.tabbar_path)
        if self.python_problem:
            self.plan = None
        else:
            self.plan = sc.plan_setup(config_path, self.entry, tabbar_path=self.tabbar_path)
        self.cli: dict[str, str | None] = {}
        self._settings = None
        try:
            self._settings = _core("settings").load(paths, update_last_good=False).settings
        except Exception:  # noqa: BLE001
            self._settings = None

    def resolve_clis(self, with_versions: bool = True) -> dict[str, tuple[str | None, str | None, str | None]]:
        """``{provider: (cli_path, version, profile_dir)}``, resolved as refresh does."""
        from .connection import LiveHostAdapter
        from .types import ProviderSettings

        host = LiveHostAdapter(self.paths, self.env, self._settings)
        out = {}
        for p in PROVIDERS:
            ps = None
            try:
                ps = self._settings.providers[p] if self._settings is not None else None
            except (KeyError, AttributeError):
                ps = None
            ps = ps or ProviderSettings(enabled=True)
            exe = host.resolve_executable(p, ps)
            try:
                prof = host.resolve_profile(p, ps)
            except Exception:  # noqa: BLE001
                prof = None
            ver = (_cli_version(p, prof, self.paths, self._settings, self.env)
                   if (exe and prof and with_versions) else None)
            out[p] = (exe, ver, prof)
            self.cli[p] = exe
        return out


def _print_preview(ctx: SetupContext, *, with_versions: bool = True) -> None:
    bridge = _core("herdr_bridge")
    _out("QuotaRibbon setup")
    _out("=================")
    _out(f"Herdr:          {bridge.herdr_version(dict(ctx.env)) or 'unknown'}")
    _out(f"Herdr config:   {ctx.config_path}")
    _out(f"Python:         {ctx.python or '-'} ({sys.version.split()[0]})")
    if ctx.python_problem:
        _out(f"  refused: {ctx.python_problem}")
    _out(f"Plugin root:    {ctx.plugin_root}")
    _out(f"Bootstrap:      {ctx.tabbar_path}")
    try:
        for p, (exe, ver, prof) in ctx.resolve_clis(with_versions).items():
            _out(f"{DISPLAY_NAMES[p] + ':':<16}{exe or 'not found'}{(' (' + ver + ')') if ver else ''}")
            _out(f"{'':<16}profile {prof or '-'}")
    except Exception as exc:  # noqa: BLE001
        _out(f"CLIs:           could not resolve ({exc.__class__.__name__})")
    plan = ctx.plan
    if plan is not None:
        ui = plan.ui_info or {}
        types = [i.get("type") + (" (QuotaRibbon)" if i.get("quotaribbon") else "") for i in ui.get("tab_bar_right", [])]
        _out(f"tab_bar_right:  {', '.join(types) if types else '(none)'}")
        _out(f"hide_tab_bar_when_single_tab: {ui.get('hide_tab_bar_when_single_tab')}")
        _out(f"tab_bar_position: {ui.get('tab_bar_position') or 'default'}")
        for w in ui.get("warnings", []):
            _out(f"note: {w}")
    _out("Display defaults: Claude ON, Codex ON (saved settings are never overwritten)")
    _out("")
    if plan is None:
        return
    _out(f"Plan: {plan.reason}")
    if plan.diff:
        _out("")
        sys.stdout.write(plan.diff)
    if plan.refusal:
        _out("")
        _out("Automatic setup refused. Manual setup:")
        sys.stdout.write(plan.manual_snippet)
    _out("")
    _out("Note: the file is re-checked right before it is replaced and a backup is kept, but an editor "
         "writing config.toml at the very same moment cannot be detected. Close other editors first.")
    sys.stdout.flush()


def _install_bootstrap(ctx: SetupContext) -> None:
    paths = ctx.paths
    paths.ensure_private_dirs()
    src = os.path.join(ctx.plugin_root, "bootstrap", "tabbar.py")
    with os.fdopen(open_regular(src), "rb") as fh:
        data = fh.read()
    atomic_write_bytes(paths.tabbar_py, data, 0o600)
    _core("setup_config").install_wrapper(ctx.python, paths.tabbar_py)
    atomic_write_json(paths.bootstrap_json, {
        "schema_version": 1,
        "bootstrap_version": BOOTSTRAP_API,
        "plugin_id": PLUGIN_ID,
        "state_dir": paths.state_dir,
        "config_dir": paths.config_dir,
        "python": ctx.python,
    })


def _write_default_settings(paths: Paths) -> bool:
    """Write default settings only when settings.json does not exist (never flips OFF→ON)."""
    settings_mod = _core("settings")
    with file_lock(paths.settings_lock, blocking=True):
        if os.path.lexists(paths.settings):
            return False
        atomic_write_json(paths.settings, settings_mod.settings_to_file_json(settings_mod.DEFAULT_SETTINGS))
        return True


def pin_cli_paths(paths: Paths, pins: Mapping[str, str]) -> str | None:
    """Record absolute CLI paths as ``binary`` in settings.json; return an error or None."""
    settings_mod = _core("settings")
    with file_lock(paths.settings_lock, blocking=True):
        try:
            obj = read_json(paths.settings)
        except CorruptFile:
            return "settings.json is unreadable; not changed"
        if not isinstance(obj, dict):
            return "settings.json is missing; not changed"
        new = json.loads(json.dumps(obj))
        for p, exe in pins.items():
            if exe and os.path.isabs(exe):
                new.setdefault("providers", {}).setdefault(p, {})["binary"] = exe
        try:
            settings_mod.parse_settings(new)
        except ValueError as exc:
            return f"settings.json is invalid; not changed ({exc})"
        atomic_write_json(paths.settings, new)
    return None


def _config_unchanged(sc, real_path: str, before_hash: str | None) -> bool | None:
    """Whether the config file still has the previewed content (None: cannot tell)."""
    try:
        return sc._current_hash(real_path) == before_hash
    except OSError:
        return None


def _config_state_note(sc, plan) -> str:
    unchanged = _config_unchanged(sc, plan.real_path, plan.before_hash)
    if unchanged is None:
        return f"could not check whether {plan.config_path} was changed; open it to check"
    return f"{plan.config_path} was not changed" if unchanged else f"{plan.config_path} was changed"


def _apply_setup(ctx: SetupContext, *, pin: bool) -> int:
    sc = _core("setup_config")
    plan = ctx.plan
    paths = ctx.paths
    written = False
    try:
        with file_lock(paths.setup_lock):
            _install_bootstrap(ctx)
            if _write_default_settings(paths):
                _out("Wrote default settings.json (Claude ON, Codex ON).")
            if pin:
                err = pin_cli_paths(paths, ctx.cli)
                _out(f"CLI paths not pinned: {err}" if err else "Pinned the CLI paths in settings.json.")
            result = sc.apply_plan(plan, paths.backups_dir)
            written = result.written
            if result.written:
                atomic_write_json(paths.setup_record, sc.setup_record(plan))
                _out(f"Updated {plan.config_path}" + (f" (backup: {result.backup_path})" if result.backup_path else ""))
                _out("Change written:")
                sys.stdout.write(plan.diff)
            else:
                if read_json_safe(paths.setup_record) is None:
                    rec = sc.setup_record(plan)
                    rec.update({"created_key": False, "created_ui_header": False, "created_file": False,
                                "edits": [], "before_hash": None})
                    atomic_write_json(paths.setup_record, rec)
                _out("The QuotaRibbon entry is already present; config.toml was not changed.")
    except LockBusy:
        _out("setup: another setup or detach is running; try again")
        return 1
    except sc.ConcurrentEdit as exc:
        _out(f"setup refused: {exc}")
        return 1
    except sc.SetupRefused as exc:
        _out(f"setup refused: {exc}")
        return 1
    except OSError as exc:
        if written:
            _out(f"setup: {plan.config_path} was updated, but the setup record could not be saved "
                 f"({describe_error(exc)}). Run detach to remove the entry, or fix the problem and run setup again.")
        else:
            _out(f"setup failed: {describe_error(exc)}; {_config_state_note(sc, plan)}. "
                 "Fix the problem and run setup again.")
        return 1
    return 0


def read_json_safe(path: str):
    try:
        return read_json(path)
    except CorruptFile:
        return None


def _reload_and_check(ctx: SetupContext, *, interactive: bool) -> int:
    status, diags = _core("herdr_bridge").reload_config(dict(ctx.env))
    if status == "applied":
        _out("Herdr reloaded the config (applied).")
        return 0
    if status == "unreachable":
        _out("Herdr server not reachable; run `herdr server reload-config` later.")
        return 0
    _out(f"Herdr config reload status: {status}")
    if diags:
        # Herdr's diagnostics can quote config lines; never echo them here.
        _out(f"  Herdr reported {len(diags)} diagnostic(s); run `herdr server reload-config` to see them.")
    rec = read_json_safe(ctx.paths.setup_record)
    if interactive and rec and _yes("Revert the QuotaRibbon entry?"):
        try:
            res = _core("setup_config").revert_setup(ctx.config_path, rec, ctx.paths.backups_dir,
                                                     ctx.paths.tabbar_py)
            if res.written:
                os.unlink(ctx.paths.setup_record)
            _out("Reverted the QuotaRibbon entry.")
        except Exception as exc:  # noqa: BLE001
            _out(f"Revert failed: {exc}")
    elif not interactive:
        _out(f"To undo: {shlex.quote(os.path.join(ctx.plugin_root, 'bin', 'quotaribbon'))} detach "
             f"--herdr-config {shlex.quote(ctx.config_path)} --yes")
    return 1


def cmd_setup_noninteractive(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    paths = _paths(env)
    ctx = SetupContext(paths, env, os.path.abspath(args.herdr_config))
    _print_preview(ctx, with_versions=not args.dry_run)
    if ctx.python_problem:
        return 1
    if ctx.plan.refusal:
        return 1
    if not args.yes:
        _out("Dry run: nothing was changed. Re-run with --yes to apply.")
        return 0
    rc = _apply_setup(ctx, pin=args.pin_cli)
    if rc != 0 or args.no_reload or ctx.plan.already_present:
        return rc
    return _reload_and_check(ctx, interactive=False)


def _popup(verb: str, body) -> int:
    """Run a pane flow: a failure prints one line (never a traceback) and waits for Enter."""
    try:
        return body()
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - never a traceback in a pane
        _out("")
        _out(f"{verb} failed: {describe_error(exc)}. Fix the problem and run {verb} again; "
             "QuotaRibbon: Diagnostics shows the current state.")
        _press_enter()
        return 1


def cmd_setup_ui(env: Mapping[str, str]) -> int:
    return _popup("setup", lambda: _setup_ui(env))


def _setup_ui(env: Mapping[str, str]) -> int:
    paths = _paths(env)
    config_path = _core("herdr_bridge").resolve_config_path(dict(env))
    ctx = SetupContext(paths, env, config_path)
    _print_preview(ctx)
    if ctx.python_problem or ctx.plan.refusal:
        _press_enter()
        return 1
    if ctx.plan.already_present:
        _install_bootstrap(ctx)
        _out("Already set up. Refreshed the stable bootstrap copy.")
        _press_enter()
        return 0
    pins = {p: e for p, e in ctx.cli.items() if e}
    pin = False
    if pins:
        _out("Pinning records the CLI paths above in settings.json; otherwise they are looked up on every refresh.")
        pin = _yes("Pin these CLI paths?")
    if not _yes("Apply?"):
        _out("Nothing changed.")
        _press_enter()
        return 0
    rc = _apply_setup(ctx, pin=pin)
    if rc == 0 and _yes("Reload the Herdr config now (herdr server reload-config)?"):
        rc = _reload_and_check(ctx, interactive=True)
    _press_enter()
    return rc


def _open_pane_or_explain(entrypoint: str, verb: str, env: Mapping[str, str]) -> int:
    ok, err = _core("herdr_bridge").open_plugin_pane(entrypoint, dict(env))
    if ok:
        _out(f"{verb}: opened the {entrypoint} pane")
        return 0
    launcher = shlex.quote(os.path.join(_plugin_root(), "bin", "quotaribbon"))
    if verb == "interval":
        _out(f"{verb}: could not open the {entrypoint} pane ({err}). Run it from a terminal instead, e.g.:")
        _out(f"  {launcher} interval 300")
        return 1
    cfg = shlex.quote(_core("herdr_bridge").resolve_config_path(dict(env)))
    _out(f"{verb}: could not open the {entrypoint} pane ({err}). Run it from a terminal instead:")
    _out(f"  {launcher} {verb} --herdr-config {cfg} --dry-run")
    _out(f"  {launcher} {verb} --herdr-config {cfg} --yes")
    return 1


def _detach_plan(paths: Paths, config_path: str, force: bool):
    sc = _core("setup_config")
    rec = read_json_safe(paths.setup_record)
    rec = sc.validate_record(rec, config_path, paths.tabbar_py) if rec is not None else None
    expected = None
    boot = read_json_safe(paths.bootstrap_json)
    if isinstance(boot, dict) and isinstance(boot.get("python"), str):
        expected = sc.build_entry(boot["python"], paths.tabbar_py)
    return sc.plan_detach(config_path, rec, tabbar_path=paths.tabbar_py, expected_entry=expected, force=force)


def _print_detach(plan) -> None:
    _out("QuotaRibbon detach")
    _out("==================")
    _out(f"Herdr config: {plan.config_path}")
    _out(f"Plan: {plan.reason}")
    if plan.entry_diff:
        _out("The entry differs from the one setup added:")
        sys.stdout.write(plan.entry_diff)
    if plan.diff:
        _out("")
        sys.stdout.write(plan.diff)
    sys.stdout.flush()


def _apply_detach(paths: Paths, plan) -> int:
    sc = _core("setup_config")
    try:
        with file_lock(paths.setup_lock):
            res = sc.apply_detach(plan, paths.backups_dir)
    except LockBusy:
        _out("detach: another setup or detach is running; try again")
        return 1
    except (sc.ConcurrentEdit, sc.SetupRefused) as exc:
        _out(f"detach refused: {exc}")
        return 1
    except OSError as exc:
        _out(f"detach failed: {describe_error(exc)}; {_config_state_note(sc, plan)}. "
             "Fix the problem and run detach again.")
        return 1
    if res.written:
        try:
            os.unlink(paths.setup_record)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _out(f"detach: the entry was removed, but the setup record {paths.setup_record} could not be "
                 f"deleted ({describe_error(exc)}); delete it by hand.")
        count = getattr(plan, "removed_count", 1) or 1
        if res.deleted:
            what = "Removed the config file setup had created"
        elif count > 1:
            what = f"Removed all {count} copies of the QuotaRibbon entry"
        else:
            what = "Removed the QuotaRibbon entry"
        _out(f"{what} (backup: {res.backup_path}).")
    return 0


def cmd_detach_noninteractive(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    paths = _paths(env)
    plan = _detach_plan(paths, os.path.abspath(args.herdr_config), args.force)
    _print_detach(plan)
    if plan.refusal:
        _out(f"detach refused: {plan.refusal}")
        return 1
    if plan.nothing_to_do:
        return 0
    if not args.yes:
        _out("Dry run: nothing was changed. Re-run with --yes to apply.")
        return 0
    rc = _apply_detach(paths, plan)
    if rc == 0 and not args.no_reload:
        status, _d = _core("herdr_bridge").reload_config(dict(env))
        _out(f"Herdr config reload: {status}")
    return rc


def cmd_detach_ui(env: Mapping[str, str]) -> int:
    return _popup("detach", lambda: _detach_ui(env))


def _detach_ui(env: Mapping[str, str]) -> int:
    paths = _paths(env)
    config_path = _core("herdr_bridge").resolve_config_path(dict(env))
    plan = _detach_plan(paths, config_path, force=False)
    _print_detach(plan)
    if plan.refusal and plan.modified:
        if _yes("Remove the modified entry anyway?"):
            plan = _detach_plan(paths, config_path, force=True)
            _print_detach(plan)
        else:
            _out("Nothing changed.")
            _press_enter()
            return 0
    if plan.refusal:
        _out(f"detach refused: {plan.refusal}")
        _press_enter()
        return 1
    if plan.nothing_to_do:
        _press_enter()
        return 0
    if not _yes("Apply?"):
        _out("Nothing changed.")
        _press_enter()
        return 0
    rc = _apply_detach(paths, plan)
    if rc == 0 and _yes("Reload the Herdr config now (herdr server reload-config)?"):
        status, _d = _core("herdr_bridge").reload_config(dict(env))
        _out(f"Herdr config reload: {status}")
    _press_enter()
    return rc


def _parser(name: str, *, detach: bool) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"quotaribbon {name}", add_help=True)
    p.add_argument("--herdr-config", required=True, help="path of the Herdr config.toml to edit")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show the preview only")
    mode.add_argument("--yes", action="store_true", help="apply without asking")
    p.add_argument("--no-reload", action="store_true", help="do not run herdr server reload-config")
    if detach:
        p.add_argument("--force", action="store_true", help="remove our entry even if it was edited")
    else:
        p.add_argument("--pin-cli", action="store_true", help="record the resolved CLI paths in settings.json")
    return p


def _parse(name: str, args: Sequence[str], *, detach: bool) -> argparse.Namespace:
    parser = _parser(name, detach=detach)

    def error(message: str):
        raise UsageError(f"quotaribbon {name}: {message}")

    parser.error = error  # type: ignore[method-assign]
    return parser.parse_args(list(args))


def main(argv: Sequence[str], env: Mapping[str, str] | None = None) -> int:
    """Dispatch ``argv[0]``; returns a process exit code."""
    env = dict(os.environ if env is None else env)
    if not argv:
        print("quotaribbon: missing subcommand (expected one of: " + ", ".join(SUBCOMMANDS) + ")", file=sys.stderr)
        return 2
    command, args = argv[0], list(argv[1:])
    if command not in SUBCOMMANDS:
        print(f"quotaribbon: unknown subcommand {command!r}", file=sys.stderr)
        return 2
    try:
        if command in ("claude-on", "claude-off", "codex-on", "codex-off"):
            if args:
                raise UsageError(f"quotaribbon {command}: takes no arguments")
            provider, _, state = command.partition("-")
            return cmd_toggle(provider, state == "on", env)
        if command == "refresh":
            return cmd_refresh(env)
        if command == "startup":
            return cmd_startup(env)
        if command == "tabbar-preview":
            return cmd_tabbar_preview(env)
        if command == "diagnostics":
            return cmd_diagnostics(env)
        if command == "diagnostics-ui":
            return cmd_diagnostics_ui(env)
        if command == "setup":
            if not args:
                return _open_pane_or_explain("setup-ui", "setup", env)
            return cmd_setup_noninteractive(_parse("setup", args, detach=False), env)
        if command == "detach":
            if not args:
                return _open_pane_or_explain("detach-ui", "detach", env)
            return cmd_detach_noninteractive(_parse("detach", args, detach=True), env)
        if command == "interval":
            if not args:
                return _open_pane_or_explain("interval-ui", "interval", env)
            return cmd_interval(_parse_interval_arg(args), env)
        if command == "interval-ui":
            return cmd_interval_ui(env)
        if command == "setup-ui":
            return cmd_setup_ui(env)
        if command == "detach-ui":
            return cmd_detach_ui(env)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    except KeyboardInterrupt:
        try:
            _out("")
            _out(f"quotaribbon {command}: cancelled; nothing further was changed")
        except BaseException:  # noqa: BLE001 - a closed pane must not turn this into a traceback
            pass
        return 130
    except Exception as exc:  # noqa: BLE001 - never dump tracebacks into logs/panes
        try:
            logger = _logger(_paths(env))
            if logger is not None:
                logger.warning("cli %s failed: %s", command, exc.__class__.__name__)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(exc, OSError):
            hint = "check that this path exists and that you can write to it, then try again"
        else:
            hint = "run QuotaRibbon: Diagnostics and try again"
        print(f"quotaribbon {command}: failed: {describe_error(exc)}; {hint}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
