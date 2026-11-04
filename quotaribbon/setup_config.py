"""Surgical, fail-closed edits of the Herdr ``config.toml``.

Herdr drops the whole config when it does not parse or a tab-bar field is invalid, so
edits are minimal and textual, re-parsed, and refused on any doubt; the semantic diff
must be exactly our one entry. ``apply_*`` re-resolve and re-hash the file right before
writing; a backup is always kept.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import os
import re
import shlex
import stat
import tempfile
import time
import tomllib
from dataclasses import dataclass, field

from .fsutil import ensure_private_dir, open_regular

ENTRY_INTERVAL_SECONDS = 5
ENTRY_TIMEOUT_SECONDS = 2
ENTRY_KEYS = ("type", "command", "interval_seconds", "timeout_seconds")
MAX_CONFIG_BYTES = 1 << 20
_MISSING = object()

WRAPPER_NAME = "tabbar.sh"
# Below the entry's timeout_seconds, so a slow tick is cut by us, quietly, not by Herdr.
WRAPPER_DEADLINE_SECONDS = "1.8"

TAB_BAR_RIGHT_TYPES = ("zoom", "hostname", "datetime", "text", "command")
TAB_BAR_POSITIONS = ("top", "bottom")
_SHAPES = ("a", "b", "c", "d", "e")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class SetupError(Exception):
    """Base class for setup/detach failures."""


class SetupRefused(SetupError):
    """The plan carries a refusal."""


class ConcurrentEdit(SetupError):
    """The file changed between preview and apply; nothing was written."""


def toml_basic_string(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def wrapper_path(tabbar_path: str) -> str:
    return os.path.join(os.path.dirname(tabbar_path), WRAPPER_NAME)


def wrapper_script(python: str, tabbar_path: str) -> str:
    """``sh`` launcher running ``python -I tabbar_path`` under a watchdog; always exits 0, silently.

    Resolves nothing via the cwd or ``PATH``. Exported ``BASH_FUNC_*`` functions would
    shadow builtins: it ``unset -f``s the ones it uses and avoids ``[`` (not unsettable).
    """
    for value in (python, tabbar_path):
        if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
            raise ValueError("wrapper paths must be absolute")
    return (
        "# QuotaRibbon tab-bar launcher, written by `quotaribbon setup` (safe to regenerate).\n"
        "# Runs the bootstrap under a watchdog that also covers interpreter start-up and\n"
        "# imports, never writes to stderr and always exits 0.\n"
        "exec 2>/dev/null </dev/null\n"
        "unset -f cd command kill wait\n"
        "cd / || exit 0\n"
        f"qr_python={shlex.quote(python)}\n"
        f"qr_bootstrap={shlex.quote(tabbar_path)}\n"
        '"$qr_python" -I "$qr_bootstrap" &\n'
        "qr_pid=$!\n"
        "(\n"
        "  qr_sleep=\n"
        "  trap 'case $qr_sleep in ?*) kill \"$qr_sleep\" ;; esac; exit 0' TERM\n"
        f"  command -p sleep {WRAPPER_DEADLINE_SECONDS} &\n"
        "  qr_sleep=$!\n"
        '  wait "$qr_sleep" && kill -9 "$qr_pid"\n'
        ") >/dev/null &\n"
        "qr_watchdog=$!\n"
        'wait "$qr_pid"\n'
        'kill "$qr_watchdog"\n'
        "exit 0\n"
    )


def install_wrapper(python: str, tabbar_path: str) -> str:
    """Atomically write the launcher (mode 0600); return its path."""
    from .fsutil import atomic_write_bytes  # noqa: PLC0415

    path = wrapper_path(tabbar_path)
    atomic_write_bytes(path, wrapper_script(python, tabbar_path).encode("utf-8"), 0o600)
    return path


def build_command(python: str, tabbar_path: str) -> str:
    """Tab-bar command; ``2>/dev/null || :`` keeps even a missing launcher silent and at exit 0."""
    del python  # recorded in the launcher script
    return f"/bin/sh {shlex.quote(wrapper_path(tabbar_path))} 2>/dev/null || :"


def legacy_command(python: str, tabbar_path: str) -> str:
    """Pre-launcher command form (interpreter run directly), still recognised as ours."""
    return f"{shlex.quote(python)} -I {shlex.quote(tabbar_path)}"


def build_entry(python: str, tabbar_path: str) -> dict:
    """The ``tab_bar_right`` element QuotaRibbon adds, as ``tomllib`` would parse it."""
    return {
        "type": "command",
        "command": build_command(python, tabbar_path),
        "interval_seconds": ENTRY_INTERVAL_SECONDS,
        "timeout_seconds": ENTRY_TIMEOUT_SECONDS,
    }


def entry_text(entry: dict) -> str:
    return (
        "{ type = "
        + toml_basic_string(str(entry["type"]))
        + ", command = "
        + toml_basic_string(str(entry["command"]))
        + f", interval_seconds = {int(entry['interval_seconds'])}, timeout_seconds = {int(entry['timeout_seconds'])} }}"
    )


def manual_snippet(entry: dict) -> str:
    return (
        "Add this element to the ui.tab_bar_right array of your Herdr config.toml\n"
        "(create the [ui] table / the array if they do not exist yet):\n\n"
        "[ui]\n"
        "tab_bar_right = [\n"
        f"  {entry_text(entry)},\n"
        "]\n"
    )


def is_canonical_command(cmd: object, tabbar_path: str) -> bool:
    """``cmd`` is exactly our current or legacy command for ``tabbar_path``."""
    if not isinstance(cmd, str) or not tabbar_path:
        return False
    if cmd == build_command("", tabbar_path):
        return True
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    return (len(argv) == 3 and os.path.isabs(argv[0]) and argv[1:] == ["-I", tabbar_path]
            and cmd == legacy_command(argv[0], tabbar_path))


def is_our_entry(element: object, tabbar_path: str) -> bool:
    """Ours (other keys may be edited) only if ``command`` is exactly one of our forms.

    A command that merely mentions our paths belongs to the user and is never claimed.
    """
    return isinstance(element, dict) and is_canonical_command(element.get("command"), tabbar_path)


def is_canonical_entry(element: object, tabbar_path: str) -> bool:
    """``element`` is exactly an entry setup writes for ``tabbar_path``, default interval and timeout."""
    if not isinstance(element, dict) or not tabbar_path or set(element) != set(ENTRY_KEYS):
        return False
    if element.get("type") != "command":
        return False
    if type(element.get("interval_seconds")) is not int or element["interval_seconds"] != ENTRY_INTERVAL_SECONDS:
        return False
    if type(element.get("timeout_seconds")) is not int or element["timeout_seconds"] != ENTRY_TIMEOUT_SECONDS:
        return False
    return is_canonical_command(element.get("command"), tabbar_path)


@dataclass
class Stmt:
    kind: str                  # "table" | "array_table" | "kv"
    key: tuple[str, ...]       # header key, or key relative to ``table`` for kv
    quoted: bool               # any key part was a quoted string
    table: tuple[str, ...]     # table in effect (for headers: the header key)
    line_start: int            # offset of the line the statement starts on
    start: int                 # offset of the statement's first char
    end: int                   # offset just past the statement (before trailing ws/comment)
    eol: int                   # offset just past the terminating newline (or len(text))
    value_start: int | None = None


class _ScanError(ValueError):
    pass


def _skip_ws(t: str, i: int) -> int:
    n = len(t)
    while i < n and t[i] in " \t":
        i += 1
    return i


def _skip_ws_nl_comments(t: str, i: int) -> int:
    n = len(t)
    while i < n:
        c = t[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#":
            while i < n and t[i] != "\n":
                i += 1
        else:
            break
    return i


def _to_eol(t: str, i: int) -> int:
    """Skip trailing ws/comment and the newline; return offset of the next line."""
    n = len(t)
    i = _skip_ws(t, i)
    if i < n and t[i] == "#":
        while i < n and t[i] != "\n":
            i += 1
    if i < n and t[i] == "\r":
        i += 1
    if i < n and t[i] == "\n":
        i += 1
    return i


def _skip_string(t: str, i: int) -> int:
    n = len(t)
    if t.startswith('"""', i) or t.startswith("'''", i):
        q = t[i] * 3
        j = i + 3
        basic = t[i] == '"'
        while j < n:
            if basic and t[j] == "\\":
                j += 2
                continue
            if t.startswith(q, j):
                j += 3
                extra = 0
                while j < n and t[j] == q[0] and extra < 2:
                    j += 1
                    extra += 1
                return j
            j += 1
        raise _ScanError("unterminated multi-line string")
    q = t[i]
    j = i + 1
    while j < n:
        if q == '"' and t[j] == "\\":
            j += 2
            continue
        if t[j] == q:
            return j + 1
        if t[j] == "\n":
            break
        j += 1
    raise _ScanError("unterminated string")


def _parse_key(t: str, i: int) -> tuple[tuple[str, ...], bool, int]:
    parts: list[str] = []
    quoted = False
    n = len(t)
    while True:
        i = _skip_ws(t, i)
        if i >= n:
            raise _ScanError("unexpected end in key")
        c = t[i]
        if c in "\"'":
            j = _skip_string(t, i)
            raw = t[i + 1 : j - 1]
            parts.append(raw)
            quoted = True
            i = j
        else:
            j = i
            while j < n and (t[j].isalnum() or t[j] in "_-"):
                j += 1
            if j == i:
                raise _ScanError("empty key")
            parts.append(t[i:j])
            i = j
        i = _skip_ws(t, i)
        if i < n and t[i] == ".":
            i += 1
            continue
        return tuple(parts), quoted, i


def _skip_value(t: str, i: int) -> int:
    n = len(t)
    if i >= n:
        raise _ScanError("missing value")
    c = t[i]
    if c in "\"'":
        return _skip_string(t, i)
    if c == "[":
        return array_layout(t, i)[2] + 1
    if c == "{":
        j = i + 1
        while True:
            j = _skip_ws(t, j)
            if j < n and t[j] == "}":
                return j + 1
            _key, _q, j = _parse_key(t, j)
            if j >= n or t[j] != "=":
                raise _ScanError("expected '=' in inline table")
            j = _skip_ws(t, j + 1)
            j = _skip_value(t, j)
            j = _skip_ws(t, j)
            if j < n and t[j] == ",":
                j += 1
                continue
            if j < n and t[j] == "}":
                return j + 1
            raise _ScanError("bad inline table")
    j = i
    while j < n and t[j] not in ",]}#\r\n":
        j += 1
    while j > i and t[j - 1] in " \t":
        j -= 1
    if j == i:
        raise _ScanError("empty value")
    return j


def array_layout(t: str, i: int) -> tuple[list[tuple[int, int]], list[int | None], int]:
    """Layout of the array literal starting at ``t[i] == "["``.

    Returns ``(elements, commas, close)``: element spans, the offset of the comma that
    follows each element (None for a last element without a trailing comma), and the
    offset of the closing ``]``.
    """
    if t[i] != "[":
        raise _ScanError("not an array")
    n = len(t)
    elems: list[tuple[int, int]] = []
    commas: list[int | None] = []
    j = i + 1
    while True:
        j = _skip_ws_nl_comments(t, j)
        if j >= n:
            raise _ScanError("unterminated array")
        if t[j] == "]":
            return elems, commas, j
        s = j
        e = _skip_value(t, j)
        elems.append((s, e))
        j = _skip_ws_nl_comments(t, e)
        if j < n and t[j] == ",":
            commas.append(j)
            j += 1
            continue
        commas.append(None)
        j = _skip_ws_nl_comments(t, j)
        if j < n and t[j] == "]":
            return elems, commas, j
        raise _ScanError("expected ',' or ']' in array")


def scan(t: str) -> list[Stmt]:
    """Statements of a (valid) TOML document with their offsets."""
    out: list[Stmt] = []
    n = len(t)
    i = 0
    table: tuple[str, ...] = ()
    while i < n:
        line_start = i
        i = _skip_ws(t, i)
        if i >= n:
            break
        c = t[i]
        if c in "\r\n":
            i = _to_eol(t, i)
            continue
        if c == "#":
            i = _to_eol(t, i)
            continue
        if c == "[":
            arr = t.startswith("[[", i)
            key, quoted, j = _parse_key(t, i + (2 if arr else 1))
            close = "]]" if arr else "]"
            if not t.startswith(close, j):
                raise _ScanError("bad table header")
            end = j + len(close)
            eol = _to_eol(t, end)
            out.append(Stmt("array_table" if arr else "table", key, quoted, key, line_start, i, end, eol))
            table = key
            i = eol
            continue
        key, quoted, j = _parse_key(t, i)
        if j >= n or t[j] != "=":
            raise _ScanError("expected '='")
        vs = _skip_ws(t, j + 1)
        ve = _skip_value(t, vs)
        eol = _to_eol(t, ve)
        out.append(Stmt("kv", key, quoted, table, line_start, i, ve, eol, vs))
        i = eol
    return out


_WARN_SINGLE_TAB = ("ui.hide_tab_bar_when_single_tab = true: with a single tab the tab bar "
                    "(and QuotaRibbon) is hidden. QuotaRibbon does not change this.")
_WARN_SINGLE_TAB_INVALID = "ui.hide_tab_bar_when_single_tab is not true or false; Herdr may reject it."
_WARN_BOTTOM = ('ui.tab_bar_position = "bottom": QuotaRibbon keeps your position (it was verified '
                "on the default top tab bar).")
_WARN_POSITION_INVALID = ('ui.tab_bar_position is not "top" or "bottom"; Herdr may reject it. '
                          "QuotaRibbon does not change it.")
_WARN_TBR_NOT_ARRAY = "ui.tab_bar_right is not an array."


def inspect_ui(config: dict | None, tabbar_path: str | None = None) -> dict:
    """Tab-bar ``[ui]`` settings, reported only from a fixed vocabulary (config text is never echoed)."""
    ui = config.get("ui") if isinstance(config, dict) else None
    ui = ui if isinstance(ui, dict) else {}
    tbr = ui.get("tab_bar_right")
    items = []
    if isinstance(tbr, list):
        for el in tbr:
            t = el.get("type") if isinstance(el, dict) else None
            items.append({"type": t if isinstance(t, str) and t in TAB_BAR_RIGHT_TYPES else "other",
                          "quotaribbon": bool(tabbar_path) and is_our_entry(el, tabbar_path or "")})
    hide = ui.get("hide_tab_bar_when_single_tab", _MISSING)
    pos = ui.get("tab_bar_position", _MISSING)
    hide_v = None if hide is _MISSING else (hide if isinstance(hide, bool) else "invalid")
    pos_v = None if pos is _MISSING else (pos if isinstance(pos, str) and pos in TAB_BAR_POSITIONS else "invalid")
    warnings = []
    if hide_v is True:
        warnings.append(_WARN_SINGLE_TAB)
    elif hide_v == "invalid":
        warnings.append(_WARN_SINGLE_TAB_INVALID)
    if pos_v == "bottom":
        warnings.append(_WARN_BOTTOM)
    elif pos_v == "invalid":
        warnings.append(_WARN_POSITION_INVALID)
    if tbr is not None and not isinstance(tbr, list):
        warnings.append(_WARN_TBR_NOT_ARRAY)
    return {
        "tab_bar_right": items,
        "tab_bar_right_present": tbr is not None,
        "hide_tab_bar_when_single_tab": hide_v,
        "tab_bar_position": pos_v,
        "warnings": warnings,
    }


# Herdr's tab-bar schema: per element type, the accepted keys besides ``type``.
_ELEMENT_KEYS: dict[str, dict[str, type]] = {
    "zoom": {},
    "hostname": {},
    "datetime": {"format": str},
    "text": {"text": str},
    "command": {"command": str, "interval_seconds": int, "timeout_seconds": int},
}
# Keys without which an element of that type means nothing (refused fail-closed).
_ELEMENT_REQUIRED = {"text": ("text",), "command": ("command",)}
_ELEMENT_RANGES = {"interval_seconds": (1, 31_536_000), "timeout_seconds": (1, 3_600)}
# Whitelist: a refusal costs a manual edit, a format Herdr rejects drops the whole config.
_DATETIME_ALLOWED = frozenset("YCymbBhdeaAwuUWGgVjDxFvHkIlPpMSRTXrnt%")
_DATETIME_PADDING = frozenset("-_0")


def _datetime_format_ok(fmt: str) -> bool:
    i, n = 0, len(fmt)
    while i < n:
        if fmt[i] != "%":
            i += 1
            continue
        i += 1
        if i < n and fmt[i] in _DATETIME_PADDING:
            i += 1  # at most one padding modifier
        if i >= n:
            return False  # a dangling "%"
        if fmt[i] not in _DATETIME_ALLOWED or (fmt[i] == "%" and fmt[i - 1] != "%"):
            return False
        i += 1
    return True


def _element_problem(el: object, pos: int) -> str | None:
    where = f"ui.tab_bar_right element {pos}"
    if not isinstance(el, dict):
        return f"{where} is not a table"
    kind = el.get("type")
    if not isinstance(kind, str) or kind not in _ELEMENT_KEYS:
        return f"{where} has no type Herdr knows"
    allowed = _ELEMENT_KEYS[kind]
    if any(k != "type" and k not in allowed for k in el):
        return f"{where} has a key Herdr does not accept for its type"
    for k, want in allowed.items():
        if k in el and type(el[k]) is not want:  # bool is not an int here
            return f"{where} has a value of the wrong type"
    if any(k not in el for k in _ELEMENT_REQUIRED.get(kind, ())):
        return f"{where} is missing a required key for its type"
    for k, (lo, hi) in _ELEMENT_RANGES.items():
        if k in el and not lo <= el[k] <= hi:
            return f"{where} has a value outside the range Herdr accepts"
    if kind == "datetime" and "format" in el and not _datetime_format_ok(el["format"]):
        return f"{where} uses a datetime directive Herdr rejects"
    return None


def _ui_schema_problem(doc: object) -> str | None:
    if not isinstance(doc, dict) or "ui" not in doc:
        return None
    ui = doc["ui"]
    if not isinstance(ui, dict):
        return "ui is not a table"
    if "hide_tab_bar_when_single_tab" in ui and not isinstance(ui["hide_tab_bar_when_single_tab"], bool):
        return "ui.hide_tab_bar_when_single_tab is not true or false"
    if "tab_bar_position" in ui and not (isinstance(ui["tab_bar_position"], str)
                                          and ui["tab_bar_position"] in TAB_BAR_POSITIONS):
        return 'ui.tab_bar_position is not "top" or "bottom"'
    if "tab_bar_right_separator" in ui and not isinstance(ui["tab_bar_right_separator"], str):
        return "ui.tab_bar_right_separator is not a string"
    if "tab_bar_right" in ui:
        tbr = ui["tab_bar_right"]
        if not isinstance(tbr, list):
            return "ui.tab_bar_right is not an array"
        for pos, el in enumerate(tbr, 1):
            why = _element_problem(el, pos)
            if why:
                return why
    return None


def herdr_ui_problem(doc: object) -> str | None:
    """None when the ``[ui]`` tab-bar fields are valid for Herdr, else a refusal reason.

    Reasons are fixed sentences; config values never appear.
    """
    why = _ui_schema_problem(doc)
    return None if why is None else why + "; Herdr would reject this config, so it is not edited automatically"


def _candidate_problem(after_text: str) -> str | None:
    """Refusal reason when the edited text is invalid for Herdr; parse errors are left to
    :func:`verify_setup_semantics`."""
    try:
        doc = tomllib.loads(after_text)
    except tomllib.TOMLDecodeError:
        return None
    why = _ui_schema_problem(doc)
    return None if why is None else f"the edited file would not be valid for Herdr ({why}); nothing is written"


@dataclass
class SetupPlan:
    config_path: str
    real_path: str
    exists: bool
    before_text: str | None
    after_text: str | None
    diff: str
    shape: str | None
    reason: str
    refusal: str | None
    already_present: bool
    before_hash: str | None
    after_hash: str | None
    entry: dict
    entry_text: str
    edits: list[list] = field(default_factory=list)   # [[offset, inserted_text], ...] on before_text
    created_key: bool = False
    created_ui_header: bool = False
    manual_snippet: str = ""
    ui_info: dict = field(default_factory=dict)
    link_identity: tuple | None = None  # how config_path named real_path at preview


@dataclass
class DetachPlan:
    config_path: str
    real_path: str
    exists: bool
    before_text: str | None
    after_text: str | None        # None with delete_file=True means "remove the file we created"
    diff: str
    reason: str
    refusal: str | None
    nothing_to_do: bool
    modified: bool
    entry_diff: str
    before_hash: str | None
    after_hash: str | None
    delete_file: bool = False
    exact_restore: bool = False
    delete_entry: dict | None = None  # with delete_file: the only content the file may hold
    link_identity: tuple | None = None
    removed_count: int = 0        # QuotaRibbon entries the plan removes


@dataclass
class ApplyResult:
    written: bool
    backup_path: str | None
    after_hash: str | None
    deleted: bool = False


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _link_identity(config_path: str) -> tuple | None:
    """How ``config_path`` itself is linked; None when unknown (never equals a recorded identity)."""
    try:
        st = os.lstat(config_path)
    except FileNotFoundError:
        return ("absent",)
    except (OSError, ValueError):
        return None
    if not stat.S_ISLNK(st.st_mode):
        return ("entry", stat.S_IFMT(st.st_mode))
    try:
        target = os.readlink(config_path)
    except (OSError, ValueError):
        return None
    return ("symlink", st.st_dev, st.st_ino, target)


def _check_linkage(config_path: str, real_path: str, link_identity: tuple | None, done: str) -> None:
    """Raise ConcurrentEdit unless ``config_path`` still reaches ``real_path`` via the previewed link."""
    try:
        now_real = os.path.realpath(config_path)
    except (OSError, ValueError):
        now_real = None
    if now_real != real_path or link_identity is None or _link_identity(config_path) != link_identity:
        raise ConcurrentEdit(f"the config path no longer names the file shown in the preview; {done}")


def _read_config(config_path: str) -> tuple[str, bool, bytes | None, str | None]:
    """Return ``(real_path, exists, raw_bytes, problem)``; ``problem`` is a refusal reason."""
    real = os.path.realpath(config_path)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        if os.path.islink(config_path):
            return real, False, None, "the config path is a dangling symlink"
        return real, False, None, None
    except OSError as exc:
        return real, False, None, f"cannot stat the config file ({exc.__class__.__name__})"
    if not stat.S_ISREG(st.st_mode):
        return real, True, None, "the config path is not a regular file"
    if st.st_uid != os.geteuid():
        return real, True, None, "the config file is not owned by the current user"
    if st.st_size > MAX_CONFIG_BYTES:
        return real, True, None, "the config file is unexpectedly large"
    try:
        with os.fdopen(open_regular(real), "rb") as fh:
            data = fh.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        return real, True, None, f"cannot read the config file ({exc.__class__.__name__})"
    return real, True, data, None


def _decode(data: bytes) -> tuple[str | None, dict | None, str | None]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "the config file is not valid UTF-8"
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return text, None, f"the config file does not parse as TOML ({exc})"
    return text, doc, None


_STRUCTURAL_RE = re.compile(r"\s*(\[ui\]|tab_bar_right\s*=\s*\[?|[\[\],\s])*\s*")
MASK = "…"


def _comment_start(seg: str) -> int:
    """Offset of a ``#`` outside single-line strings in ``seg``, or -1 (display only)."""
    quote = None
    i = 0
    while i < len(seg):
        c = seg[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return i
        i += 1
    return -1


def _mask_segment(seg: str) -> str:
    """``seg`` with its content masked but its layout kept visible: indentation, a leading or
    trailing comma and the presence of a comment, so a moved comma or comment shows in the diff."""
    if _STRUCTURAL_RE.fullmatch(seg):
        return seg
    hash_at = _comment_start(seg)
    code, comment = (seg, "") if hash_at < 0 else (seg[:hash_at], seg[hash_at:])
    stripped = code.strip(" \t")
    indent = code[: len(code) - len(code.lstrip(" \t"))]
    parts = [indent]
    if stripped:
        if stripped.startswith(","):
            parts.append(", " if len(stripped) > 1 else ",")
        if stripped.strip(" \t,"):
            parts.append(MASK)
        if stripped.endswith(",") and len(stripped) > 1:
            parts.append(",")
    if comment:
        parts.append((" " if stripped else "") + "# " + MASK)
    return "".join(parts)


def _mask_line(line: str, ours: str | None) -> str:
    body = line.rstrip("\n")
    segments = body.split(ours) if ours else [body]
    return (ours or "").join(_mask_segment(seg) for seg in segments)


def _comments(t: str) -> list[str] | None:
    """Comment texts of TOML document ``t`` in order (``#`` to end of line, outside strings);
    None if a string is unterminated."""
    out: list[str] = []
    n = len(t)
    i = 0
    while i < n:
        c = t[i]
        if c == "#":
            j = t.find("\n", i)
            j = n if j < 0 else j
            out.append(t[i:j].rstrip("\r"))
            i = j
        elif c in "\"'":
            if t.startswith(c * 3, i):
                j = i + 3
                while True:
                    k = t.find(c * 3, j)
                    if k < 0:
                        return None
                    if c == '"':
                        b = k
                        while b > i + 3 and t[b - 1] == "\\":
                            b -= 1
                        if (k - b) % 2:
                            j = k + 1
                            continue
                    break
                e = k + 3
                while e < n and e - k - 3 < 2 and t[e] == c:  # up to two quotes before the closer
                    e += 1
                i = e
            else:
                j = i + 1
                while j < n and t[j] != c:
                    if t[j] == "\n":
                        return None
                    j += 2 if (c == '"' and t[j] == "\\") else 1
                if j >= n:
                    return None
                i = j + 1
        else:
            i += 1
    return out


_HUNK_HEADER = re.compile(r"@@ -\d+(,\d+)? \+\d+(,\d+)? @@")


def _udiff(before: str | None, after: str | None, path: str, ours: str | None = None) -> str:
    """Zero-context diff showing only our entry and TOML structure; anything else (other
    commands may hold credentials) is masked."""
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    out = []
    for idx, ln in enumerate(difflib.unified_diff(a, b, fromfile=path, tofile=path + " (after)", n=0)):
        # Only the two file headers and well-formed hunk headers pass through: a user line whose
        # content starts with "--", "++" or "@@" must still be masked.
        if idx < 2 or _HUNK_HEADER.fullmatch(ln.rstrip("\n")):
            out.append(ln if ln.endswith("\n") else ln + "\n")
        elif ln[:1] in "+- ":
            out.append(ln[0] + _mask_line(ln[1:], ours) + "\n")
    return "".join(out)


def _apply_edits(text: str, edits: list[list]) -> str:
    for offset, ins in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:offset] + ins + text[offset:]
    return text


def _expected_after_setup(doc: dict, entry: dict) -> dict:
    exp = copy.deepcopy(doc)
    ui = exp.setdefault("ui", {})
    ui.setdefault("tab_bar_right", []).append(copy.deepcopy(entry))
    return exp


def verify_setup_semantics(before_doc: dict, after_text: str, entry: dict) -> str | None:
    """None if ``after_text`` == ``before_doc`` plus exactly our appended entry, else a reason."""
    try:
        after_doc = tomllib.loads(after_text)
    except tomllib.TOMLDecodeError as exc:
        return f"the edited file would not parse ({exc})"
    if after_doc != _expected_after_setup(before_doc, entry):
        return "the edit would change more than ui.tab_bar_right"
    return None


def _analyze(text: str, doc: dict) -> tuple[str | None, str | None, list[Stmt]]:
    """Return ``(shape, refusal, stmts)``."""
    try:
        stmts = scan(text)
    except _ScanError as exc:
        return None, f"the config file uses TOML syntax this tool does not edit automatically ({exc})", []
    for s in stmts:
        eff = s.key if s.kind != "kv" else s.table + s.key
        if not eff or eff[0] != "ui":
            continue
        if s.quoted:
            return None, "quoted \"ui\" keys are not edited automatically", stmts
        if s.kind == "array_table" and len(s.key) <= 2:
            return None, "[[ui]] / [[ui.tab_bar_right]] array-of-tables are not edited automatically", stmts
        if s.kind == "kv" and s.table == ():
            return None, "root-level dotted ui.* keys or an inline ui = {...} table are not edited automatically", stmts
    if "ui" not in doc:
        return "b", None, stmts
    ui = doc["ui"]
    if not isinstance(ui, dict):
        return None, "ui is not a table", stmts
    headers = [s for s in stmts if s.kind == "table" and s.key == ("ui",)]
    if len(headers) > 1:
        return None, "the [ui] table is declared more than once", stmts
    if headers:
        tbr = ui.get("tab_bar_right", _MISSING)
        if tbr is _MISSING:
            return "c", None, stmts
        if not isinstance(tbr, list):
            return None, "ui.tab_bar_right is not an array", stmts
        kvs = [s for s in stmts if s.kind == "kv" and s.table == ("ui",) and s.key == ("tab_bar_right",)]
        if len(kvs) != 1 or kvs[0].value_start is None or text[kvs[0].value_start] != "[":
            return None, "ui.tab_bar_right is not written as a plain array literal under [ui]", stmts
        return "d", None, stmts
    if "tab_bar_right" in ui:
        return None, "ui.tab_bar_right is defined in an unsupported way", stmts
    if any(s.kind in ("table", "array_table") and len(s.key) > 1 and s.key[0] == "ui" for s in stmts):
        return "e", None, stmts
    return None, "the ui table is defined in an unsupported way", stmts


def _line_indent(t: str, pos: int) -> str:
    ls = t.rfind("\n", 0, pos) + 1
    j = ls
    while j < len(t) and t[j] in " \t":
        j += 1
    return t[ls:j]


def _plan_edits(text: str, shape: str, stmts: list[Stmt], etext: str) -> tuple[list[list], bool, bool]:
    """Edits for ``shape``; returns ``(edits, created_key, created_ui_header)``."""
    if shape in ("b", "e"):
        sep = ""
        if text and not text.endswith("\n"):
            sep = "\n"
        if text.strip():
            sep += "\n"
        return [[len(text), f"{sep}[ui]\ntab_bar_right = [{etext}]\n"]], True, True
    if shape == "c":
        header = next(s for s in stmts if s.kind == "table" and s.key == ("ui",))
        pre = "" if header.eol > header.end and text[header.eol - 1] == "\n" else "\n"
        return [[header.eol, f"{pre}tab_bar_right = [{etext}]\n"]], True, False
    # shape d
    kv = next(s for s in stmts if s.kind == "kv" and s.table == ("ui",) and s.key == ("tab_bar_right",))
    vs = kv.value_start
    elems, commas, close = array_layout(text, vs)
    open_line = text.count("\n", 0, vs)
    close_line = text.count("\n", 0, close)
    close_line_start = text.rfind("\n", 0, close) + 1
    close_alone = text[close_line_start:close].strip(" \t") == ""
    if close_line != open_line and close_alone:
        edits: list[list] = []
        if elems:
            indent = _line_indent(text, elems[-1][0])
            if commas[-1] is None:
                edits.append([elems[-1][1], ","])
        else:
            indent = _line_indent(text, vs) + "  "
        edits.append([close_line_start, f"{indent}{etext},\n"])
        return edits, False, False
    if not elems:
        return [[close, etext]], False, False
    if commas[-1] is not None:
        return [[close, f" {etext}" if text[close - 1] not in " \t" else etext]], False, False
    return [[elems[-1][1], f", {etext}"]], False, False


def plan_setup(config_path: str, entry: dict, *, tabbar_path: str | None = None) -> SetupPlan:
    """Preview the edit that adds ``entry`` to ``ui.tab_bar_right`` of ``config_path``."""
    etext = entry_text(entry)
    snippet = manual_snippet(entry)
    link = _link_identity(config_path)
    real, exists, data, problem = _read_config(config_path)

    def refuse(reason: str, before: str | None = None, ui_info: dict | None = None) -> SetupPlan:
        return SetupPlan(config_path, real, exists, before, None, "", None, reason, reason, False,
                         _sha(data), None, entry, etext, manual_snippet=snippet, ui_info=ui_info or {},
                         link_identity=link)

    if problem:
        return refuse(problem)
    if not exists:
        after = f"[ui]\ntab_bar_right = [{etext}]\n"
        why = verify_setup_semantics({}, after, entry) or _candidate_problem(after)
        if why:
            return refuse(why)
        return SetupPlan(config_path, real, False, None, after, _udiff(None, after, config_path, etext), "a",
                         "no config file yet: create one with only the QuotaRibbon tab-bar entry", None, False,
                         None, _sha(after.encode("utf-8")), entry, etext, [[0, after]], True, True, snippet,
                         inspect_ui({}, tabbar_path), link)
    text, doc, problem = _decode(data or b"")
    if problem or doc is None or text is None:
        return refuse(problem or "unreadable config", text)
    ui_info = inspect_ui(doc, tabbar_path)
    # Never edit, or report "already present" (inviting a reload) for, a file Herdr would reject.
    invalid = herdr_ui_problem(doc)
    if invalid:
        return refuse(invalid, text, ui_info)
    tbr = doc.get("ui", {}).get("tab_bar_right") if isinstance(doc.get("ui"), dict) else None
    if isinstance(tbr, list):
        if any(el == entry for el in tbr):
            return SetupPlan(config_path, real, True, text, text, "", None,
                             "the QuotaRibbon entry is already present; nothing to do", None, True,
                             _sha(data), _sha(data), entry, etext, manual_snippet=snippet, ui_info=ui_info,
                             link_identity=link)
        if tabbar_path and any(is_our_entry(el, tabbar_path) for el in tbr):
            return refuse("a different (edited or outdated) QuotaRibbon entry is already present; "
                          "run detach first (or edit it by hand)", text, ui_info)
    shape, refusal, stmts = _analyze(text, doc)
    if refusal or shape is None:
        return refuse(refusal or "unsupported config shape", text, ui_info)
    try:
        edits, created_key, created_header = _plan_edits(text, shape, stmts, etext)
    except (_ScanError, StopIteration, IndexError) as exc:
        return refuse(f"could not locate the edit position ({exc.__class__.__name__})", text, ui_info)
    after = _apply_edits(text, edits)
    why = verify_setup_semantics(doc, after, entry) or _candidate_problem(after)
    if why:
        return refuse(why, text, ui_info)
    reasons = {
        "b": "no ui table yet: append a [ui] table with tab_bar_right",
        "c": "[ui] has no tab_bar_right: add it right after the [ui] header",
        "d": "append one element to the existing ui.tab_bar_right array",
        "e": "ui is only defined via [ui.<sub>] tables: append a new [ui] table with tab_bar_right",
    }
    return SetupPlan(config_path, real, True, text, after, _udiff(text, after, config_path, etext), shape,
                     reasons[shape], None, False, _sha(data), _sha(after.encode("utf-8")), entry, etext,
                     edits, created_key, created_header, snippet, ui_info, link)


def setup_record(plan: SetupPlan, now: float | None = None) -> dict:
    """``setup.json`` content for an applied plan (used by detach)."""
    now = time.time() if now is None else now
    return {
        "schema_version": 1,
        "config_path": plan.config_path,
        "real_path": plan.real_path,
        "entry": plan.entry,
        "entry_text": plan.entry_text,
        "shape": plan.shape,
        "created_key": plan.created_key,
        "created_ui_header": plan.created_ui_header,
        "created_file": not plan.exists,
        "edits": plan.edits,
        "before_hash": plan.before_hash,
        "after_hash": plan.after_hash,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }


def _creation_text(etext: str) -> str:
    return f"[ui]\ntab_bar_right = [{etext}]\n"


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != set(ENTRY_KEYS):
        return False
    return (entry.get("type") == "command" and isinstance(entry.get("command"), str)
            and type(entry.get("interval_seconds")) is int and type(entry.get("timeout_seconds")) is int)


def _is_hash(v: object) -> bool:
    return isinstance(v, str) and _HASH_RE.match(v) is not None


def _shape_consistent(rec: dict, etext: str) -> bool:
    """Shape, ownership flags, edits and hashes describe one setup that could have happened."""
    shape = rec.get("shape")
    edits = rec["edits"]
    flags = (rec["created_file"], rec["created_key"], rec["created_ui_header"])
    before = rec.get("before_hash")
    if any(e[0] < 0 for e in edits) or len({e[0] for e in edits}) != len(edits):
        return False
    if shape is None:  # "already present" record: nothing of the file is ours but the entry
        return flags == (False, False, False) and edits == [] and before is None
    if shape not in _SHAPES:
        return False
    if shape == "a":
        created = _creation_text(etext)
        return (flags == (True, True, True) and edits == [[0, created]] and before is None
                and rec.get("after_hash") == _sha(created.encode("utf-8")))
    if not _is_hash(before) or flags[0]:
        return False
    if shape in ("b", "e"):
        tail = _creation_text(etext)
        return (flags == (False, True, True) and len(edits) == 1
                and edits[0][1] in (tail, "\n" + tail, "\n\n" + tail))
    if shape == "c":
        line = f"tab_bar_right = [{etext}]\n"
        return flags == (False, True, False) and len(edits) == 1 and edits[0][1] in (line, "\n" + line)
    # shape d: one insertion carrying the entry (plus separators), optionally one comma
    if flags != (False, False, False) or not 1 <= len(edits) <= 2:
        return False
    carriers = [e for e in edits if etext in e[1]]
    if len(carriers) != 1:
        return False
    rest = carriers[0][1].replace(etext, "", 1)
    if rest.strip(" \t,\n") != "":
        return False
    return all(e is carriers[0] or e[1] == "," for e in edits)


def validate_record(rec: object, config_path: str | None = None, tabbar_path: str | None = None) -> dict | None:
    """``rec`` if it is a self-consistent setup record (for ``config_path``), else None.

    The record is untrusted: with ``tabbar_path`` its entry must be canonical, so a
    record alone never establishes ownership of an arbitrary command.
    """
    if not isinstance(rec, dict) or rec.get("schema_version") != 1:
        return None
    entry = rec.get("entry")
    if not _valid_entry(entry):
        return None
    try:
        etext = entry_text(entry)
    except (KeyError, ValueError, TypeError):
        return None
    if rec.get("entry_text") != etext:
        return None
    if tabbar_path is not None and not is_canonical_entry(entry, tabbar_path):
        return None
    edits = rec.get("edits")
    if not isinstance(edits, list) or not all(
        isinstance(e, list) and len(e) == 2 and type(e[0]) is int and isinstance(e[1], str) for e in edits
    ):
        return None
    for k in ("created_key", "created_ui_header", "created_file"):
        if not isinstance(rec.get(k), bool):
            return None
    for k in ("config_path", "real_path"):
        v = rec.get(k)
        if not isinstance(v, str) or not os.path.isabs(v) or "\0" in v:
            return None
    if not _is_hash(rec.get("after_hash")):
        return None
    if not _shape_consistent(rec, etext):
        return None
    if config_path is not None:
        try:
            if os.path.realpath(config_path) != rec["real_path"]:
                return None
        except (OSError, ValueError):
            return None
    return rec


def _inverse_edits(text: str, edits: list[list]) -> str | None:
    """Undo insertions whose offsets refer to the *before* text; None if they do not match."""
    shift = 0
    out = text
    for offset, ins in sorted(edits, key=lambda e: e[0]):
        pos = offset + shift
        if out[pos : pos + len(ins)] != ins:
            return None
        shift += len(ins)
    shift = sum(len(ins) for _o, ins in edits)
    for offset, ins in sorted(edits, key=lambda e: e[0], reverse=True):
        shift -= len(ins)
        pos = offset + shift
        out = out[:pos] + out[pos + len(ins):]
    return out


def _delete_span(text: str, a: int, b: int) -> str:
    """Delete ``text[a:b]`` (on one line) and tidy that line: drop it if only whitespace is
    left, otherwise drop the whitespace the deletion left at its start or end. Never removes
    anything but whitespace beyond ``[a, b)``."""
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    le = len(text) if le < 0 else le
    left = text[ls:a]
    right = text[b:le]
    cr = right.endswith("\r")
    right_body = right[:-1] if cr else right
    if left.strip(" \t") == "" and right_body.strip(" \t") == "":
        return text[:ls] + text[min(le + 1, len(text)):]
    if left.strip(" \t") == "":
        return text[:a] + right.lstrip(" \t") + text[le:]
    if right_body.strip(" \t") == "":
        return text[:ls] + left.rstrip(" \t") + ("\r" if cr else "") + text[le:]
    return text[:a] + text[b:]


def _remove_element(text: str, vs: int, idx: int) -> str:
    """``text`` without element ``idx`` of the array at ``vs`` and one separating comma.

    Only our element, that comma and whitespace go; comments and other text stay.
    """
    elems, commas, _close = array_layout(text, vs)
    s, e = elems[idx]
    comma = commas[idx]
    line_start = text.rfind("\n", 0, s) + 1
    alone_before = text[line_start:s].strip(" \t") == ""
    if comma is not None:
        between = text[e:comma]
        j = _skip_ws(text, comma + 1)
        rest_blank = j >= len(text) or text[j] in "\r\n"
        if between.strip(" \t") == "":
            if alone_before and rest_blank:
                end = _to_eol(text, comma + 1)
                return text[:line_start] + text[end:]
            return text[:s] + text[j:]
        # A comment or line break sits between the element and its comma (leading-comma
        # style): remove the comma and the element separately.
        return _delete_span(_delete_span(text, comma, comma + 1), s, e)
    if idx > 0:
        j = _skip_ws(text, e)
        rest_blank = j >= len(text) or text[j] in "\r\n"
        pc = commas[idx - 1]
        if pc is None:
            raise _ScanError("missing separator")
        if alone_before and rest_blank:
            # "  prev,\n  ours\n]"  ->  "  prev\n]"
            return text[:pc] + text[pc + 1:line_start] + text[_to_eol(text, e):]
        if text[pc + 1:s].strip(" \t") == "":
            # "prev, ours]" -> "prev]";  "prev\n  , ours\n]" -> "prev\n]"
            return _delete_span(text, pc, e)
        # Text (a comment, a line break) between the separating comma and our element stays.
        return _delete_span(_delete_span(text, s, e), pc, pc + 1)
    return text[:s] + text[e:]


_EMPTY_KEY_LINE_RE = re.compile(r"[ \t]*tab_bar_right[ \t]*=[ \t]*\[[ \t]*\][ \t]*\r?\n?")
_BARE_UI_HEADER_RE = re.compile(r"[ \t]*\[[ \t]*ui[ \t]*\][ \t]*\r?\n?")


def _detach_structural(text: str, doc: dict, idxs: list[int], rec: dict | None) -> tuple[str | None, str | None]:
    """Remove elements ``idxs`` of ``ui.tab_bar_right`` textually; returns ``(after, refusal)``.

    Only those elements, their separating commas and whitespace are removed (plus the key and
    ``[ui]`` header setup created, when nothing else is left on their lines); any edit that
    would drop or move a comment is refused.
    """
    before_comments = _comments(text)
    if before_comments is None:
        return None, "could not read the comments of the config file; remove the entry by hand"
    try:
        stmts = scan(text)
        kvs = [s for s in stmts if s.kind == "kv" and s.table == ("ui",) and s.key == ("tab_bar_right",)
               and not s.quoted]
        if len(kvs) != 1 or text[kvs[0].value_start] != "[":
            return None, "ui.tab_bar_right is not a plain array literal under [ui]; remove the entry by hand"
        after = text
        exp = copy.deepcopy(doc)
        for idx in sorted(set(idxs), reverse=True):  # later elements first: earlier offsets stay valid
            after = _remove_element(after, kvs[0].value_start, idx)
            del exp["ui"]["tab_bar_right"][idx]
        created_key = bool(rec and rec.get("created_key"))
        created_header = bool(rec and rec.get("created_ui_header"))
        if created_key and exp["ui"]["tab_bar_right"] == []:
            st2 = scan(after)
            kv2 = next(s for s in st2 if s.kind == "kv" and s.table == ("ui",) and s.key == ("tab_bar_right",))
            if _EMPTY_KEY_LINE_RE.fullmatch(after[kv2.line_start:kv2.eol]):
                after = after[:kv2.line_start] + after[kv2.eol:]
                del exp["ui"]["tab_bar_right"]
                if created_header:
                    st3 = scan(after)
                    headers = [s for s in st3 if s.kind == "table" and s.key == ("ui",)]
                    body = [s for s in st3 if s.kind == "kv" and s.table == ("ui",)]
                    if (len(headers) == 1 and not body
                            and _BARE_UI_HEADER_RE.fullmatch(after[headers[0].line_start:headers[0].eol])):
                        h = headers[0]
                        cut_start = h.line_start
                        if after[:cut_start].endswith("\n\n"):
                            cut_start -= 1
                        after = after[:cut_start] + after[h.eol:]
                        if exp["ui"] == {}:
                            del exp["ui"]
    except (_ScanError, StopIteration, IndexError, KeyError) as exc:
        return None, f"could not locate the entry text ({exc.__class__.__name__}); remove it by hand"
    try:
        got = tomllib.loads(after)
    except tomllib.TOMLDecodeError:
        return None, "removing the entry would break the file; remove it by hand"
    if got != exp:
        return None, "removing the entry would change other settings; remove it by hand"
    if _comments(after) != before_comments:
        return None, "removing the entry would drop or move a comment; remove it by hand"
    return after, None


def _expected_after_removal(doc: dict, idx: int, created_key: bool, created_header: bool) -> dict:
    """``doc`` minus our element, and minus the key / ``ui`` table setup created if now empty."""
    exp = copy.deepcopy(doc)
    del exp["ui"]["tab_bar_right"][idx]
    if created_key and exp["ui"]["tab_bar_right"] == []:
        del exp["ui"]["tab_bar_right"]
        if created_header and exp["ui"] == {}:
            del exp["ui"]
    return exp


def is_minimal_created_config(text: str, entry: dict) -> bool:
    """``text`` is byte for byte the file setup creates from nothing for ``entry``."""
    try:
        etext = entry_text(entry)
        doc = tomllib.loads(text)
    except (KeyError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return False
    return doc == {"ui": {"tab_bar_right": [entry]}} and text == _creation_text(etext)


def plan_detach(config_path: str, recorded_entry: dict | None, *, tabbar_path: str | None = None,
                expected_entry: dict | None = None, force: bool = False) -> DetachPlan:
    """Preview removing **only** our tab-bar entry.

    ``recorded_entry`` (the untrusted setup record) is used only if it validates for this
    file. A modified entry of ours is refused unless ``force``. The file is deleted only
    when it is exactly the minimal config setup created.
    """
    # Without the trusted bootstrap path no recorded command can be proven ours.
    rec = (validate_record(recorded_entry, config_path, tabbar_path)
           if recorded_entry is not None and tabbar_path else None)
    if expected_entry is not None and not (tabbar_path and is_canonical_entry(expected_entry, tabbar_path)):
        expected_entry = None
    target = rec["entry"] if rec else expected_entry
    link = _link_identity(config_path)
    real, exists, data, problem = _read_config(config_path)

    def plan(**kw) -> DetachPlan:
        base = dict(config_path=config_path, real_path=real, exists=exists, before_text=None, after_text=None,
                    diff="", reason="", refusal=None, nothing_to_do=False, modified=False, entry_diff="",
                    before_hash=_sha(data), after_hash=None, link_identity=link)
        base.update(kw)
        return DetachPlan(**base)

    if problem:
        return plan(reason=problem, refusal=problem)
    if not exists:
        return plan(reason="no config file: nothing to remove", nothing_to_do=True)
    text, doc, problem = _decode(data or b"")
    if problem or doc is None or text is None:
        return plan(before_text=text, reason=problem or "unreadable", refusal=problem or "unreadable config")
    ui = doc.get("ui") if isinstance(doc.get("ui"), dict) else {}
    tbr = ui.get("tab_bar_right")
    nothing = "no QuotaRibbon entry found: nothing to remove"
    if not isinstance(tbr, list):
        return plan(before_text=text, reason=nothing, nothing_to_do=True)
    exact = [i for i, el in enumerate(tbr)
             if (target is not None and el == target) or (tabbar_path and is_canonical_entry(el, tabbar_path))]
    edited = [i for i, el in enumerate(tbr) if i not in exact and tabbar_path and is_our_entry(el, tabbar_path)]
    if not exact and not edited:
        return plan(before_text=text, reason=nothing, nothing_to_do=True)
    modified = bool(edited)
    entry_diff = ""
    if exact:
        target = tbr[exact[0]]  # equal to the target, or a canonical entry of ours
    if edited:
        entry_diff = _entry_diff(target, tbr[edited[0]])
        if not force:
            what = ("an edited copy of the QuotaRibbon entry is also present" if exact
                    else "the QuotaRibbon entry was modified after setup")
            return plan(before_text=text, reason=what,
                        refusal=f"{what}; review the diff and re-run with --force to remove it anyway",
                        modified=True, entry_diff=entry_diff)
    # Every copy of our entry goes, so nothing of ours is left behind once the record is deleted.
    idxs = sorted(exact + edited)
    count = len(idxs)
    try:
        ours_text = entry_text(target) if isinstance(target, dict) else None
    except (KeyError, ValueError, TypeError):
        ours_text = None
    # Exact restoration when the file is untouched since setup.
    if (rec and not modified and count == 1 and rec.get("after_hash") == _sha(data)
            and tbr and tbr[-1] == rec["entry"]):
        if rec.get("created_file"):
            if is_minimal_created_config(text, rec["entry"]):
                return plan(before_text=text, after_text=None, diff=_udiff(text, "", config_path, ours_text),
                            reason="the config file was created by setup and is unchanged: remove it",
                            delete_file=True, exact_restore=True, delete_entry=copy.deepcopy(rec["entry"]),
                            removed_count=1)
        else:
            restored = _inverse_edits(text, rec["edits"])
            if restored is not None and _sha(restored.encode("utf-8")) == rec.get("before_hash"):
                try:
                    got = tomllib.loads(restored)
                except tomllib.TOMLDecodeError:
                    got = None
                exp = _expected_after_removal(doc, len(tbr) - 1, rec["created_key"], rec["created_ui_header"])
                if got == exp:
                    return plan(before_text=text, after_text=restored,
                                diff=_udiff(text, restored, config_path, ours_text),
                                reason="restore the exact bytes from before setup", exact_restore=True,
                                after_hash=_sha(restored.encode("utf-8")), removed_count=1)
    after, refusal = _detach_structural(text, doc, idxs, rec)
    if refusal:
        return plan(before_text=text, reason=refusal, refusal=refusal, modified=modified, entry_diff=entry_diff)
    reason = ("remove only the QuotaRibbon entry" if count == 1
              else f"remove only the QuotaRibbon entries ({count} copies)")
    return plan(before_text=text, after_text=after, diff=_udiff(text, after, config_path, ours_text),
                reason=reason, modified=modified, entry_diff=entry_diff,
                after_hash=_sha(after.encode("utf-8")), removed_count=count)


def _entry_diff(recorded: dict | None, current: object) -> str:
    """Names of the edited fields of our entry, never their values."""
    rec = recorded if isinstance(recorded, dict) else {}
    cur = current if isinstance(current, dict) else {}
    keys = sorted(k for k in set(rec) | set(cur) if isinstance(k, str) and rec.get(k) != cur.get(k))
    shown = [k if k in ENTRY_KEYS else "(another key)" for k in keys]
    return "".join(f"  changed: {k}\n" for k in dict.fromkeys(shown)) or "  changed: (entry type)\n"


def _current_hash(real_path: str) -> str | None:
    try:
        with os.fdopen(open_regular(real_path), "rb") as fh:
            return _sha(fh.read(MAX_CONFIG_BYTES + 1))
    except FileNotFoundError:
        return None


def _check_owner(real_path: str) -> None:
    try:
        st = os.stat(real_path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise SetupRefused("the config file is not a regular file owned by the current user")


# Backups can hold other tab-bar commands: keep few.
MAX_BACKUPS = 5
_BACKUP_RE = re.compile(r"config\.toml\.[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")


def _backup(backup_dir: str, data: bytes) -> str:
    """Write a private backup; on error remove it (a partial backup would look valid)."""
    ensure_private_dir(backup_dir)
    t = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"
    path = os.path.join(backup_dir, f"config.toml.{stamp}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _discard_backup(path)
        raise
    return path


def _discard_backup(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _prune_backups(backup_dir: str, keep: str | None) -> None:
    """Keep the newest :data:`MAX_BACKUPS` backups, always including ``keep``; never raises."""
    try:
        names = sorted((n for n in os.listdir(backup_dir) if _BACKUP_RE.match(n)), reverse=True)
    except OSError:
        return
    keep_name = os.path.basename(keep) if keep else None
    kept = 1 if keep_name in names else 0
    for name in names:
        if name == keep_name:
            continue
        if kept < MAX_BACKUPS:
            kept += 1
            continue
        path = os.path.join(backup_dir, name)
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass


def _write_checked(plan: SetupPlan | DetachPlan, data: bytes, expected_hash: str | None, mode: int) -> None:
    """Atomic replace that re-hashes and re-resolves the target right before ``os.replace``."""
    real_path = plan.real_path
    directory = os.path.dirname(real_path)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.toml.", suffix=".qr-tmp", dir=directory)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if _current_hash(real_path) != expected_hash:
            raise ConcurrentEdit("the config file changed since the preview; nothing was written")
        _check_linkage(plan.config_path, real_path, plan.link_identity, _NOTHING_WRITTEN)
        os.replace(tmp, real_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _mode_of(real_path: str) -> int:
    try:
        return stat.S_IMODE(os.stat(real_path).st_mode)
    except FileNotFoundError:
        return 0o600


_NOTHING_WRITTEN = "nothing was written"


def apply_plan(plan: SetupPlan, backup_dir: str) -> ApplyResult:
    """Write a :func:`plan_setup` result after re-checking everything."""
    if plan.refusal:
        raise SetupRefused(plan.refusal)
    if plan.already_present:
        return ApplyResult(False, None, plan.before_hash)
    if plan.after_text is None:
        raise SetupRefused("nothing to write")
    _check_linkage(plan.config_path, plan.real_path, plan.link_identity, _NOTHING_WRITTEN)
    _check_owner(plan.real_path)
    if _current_hash(plan.real_path) != plan.before_hash:
        raise ConcurrentEdit("the config file changed since the preview; nothing was written")
    before_doc = tomllib.loads(plan.before_text) if plan.before_text else {}
    why = (herdr_ui_problem(before_doc) or verify_setup_semantics(before_doc, plan.after_text, plan.entry)
           or _candidate_problem(plan.after_text))
    if why:
        raise SetupRefused(why)
    backup = None
    if plan.exists:
        with os.fdopen(open_regular(plan.real_path), "rb") as fh:
            current = fh.read(MAX_CONFIG_BYTES + 1)
        if _sha(current) != plan.before_hash:
            raise ConcurrentEdit("the config file changed since the preview; nothing was written")
        backup = _backup(backup_dir, current)
    try:
        _write_checked(plan, plan.after_text.encode("utf-8"), plan.before_hash, _mode_of(plan.real_path))
    except BaseException:
        _discard_backup(backup)
        raise
    _prune_backups(backup_dir, backup)
    return ApplyResult(True, backup, _current_hash(plan.real_path))


def apply_detach(plan: DetachPlan, backup_dir: str) -> ApplyResult:
    """Write (or, for a file setup created and nobody touched, delete) a detach plan."""
    if plan.refusal:
        raise SetupRefused(plan.refusal)
    if plan.nothing_to_do:
        return ApplyResult(False, None, plan.before_hash)
    done = "nothing was deleted" if plan.delete_file else _NOTHING_WRITTEN
    _check_linkage(plan.config_path, plan.real_path, plan.link_identity, done)
    _check_owner(plan.real_path)
    with os.fdopen(open_regular(plan.real_path), "rb") as fh:
        current = fh.read(MAX_CONFIG_BYTES + 1)
    if _sha(current) != plan.before_hash:
        raise ConcurrentEdit("the config file changed since the preview; nothing was written")
    if plan.delete_file:
        # Re-check the bytes on disk rather than trusting the plan's flags.
        try:
            current_text = current.decode("utf-8")
        except UnicodeDecodeError:
            current_text = None
        if (not isinstance(plan.delete_entry, dict) or current_text is None
                or not is_minimal_created_config(current_text, plan.delete_entry)):
            raise SetupRefused("the config file holds more than the QuotaRibbon entry; it is not deleted")
    if not plan.delete_file:
        if plan.after_text is None:
            raise SetupRefused("nothing to write")
        tomllib.loads(plan.after_text)  # never write something that does not parse
    backup = _backup(backup_dir, current)
    try:
        if plan.delete_file:
            if _current_hash(plan.real_path) != plan.before_hash:
                raise ConcurrentEdit("the config file changed since the preview; nothing was written")
            _check_linkage(plan.config_path, plan.real_path, plan.link_identity, done)
            os.unlink(plan.real_path)
        else:
            _write_checked(plan, plan.after_text.encode("utf-8"), plan.before_hash, _mode_of(plan.real_path))
    except BaseException:
        _discard_backup(backup)
        raise
    _prune_backups(backup_dir, backup)
    if plan.delete_file:
        return ApplyResult(True, backup, None, deleted=True)
    return ApplyResult(True, backup, _current_hash(plan.real_path))


def revert_setup(config_path: str, record: dict, backup_dir: str, tabbar_path: str) -> ApplyResult:
    """Remove the entry a just-applied setup added."""
    plan = plan_detach(config_path, record, tabbar_path=tabbar_path)
    return apply_detach(plan, backup_dir)


__all__ = [
    "SetupError",
    "SetupRefused",
    "ConcurrentEdit",
    "SetupPlan",
    "DetachPlan",
    "ApplyResult",
    "toml_basic_string",
    "build_command",
    "build_entry",
    "entry_text",
    "manual_snippet",
    "is_our_entry",
    "is_canonical_command",
    "is_canonical_entry",
    "is_minimal_created_config",
    "wrapper_path",
    "wrapper_script",
    "install_wrapper",
    "legacy_command",
    "TAB_BAR_RIGHT_TYPES",
    "TAB_BAR_POSITIONS",
    "scan",
    "array_layout",
    "inspect_ui",
    "plan_setup",
    "apply_plan",
    "setup_record",
    "validate_record",
    "plan_detach",
    "apply_detach",
    "revert_setup",
    "verify_setup_semantics",
    "herdr_ui_problem",
]
