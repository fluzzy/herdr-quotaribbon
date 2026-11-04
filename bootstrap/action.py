"""Python entry point for every manifest command, run with ``python -I``.

The plugin root goes first on ``sys.path`` so the real ``quotaribbon`` package is always imported.
"""

from __future__ import annotations

import os
import stat
import sys

# Standard-library modules the cache check needs come from the interpreter's own cache, before
# any pycache prefix is chosen.
import grp  # noqa: F401
import pwd  # noqa: F401
import subprocess  # noqa: F401
import threading  # noqa: F401

# Below a character device nothing can exist: bytecode is then neither read nor written.
_NO_BYTECODE_PREFIX = os.path.join(os.devnull, "quotaribbon-pycache")
_MAX_CACHE_ENTRIES = 16384


def _plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.realpath(os.path.abspath(__file__))))


def _no_bytecode_location() -> bool:
    try:
        st = os.lstat(os.devnull)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and st.st_uid == 0


def _trusted_cache(pycache: str) -> bool:
    """True when no other account can change ``pycache`` or anything below it. Never raises."""
    from quotaribbon import trust  # noqa: PLC0415 - compiled from source: no prefix is trusted yet

    try:
        if not trust.trusted_directory(os.path.dirname(pycache)):
            return False
        if not os.path.lexists(pycache):
            return True  # created on demand inside the checked state dir
        components = []
        todo = [pycache]
        st = os.lstat(pycache)
        if not stat.S_ISDIR(st.st_mode):
            return False
        components.append((pycache, st))
        while todo:
            cur = todo.pop()
            with os.scandir(cur) as it:
                names = [e.name for e in it]
            for name in names:
                path = os.path.join(cur, name)
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue  # removed meanwhile: a missing entry is never read
                directory = stat.S_ISDIR(st.st_mode)
                if not (directory or stat.S_ISREG(st.st_mode)):
                    return False  # symlinks, FIFOs, sockets and devices
                if not trust._trusted_owner(st.st_uid) or not trust._safe_write_bits(st, directory=False):
                    return False
                components.append((path, st))
                if len(components) > _MAX_CACHE_ENTRIES:
                    return False
                if directory:
                    todo.append(path)
        root_st = components[0][1]
        if not trust._trusted_owner(root_st.st_uid) or not trust._safe_write_bits(root_st, directory=False):
            return False
        return trust._acls_ok(components)
    except (OSError, ValueError):
        return False


def _configure_pycache() -> None:
    """Read bytecode only from a state cache no other account can change; else use none.

    The cache is executable input: a planted ``.pyc`` must not run before startup clears it.
    """
    if not _no_bytecode_location():
        raise SystemExit("QuotaRibbon: refusing to run: bytecode cannot be kept off")
    write_default = sys.dont_write_bytecode
    sys.pycache_prefix = _NO_BYTECODE_PREFIX
    sys.dont_write_bytecode = True
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir or not os.path.isabs(state_dir):
        return
    pycache = os.path.join(state_dir, "pycache")
    if _trusted_cache(pycache):
        sys.pycache_prefix = pycache
        sys.dont_write_bytecode = write_default


def main() -> int:
    root = _plugin_root()
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    _configure_pycache()
    from quotaribbon.cli import main as cli_main

    return int(cli_main(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
