#!/usr/bin/env python3
"""Fake ``claude`` CLI for collector tests (no network, canary identities only).

``QR_FAKE_SCENARIO`` picks the behaviour. Optional variables: ``QR_FAKE_LOG`` (JSONL of
invocations and session stdin lines), ``QR_FAKE_STATE`` (``auth status`` call counter),
``QR_FAKE_PIDFILE`` (``<pid> <grandchild pid>``), ``QR_FAKE_ACCOUNT`` (``a``/``b``),
``QR_FAKE_USAGE_DELAY`` (seconds before ``get_usage``), ``QR_FAKE_CHECK_FD`` (records
whether that fd was inherited), ``QR_FAKE_AS_OF_AGE`` (age of the as-of time, or
``none``/``garbage``). Python 3.9+.
"""

import json
import os
import sys
import time

# ``signal`` and ``subprocess`` are imported lazily so common invocations start fast.

SCENARIO = os.environ.get("QR_FAKE_SCENARIO", "ok")
LOG = os.environ.get("QR_FAKE_LOG")
STATE = os.environ.get("QR_FAKE_STATE")
PIDFILE = os.environ.get("QR_FAKE_PIDFILE")
CHECK_FD = os.environ.get("QR_FAKE_CHECK_FD")

EMAIL_A = "canary-a@example.invalid"
EMAIL_B = "canary-b@example.invalid"
ORG_ID_A = "org-CANARY-0000-aaaa"
ORG_ID_B = "org-CANARY-0000-bbbb"
ORG_NAME_A = "Canary Org A"
ORG_NAME_B = "Canary Org B"
if os.environ.get("QR_FAKE_ACCOUNT", "a") == "b":
    EMAIL_A, EMAIL_B = EMAIL_B, EMAIL_A
    ORG_ID_A, ORG_ID_B = ORG_ID_B, ORG_ID_A
    ORG_NAME_A, ORG_NAME_B = ORG_NAME_B, ORG_NAME_A
try:
    USAGE_DELAY = max(0.0, float(os.environ.get("QR_FAKE_USAGE_DELAY", "0") or 0))
except ValueError:
    USAGE_DELAY = 0.0
HUGE = 10 ** 400  # a valid JSON integer far beyond float range


def log(record):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def bump_counter():
    if not STATE:
        return 1
    try:
        with open(STATE, encoding="utf-8") as fh:
            n = int(fh.read().strip() or "0")
    except (OSError, ValueError):
        n = 0
    n += 1
    with open(STATE, "w", encoding="utf-8") as fh:
        fh.write(str(n))
    return n


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ.get("HOME", "/nonexistent"), ".claude")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def emit_raw(text):
    sys.stdout.write(text)
    sys.stdout.flush()


# auth status


def subscription(email=EMAIL_A, org_id=ORG_ID_A, org_name=ORG_NAME_A, cfg=None, drop=()):
    obj = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "analyticsDisabled": False,
        "projectsDirectory": os.path.join(cfg or config_dir(), "projects"),
        "configDirectory": cfg or config_dir(),
        "email": email,
        "orgId": org_id,
        "orgName": org_name,
        "subscriptionType": "pro",
    }
    for k in drop:
        obj.pop(k, None)
    return obj


def auth_status():
    n = bump_counter()
    if SCENARIO == "logged_out":
        emit_raw(json.dumps({
            "loggedIn": False, "authMethod": "none", "apiProvider": "firstParty",
            "analyticsDisabled": False, "projectsDirectory": os.path.join(config_dir(), "projects"),
            "configDirectory": config_dir(),
        }, indent=1) + "\n")
        return 1
    if SCENARIO == "api_key":
        emit({
            "loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty",
            "analyticsDisabled": False, "configDirectory": config_dir(), "apiKeySource": "ANTHROPIC_API_KEY",
        })
        return 0
    if SCENARIO == "auth_garbage":
        emit_raw("Not JSON at all " + EMAIL_A + "\n")
        return 0
    if SCENARIO == "auth_slow":
        time.sleep(30)
        return 0
    if SCENARIO in ("init_unsupported_switch", "switch_after_session") and n >= 2:
        emit(subscription(EMAIL_B, ORG_ID_B, ORG_NAME_B))
        return 0
    if SCENARIO == "same_org_name_other_org_id" and n >= 2:
        # Same email and organization NAME, different organization: a different account.
        emit(subscription(org_id=ORG_ID_B))
        return 0
    if SCENARIO == "auth_after_no_org_id" and n >= 2:
        emit(subscription(drop=("orgId",)))
        return 0
    if SCENARIO == "auth_before_no_org_id" and n == 1:
        emit(subscription(drop=("orgId",)))
        return 0
    if SCENARIO == "auth_after_logged_out" and n >= 2:
        emit({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty", "configDirectory": config_dir()})
        return 1
    if SCENARIO == "auth_after_slow" and n >= 2:
        time.sleep(0.2)
    emit(subscription())
    return 0


# stream-json session


# Reset times lie far in the future so values from the fake never expire in end-to-end runs.
def window(util, resets="2036-01-08T00:00:00.500000+00:00"):
    return {"utilization": util, "resets_at": resets, "limit_dollars": None, "used_dollars": None,
            "remaining_dollars": None, "locked_reason": None}


def as_of_value():
    raw = os.environ.get("QR_FAKE_AS_OF_AGE", "0")
    if raw in ("none", "garbage"):
        return raw
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() - float(raw), tz=timezone.utc).isoformat()


def rate_limits(five=None, seven=None, drop=()):
    rl = {
        "five_hour": five,
        "seven_day": seven,
        "seven_day_opus": window(12),
        "seven_day_sonnet": None,
        "extra_bucket_c": None,
        # Unknown bucket at 100 % with dollar fields: must never be shown.
        "extra_bucket_a": {"utilization": 100, "resets_at": "2036-01-09T00:00:00+00:00", "limit_dollars": 100,
                           "used_dollars": 100, "remaining_dollars": 0, "locked_reason": None},
        "extra_bucket_b": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
        "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 68, "severity": "normal",
                    "resets_at": "2036-01-08T00:00:00.500000+00:00", "scope": None, "is_active": True}],
        "spend": {"percent": 0, "severity": "normal", "enabled": False},
    }
    as_of = as_of_value()
    if as_of != "none":
        rl["seven_day_breakdown"] = {"as_of": "not-a-time" if as_of == "garbage" else as_of}
    for k in drop:
        rl.pop(k, None)
    return rl


def usage_body():
    s = SCENARIO
    body = {
        "session": {"total_cost_usd": 0, "total_api_duration_ms": 0, "total_duration_ms": 12,
                    "total_lines_added": 0, "total_lines_removed": 0, "model_usage": {}},
        "subscription_type": "pro",
        "rate_limits_available": True,
        "behaviors": None,
    }
    if s == "unavailable":
        body.update(subscription_type=None, rate_limits_available=False, rate_limits=None)
    elif s == "huge_numbers":
        body["rate_limits"] = rate_limits(window(HUGE), window(68, HUGE))
    elif s == "five_hour_null":
        body["rate_limits"] = rate_limits(None, window(79))
    elif s == "no_windows":
        body["rate_limits"] = rate_limits(None, None)
    else:
        body["rate_limits"] = rate_limits(window(32, "2036-11-02T08:00:00+00:00"), window(68))
    return body


def init_body():
    if SCENARIO == "identity_changed":
        acct = {"email": EMAIL_B, "organization": ORG_NAME_B, "subscriptionType": "Example Plan", "apiProvider": "firstParty"}
    elif SCENARIO == "init_token_none":
        acct = {"tokenSource": "none", "apiProvider": "firstParty"}
    elif SCENARIO == "init_api_key_source":
        acct = {"email": EMAIL_A, "organization": ORG_NAME_A, "apiKeySource": "ANTHROPIC_API_KEY", "apiProvider": "firstParty"}
    elif SCENARIO == "init_no_email":
        acct = {"organization": ORG_NAME_A, "subscriptionType": "Example Plan", "apiProvider": "firstParty"}
    else:
        acct = {"email": EMAIL_A, "organization": ORG_NAME_A, "subscriptionType": "Example Plan", "apiProvider": "firstParty"}
    return {"commands": [], "models": [], "account": acct, "claude_code_version": "2.1.316", "hooks_applied": None}


def spawn_grandchild():
    import subprocess

    gc = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if PIDFILE:
        with open(PIDFILE, "w", encoding="utf-8") as fh:
            fh.write("%d %d" % (os.getpid(), gc.pid))
    return gc


def respond(req, body=None, error=None):
    rid = req.get("request_id")
    if error is not None:
        emit({"type": "control_response", "response": {"subtype": "error", "request_id": rid, "error": error}})
    else:
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": body}})


def session():
    s = SCENARIO
    if s in ("slow", "slow_ignore_term"):
        spawn_grandchild()
        if s == "slow_ignore_term":
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if s == "hold_until_eof":
        if PIDFILE:
            with open(PIDFILE, "w", encoding="utf-8") as fh:
                fh.write("%d 0" % os.getpid())
    emit({"type": "system", "subtype": "ui_invalidate", "event": "ui.render", "uuid": "00000000", "session_id": "00000000"})
    if s == "stdin_closed_early":
        sys.stdin.close()
        os.close(0)
        return 0
    for raw in sys.stdin:
        log({"mode": "session", "line": raw.rstrip("\n")})
        if s in ("slow", "slow_ignore_term", "hold_until_eof"):
            continue  # never answer; exit only when stdin closes (or when killed)
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        req = msg.get("request") or {}
        sub = req.get("subtype")
        if sub == "initialize":
            if s in ("init_unsupported_ok", "init_unsupported_switch"):
                respond(msg, error="Unknown control request subtype: initialize")
            else:
                respond(msg, init_body())
            continue
        if sub != "get_usage":
            respond(msg, error="unsupported")
            continue
        if USAGE_DELAY:
            time.sleep(USAGE_DELAY)
        if s == "assistant_turn":
            emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}})
            continue
        if s == "malformed_json":
            emit_raw('{"type": "control_response", "response": {"subtype": "success", "request_id": \n')
            continue
        if s == "nan_literal":
            rid = msg.get("request_id")
            emit_raw('{"type":"control_response","response":{"subtype":"success","request_id":"%s","response":'
                     '{"subscription_type":"pro","rate_limits_available":true,"rate_limits":{"five_hour":'
                     '{"utilization":NaN,"resets_at":null},"seven_day":null}}}}\n' % rid)
            continue
        if s == "huge_line":
            emit_raw('{"type":"system","subtype":"blob","data":"' + ("x" * (2 << 20)) + '"}\n')
            respond(msg, usage_body())
            continue
        if s == "raw_control_chars":
            # A raw ESC and a raw newline inside a JSON string: invalid JSON lines.
            emit_raw('{"type":"system","subtype":"note","text":"\x1b[31mred\nline"}\n')
            respond(msg, usage_body())
            continue
        if s == "escaped_control_chars":
            # Valid JSON: escaped ESC / newline inside an ignored string field.
            emit({"type": "system", "subtype": "note", "text": "\x1b[31m" + EMAIL_A + "\nsecond line\x07"})
            body = usage_body()
            body["session"]["note"] = "\x1b]0;title\x07" + ORG_ID_A
            respond(msg, body)
            continue
        if s == "usage_error":
            respond(msg, error="usage fetch failed for " + EMAIL_A)
            continue
        if s == "cli_control_request":
            emit({"type": "control_request", "request_id": "cli-1", "request": {"subtype": "can_use_tool"}})
        if s in ("trailing_malformed_same_write", "trailing_turn_same_write"):
            # The matching response and the bad line arrive in one write.
            rid = msg.get("request_id")
            ok = json.dumps({"type": "control_response",
                             "response": {"subtype": "success", "request_id": rid, "response": usage_body()}})
            bad = '{"type": "system", "subtype": ' if s == "trailing_malformed_same_write" else json.dumps(
                {"type": "assistant", "message": {"role": "assistant", "content": []}})
            emit_raw(ok + "\n" + bad + "\n")
            continue
        respond(msg, usage_body())
    if s == "trailing_malformed_on_eof":
        emit_raw('{"type": "system", "subtype": "late", \n')
    if s == "trailing_turn_on_eof":
        emit({"type": "result", "subtype": "success", "is_error": False})
    if s == "trailing_system_on_eof":
        emit({"type": "system", "subtype": "late_notice"})
    if s == "slow_exit":
        time.sleep(0.3)  # usage answered well before the process ends
    if s == "hold_until_eof":
        time.sleep(0.3)  # a short, graceful shutdown after stdin EOF
    if s == "slow_ignore_term":
        while True:  # ignores both stdin EOF and SIGTERM: only SIGKILL ends it
            time.sleep(0.2)
    return 0


def fd_open():
    if not CHECK_FD:
        return None
    try:
        os.fstat(int(CHECK_FD))
        return True
    except OSError:
        return False


def invocation_kind(argv):
    if argv[:1] == ["--version"]:
        return "version"
    if argv[:2] == ["auth", "status"]:
        return "auth_status"
    if "-p" in argv and "stream-json" in argv:
        return "session"
    return "other"


def main(argv):
    log({"mode": "invoke", "kind": invocation_kind(argv), "argv": argv, "pid": os.getpid(),
         "env": {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID",
                                               "HERDR_WORKSPACE_ID", "HERDR_PLUGIN_ID", "ANTHROPIC_API_KEY")},
         "cwd": os.getcwd(), "fd_open": fd_open()})
    if argv[:1] == ["--version"]:
        sys.stdout.write("2.1.316 (Claude Code)\n")
        return 0
    if argv[:2] == ["auth", "status"]:
        return auth_status()
    if "-p" in argv and "stream-json" in argv:
        return session()
    sys.stderr.write("fake_claude: unsupported invocation\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
