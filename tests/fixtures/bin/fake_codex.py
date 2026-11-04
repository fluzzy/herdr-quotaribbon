#!/usr/bin/env python3
"""Fake ``codex`` CLI (``--version`` and ``app-server`` on stdio) for collector tests.

``QR_FAKE_SCENARIO`` picks the behaviour; framing matches codex-cli 0.166.0 (NDJSON, no
``"jsonrpc"`` field, notifications interleaved with responses). Optional variables:
``QR_FAKE_LOG``, ``QR_FAKE_CHECK_FD``, ``QR_FAKE_ACCOUNT`` (``a``/``b``) and
``QR_FAKE_USAGE_DELAY``, as for the fake ``claude``. Python 3.9+.
"""

import json
import os
import sys
import time

SCENARIO = os.environ.get("QR_FAKE_SCENARIO", "ok")
LOG = os.environ.get("QR_FAKE_LOG")
CHECK_FD = os.environ.get("QR_FAKE_CHECK_FD")

EMAIL_A = "canary-a@example.invalid"
EMAIL_B = "canary-b@example.invalid"
ACCT_A = "acct-CANARY-aaaa-0000"
ACCT_B = "acct-CANARY-bbbb-0000"
if os.environ.get("QR_FAKE_ACCOUNT", "a") == "b":
    EMAIL_A, EMAIL_B = EMAIL_B, EMAIL_A
    ACCT_A, ACCT_B = ACCT_B, ACCT_A
try:
    USAGE_DELAY = max(0.0, float(os.environ.get("QR_FAKE_USAGE_DELAY", "0") or 0))
except ValueError:
    USAGE_DELAY = 0.0
RESET = 2109352885  # Unix seconds, far in the future so end-to-end values never expire
HUGE = 10 ** 400  # a valid JSON integer far beyond float range

# Server request ids that must never be echoed back.
BAD_SERVER_IDS = {
    "bad_server_id_object": {"nested": 1},
    "bad_server_id_huge_int": HUGE,
    "bad_server_id_long": "x" * 65,
    "bad_server_id_spaces": "id with spaces",
    "bad_server_id_bool": True,
}


def log(record):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(method, params):
    emit({"method": method, "params": params, "emittedAtMs": 2000000000000})


def win(pct, mins, reset=RESET):
    return {"usedPercent": pct, "windowDurationMins": mins, "resetsAt": reset}


def snapshot(limit_id, primary, secondary):
    return {"limitId": limit_id, "limitName": None, "normalModelSlug": None, "primary": primary,
            "secondary": secondary, "credits": {"hasCredits": False, "unlimited": False, "balance": "0E-10"},
            "individualLimit": None, "spendControlReached": False, "planType": "plus",
            "rateLimitReachedType": None}


def rate_limits_result():
    s = SCENARIO
    if s == "huge_numbers":
        codex = snapshot("codex", win(HUGE, 300), win(21, 10080, HUGE))
    else:
        codex = snapshot("codex", win(54, 300, RESET - 100000), win(21, 10080))
    result = {"ordinaryUsageAllowed": True, "rateLimits": codex, "rateLimitsByLimitId": {"codex": codex},
              "rateLimitResetCredits": {"availableCount": 1, "credits": None},
              "accountId": ACCT_A, "rateLimitUpsell": None}
    if s == "model_only":
        model = snapshot("gpt_model_only", win(77, 300), win(88, 10080))
        result["rateLimits"] = model
        result["rateLimitsByLimitId"] = {"gpt_model_only": model}
    if s == "usage_account_mismatch":
        result["accountId"] = ACCT_B
    if s == "usage_account_id_not_a_string":
        result["accountId"] = 12345
    if s == "usage_without_account_id":
        del result["accountId"]
    return result


def account_result(n_read):
    s = SCENARIO
    if s in ("logged_out", "env_api_key"):
        return {"account": None, "requiresOpenaiAuth": True, "workspaceRouting": None}
    if s == "api_key":
        return {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True, "workspaceRouting": None}
    email, acct = EMAIL_A, ACCT_A
    if s == "account_changed" and n_read >= 2:
        email, acct = EMAIL_B, ACCT_B
    if s == "no_routing_id" or (s == "routing_missing_after" and n_read >= 2):
        # Email only: not a stable account identity.
        return {"account": {"type": "chatgpt", "email": email, "planType": "plus"},
                "requiresOpenaiAuth": True, "workspaceRouting": None}
    return {"account": {"type": "chatgpt", "email": email, "planType": "plus", "workspaceName": None},
            "requiresOpenaiAuth": True,
            "workspaceRouting": {"chatgptAccountId": acct, "backendOrigin": "https://chatgpt.invalid",
                                 "accountRoutingOverride": "NO_CONSTRAINT"}}


def app_server():
    s = SCENARIO
    reads = 0
    for raw in sys.stdin:
        log({"mode": "session", "line": raw.rstrip("\n")})
        if s == "slow":
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            home = os.environ.get("CODEX_HOME") or os.path.join(os.environ.get("HOME", "/nonexistent"), ".codex")
            if s == "wrong_home":
                home = "/nonexistent/other-codex-home"
            if s == "bool_id_response":
                # ``true == 1`` in Python: must never be taken as the response to id 1.
                emit({"id": True, "result": {"codexHome": "/nonexistent/other-codex-home"}})
            emit({"id": rid, "result": {"userAgent": "quotaribbon/0.166.0", "codexHome": home,
                                        "platformFamily": "unix", "platformOs": "macos"}})
        elif method == "initialized":
            note("remoteControl/status/changed", {"status": "disabled", "serverName": "host.invalid",
                                                  "installationId": "00000000", "environmentId": None})
        elif method == "account/read":
            reads += 1
            if reads == 1 and s not in ("logged_out", "api_key", "env_api_key"):
                note("account/updated", {"authMode": "chatgpt", "planType": "plus"})
            if s == "server_request" and reads == 1:
                emit({"id": "srv-1", "method": "account/chatgptAuthTokens/refresh", "params": {"reason": "x"}})
            if s in BAD_SERVER_IDS and reads == 1:
                emit({"id": BAD_SERVER_IDS[s], "method": "account/chatgptAuthTokens/refresh", "params": {}})
            if s == "turn_notification" and reads == 1:
                note("turn/started", {"threadId": "t", "turn": {"id": "x"}})
            if s == "approval_request" and reads == 1:
                emit({"id": 7, "method": "item/commandExecution/requestApproval", "params": {}})
            if s == "trailing_malformed_same_write" and reads == 2:
                sys.stdout.write(json.dumps({"id": rid, "result": account_result(reads)}) + "\n" + '{"method": \n')
                sys.stdout.flush()
                continue
            emit({"id": rid, "result": account_result(reads)})
        elif method == "account/rateLimits/read":
            if USAGE_DELAY:
                time.sleep(USAGE_DELAY)
            if s in ("logged_out", "env_api_key"):
                emit({"error": {"code": -32600, "message": "codex account authentication required to read rate limits"}, "id": rid})
            elif s == "rate_limited":
                emit({"error": {"code": -32603, "message": "rate limited " + EMAIL_A,
                                "data": {"codexErrorInfo": "rateLimitExceeded"}}, "id": rid})
            elif s in ("rpc_error", "usage_error"):
                emit({"error": {"code": -32603, "message": "internal " + ACCT_A}, "id": rid})
            elif s == "malformed_json":
                sys.stdout.write('{"id": %s, "result": {"rateLimits": \n' % json.dumps(rid))
                sys.stdout.flush()
            else:
                note("account/rateLimits/updated", {"rateLimits": snapshot("codex", win(1, 300), None)})
                emit({"id": rid, "result": rate_limits_result()})
        elif rid is not None and method is not None:
            emit({"error": {"code": -32601, "message": "method not found"}, "id": rid})
    if s == "trailing_turn_on_eof":
        note("item/started", {"item": {"type": "agentMessage"}})
    if s == "trailing_malformed_on_eof":
        sys.stdout.write("not json\n")
        sys.stdout.flush()
    if s == "trailing_notification_on_eof":
        note("account/updated", {"authMode": "chatgpt", "planType": "plus"})
    if s == "slow_exit":
        time.sleep(0.3)
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
    if argv[:1] == ["app-server"]:
        return "app_server"
    return "other"


def main(argv):
    log({"mode": "invoke", "kind": invocation_kind(argv), "argv": argv, "pid": os.getpid(),
         "env": {k: os.environ.get(k) for k in ("CODEX_HOME", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_PLUGIN_ID", "OPENAI_API_KEY")},
         "cwd": os.getcwd(), "fd_open": fd_open()})
    if argv[:1] == ["--version"]:
        sys.stdout.write("codex-cli 0.166.0\n")
        return 0
    if argv[:1] == ["app-server"]:
        if "--startup-sign-out" in argv:
            sys.stderr.write("refusing\n")
            return 3
        return app_server()
    sys.stderr.write("fake_codex: unsupported invocation\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
