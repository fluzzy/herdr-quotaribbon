"""Builders for persisted records and in-memory views used by renderer/scheduler tests."""

from __future__ import annotations

from quotaribbon import binding
from quotaribbon import types as t
from tests.helpers.core_fakes import ACCT_A, T0, window

EPOCH = "epoch-1"
GEN = 4


def bind(provider: str) -> binding.Binding:
    return binding.make_binding(provider, f"/profiles/{provider}", "/cfg/config.toml", host="h", uid=501)


def conn(provider: str = "claude", **kw) -> t.ConnectionRecord:
    b = bind(provider)
    base = dict(
        schema_version=1, provider=provider, binding_key=b.binding_key, profile_key=b.profile_key, state="connected",
        ever_connected=True, evidence={"source": "integration", "integration_state": "current", "observed_at": T0 - 10},
        executable=f"/usr/bin/{provider}", cli_version="1.0.0", account_key=ACCT_A, auth_epoch=2, verified_at=T0 - 600,
        verified_server_epoch=EPOCH, verified_generation=GEN, next_verify_at=T0 - 600, last_reason=None, env_conflicts=[],
    )
    base.update(kw)
    return t.ConnectionRecord(**base)


def stat(provider: str = "claude", **kw) -> t.ProviderStatus:
    base = dict(
        schema_version=1, provider=provider, data_state="ok", attempt_started_at=T0 - 61, attempt_finished_at=T0 - 60,
        last_success_at=T0 - 60, last_failure_at=None, consecutive_failures=0, next_due_at=T0 + 60, retry_not_before=None,
        last_error_code=None, unsupported_cli_version=None, notes=[],
    )
    base.update(kw)
    return t.ProviderStatus(**base)


CLAUDE_W = [window("five_hour", 18000, 32.0), window("seven_day", 604800, 68.0)]
CODEX_W = [window("primary", 18000, 54.0), window("secondary", 604800, 21.0)]


def snap(provider: str = "claude", age: float = 60.0, windows=None, **kw) -> t.Snapshot:
    b = bind(provider)
    base = dict(
        schema_version=1, provider=provider, scope_key="scope", profile_key=b.profile_key, account_key=ACCT_A, auth_epoch=2,
        source=t.PROVIDER_SOURCES[provider], fetched_at=t.iso_utc_precise(T0 - age), source_observed_at=None, status="ok",
        windows=list(windows if windows is not None else (CLAUDE_W if provider == "claude" else CODEX_W)),
        cli_version="1.0.0", server_epoch=EPOCH,
    )
    base.update(kw)
    return t.Snapshot(**base)


def pview(provider: str = "claude", *, connection="default", status="default", snapshot="default", generation: int = GEN) -> t.ProviderView:
    return t.ProviderView(
        connection=conn(provider) if connection == "default" else connection,
        status=stat(provider) if status == "default" else status,
        snapshot=snap(provider) if snapshot == "default" else snapshot,
        generation=generation,
    )


def state(claude=None, codex=None, *, epoch: str | None = EPOCH, dispatch=None) -> t.StateView:
    providers = {}
    if claude is not None:
        providers["claude"] = claude
    if codex is not None:
        providers["codex"] = codex
    return t.StateView(server_epoch=epoch, providers=providers, dispatch=dispatch)


def settings(claude: bool = True, codex: bool = True, **refresh) -> t.Settings:
    obj = {"schema_version": 1, "providers": {"claude": {"enabled": claude}, "codex": {"enabled": codex}}}
    if refresh:
        obj["refresh"] = refresh
    return t.Settings.from_json(obj)
