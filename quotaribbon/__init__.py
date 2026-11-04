"""QuotaRibbon: Claude Code and Codex subscription limits in the Herdr tab bar."""

from __future__ import annotations

PLUGIN_ID = "fluzzy.herdr-quotaribbon"
__version__ = "0.1.0"
# Version of the stable tab-bar bootstrap contract (bootstrap.json / tabbar.py).
BOOTSTRAP_API = 1
REFRESH_ACTION_QUALIFIED = "fluzzy.herdr-quotaribbon.refresh"


def _core(name: str):
    """Import ``quotaribbon.<name>`` lazily.

    Unlike a bare ``sys.modules`` lookup, the import machinery waits for a module another
    thread is still initialising.
    """
    return __import__(f"{__name__}.{name}", fromlist=("_",))

__all__ = ["PLUGIN_ID", "__version__", "BOOTSTRAP_API", "REFRESH_ACTION_QUALIFIED"]
