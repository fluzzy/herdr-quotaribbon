"""State files as a successful refresh would write them, built with the real core modules."""

from __future__ import annotations

import time

from quotaribbon import binding, cache
from quotaribbon import settings as settings_mod
from quotaribbon import types as t
from tests.helpers.core_fakes import ACCT_A, window

WINDOWS = {
    "claude": [window("five_hour", 18000, 32.0), window("seven_day", 604800, 68.0)],
    "codex": [window("primary", 18000, 54.0), window("secondary", 604800, 21.0)],
}
LINES = {"claude": "Claude 5h 32% | 7d 68%", "codex": "Codex 5h 54% | 7d 21%"}


def seed_connected(paths, env: dict, *, providers=("claude", "codex"), now: float | None = None,
                   age: float = 60.0, usage_due: bool = False) -> dict:
    """Seed connected providers with a fresh snapshot; returns ``{provider: Binding}``.

    ``usage_due=True`` makes the next usage collection due at once.
    """
    now = time.time() if now is None else now
    paths.ensure_private_dirs()
    s = settings_mod.DEFAULT_SETTINGS
    gens = cache.sync_fingerprints(paths, settings_mod.settings_fingerprints(s))
    epoch = cache.read_server_epoch(paths) or cache.rotate_server_epoch(paths, now=now - 1000)
    path, ident = binding.server_identity(env)
    servers = {path: ident} if ident else {}
    out = {}
    for p in providers:
        b = binding.binding_for(p, s, env)
        paths.ensure_binding_dir(p, b.binding_key)
        cache.write_connection(paths, t.ConnectionRecord(
            schema_version=1, provider=p, binding_key=b.binding_key, profile_key=b.profile_key, state="connected",
            ever_connected=True, evidence={"source": "integration", "integration_state": "current", "observed_at": now - 10},
            executable=f"/usr/bin/{p}", cli_version="1.0.0", account_key=ACCT_A, auth_epoch=1, verified_at=now - 600,
            verified_server_epoch=epoch, verified_generation=gens[p], next_verify_at=now - 600, last_reason=None,
            env_conflicts=[], verified_servers=dict(servers),
        ))
        cache.write_status(paths, t.ProviderStatus(
            schema_version=1, provider=p, data_state="ok", attempt_started_at=now - age - 1,
            attempt_finished_at=now - age, last_success_at=now - age, last_failure_at=None, consecutive_failures=0,
            next_due_at=(now - 1) if usage_due else (now + 600), retry_not_before=None, last_error_code=None,
            unsupported_cli_version=None, notes=[],
        ), b.binding_key)
        cache.write_snapshot(paths, t.Snapshot(
            schema_version=1, provider=p, scope_key=binding.scope_key(p, b.profile_dir), profile_key=b.profile_key,
            account_key=ACCT_A, auth_epoch=1, source=t.PROVIDER_SOURCES[p], fetched_at=t.iso_utc_precise(now - age),
            source_observed_at=None, status="ok", windows=list(WINDOWS[p]), cli_version="1.0.0", server_epoch=epoch,
        ), b.binding_key)
        out[p] = b
    return out


__all__ = ["LINES", "WINDOWS", "seed_connected"]
