"""Parsing of Codex ``account/read`` and ``account/rateLimits/read`` (redacted 0.166.0 captures)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from quotaribbon.providers import codex
from quotaribbon.providers.codex import (
    _check_message,
    _is_rate_limit_error,
    _valid_server_id,
    _Violation,
    parse_account_read,
    parse_rate_limits,
)
from quotaribbon.types import Window, is_valid_note

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "codex")
SALT = b"\x03" * 32
HOME = os.path.join(tempfile.gettempdir(), "qr-home")


def session(name: str) -> list[dict]:
    with open(os.path.join(FIX, f"app-server.{name}.session.json"), encoding="utf-8") as fh:
        return json.loads(fh.read().replace("{HOME}", HOME))


def response(name: str, rid: int) -> dict:
    for m in session(name):
        if m["dir"] == "recv" and m["msg"].get("id") == rid:
            return m["msg"]
    raise AssertionError(f"no response id {rid}")


def win(pct, mins, reset=1767830400):
    return {"usedPercent": pct, "windowDurationMins": mins, "resetsAt": reset}


def snap(limit_id, primary, secondary):
    return {"limitId": limit_id, "primary": primary, "secondary": secondary, "planType": "plus"}


def by_id(**buckets):
    return {"ordinaryUsageAllowed": True, "rateLimits": next(iter(buckets.values())), "rateLimitsByLimitId": buckets}


class AccountReadTests(unittest.TestCase):
    def test_chatgpt_account_is_subscription_with_hashed_key(self):
        r = parse_account_read(response("subscription", 2)["result"], salt=SALT, env={})
        self.assertEqual(r.state, "subscription")
        self.assertEqual(r.detail, "auth_method:chatgpt")
        self.assertRegex(r.account_key, r"^acct:[0-9a-f]{24}$")
        self.assertNotIn("example.invalid", repr(r))
        self.assertNotIn("acct-REDACTED", repr(r))

    def test_both_account_reads_of_one_session_give_the_same_key(self):
        a = parse_account_read(response("subscription", 2)["result"], salt=SALT, env={})
        b = parse_account_read(response("subscription", 4)["result"], salt=SALT, env={})
        self.assertEqual(a.account_key, b.account_key)

    def test_different_email_or_account_id_changes_key(self):
        base = response("subscription", 2)["result"]
        k0 = parse_account_read(base, salt=SALT, env={}).account_key
        other_email = copy.deepcopy(base)
        other_email["account"]["email"] = "other@example.invalid"
        other_id = copy.deepcopy(base)
        other_id["workspaceRouting"]["chatgptAccountId"] = "acct-OTHER"
        self.assertNotEqual(k0, parse_account_read(other_email, salt=SALT, env={}).account_key)
        self.assertNotEqual(k0, parse_account_read(other_id, salt=SALT, env={}).account_key)

    def test_logged_out_is_logged_out(self):
        r = parse_account_read(response("logged-out", 2)["result"], salt=SALT, env={})
        self.assertEqual(r.state, "logged_out")
        self.assertIsNone(r.account_key)

    def test_env_api_key_alone_is_not_a_login_but_is_reported(self):
        r = parse_account_read(response("env-api-key", 2)["result"], salt=SALT, env={"OPENAI_API_KEY": "sk-CANARY"})
        self.assertEqual(r.state, "logged_out")
        self.assertEqual(r.env_conflicts, ("OPENAI_API_KEY",))
        self.assertNotIn("sk-CANARY", repr(r))

    def test_api_key_login_is_api_auth(self):
        r = parse_account_read(response("api-key-login", 2)["result"], salt=SALT, env={})
        self.assertEqual(r.state, "api_auth")
        self.assertEqual(r.detail, "auth_method:apiKey")

    def test_cloud_accounts_are_api_auth(self):
        for kind in ("amazonBedrock", "gcp"):
            r = parse_account_read({"account": {"type": kind}, "requiresOpenaiAuth": True}, salt=SALT, env={})
            self.assertEqual(r.state, "api_auth", kind)

    def test_unknown_account_type_is_unsupported(self):
        r = parse_account_read({"account": {"type": "quantumLink"}}, salt=SALT, env={})
        self.assertEqual((r.state, r.detail), ("unsupported_auth", "auth_method:quantumLink"))
        r2 = parse_account_read({"account": {"type": "x" * 200}}, salt=SALT, env={})
        self.assertEqual(r2.detail, "auth_method:other")

    def test_subscription_without_identifiers_has_no_key(self):
        r = parse_account_read({"account": {"type": "chatgpt", "email": None, "planType": "plus"}}, salt=SALT, env={})
        self.assertEqual(r.state, "subscription")
        self.assertIsNone(r.account_key)

    def test_email_alone_is_never_an_account_identity(self):
        base = response("subscription", 2)["result"]
        no_routing = copy.deepcopy(base)
        no_routing["workspaceRouting"] = None
        empty_id = copy.deepcopy(base)
        empty_id["workspaceRouting"]["chatgptAccountId"] = ""
        not_str = copy.deepcopy(base)
        not_str["workspaceRouting"]["chatgptAccountId"] = 12345
        for obj in (no_routing, empty_id, not_str):
            r = parse_account_read(obj, salt=SALT, env={})
            self.assertEqual(r.state, "subscription")
            self.assertIsNone(r.account_key)

    def test_stable_account_id_without_email_gives_a_key(self):
        base = copy.deepcopy(response("subscription", 2)["result"])
        base["account"]["email"] = None
        self.assertRegex(parse_account_read(base, salt=SALT, env={}).account_key, r"^acct:[0-9a-f]{24}$")

    def test_unexpected_exception_while_parsing_account_is_a_retryable_error(self):
        with mock.patch.object(codex, "account_key", side_effect=RuntimeError):
            r = parse_account_read(response("subscription", 2)["result"], salt=SALT, env={})
        self.assertEqual((r.state, r.detail, r.retryable), ("error", "account_read_unparsable", True))

    def test_malformed_results_are_retryable_errors(self):
        for obj in (None, [], {}, {"account": "chatgpt"}, {"account": {"type": 1}}):
            r = parse_account_read(obj, salt=SALT, env={})
            self.assertEqual(r.state, "error", obj)
            self.assertTrue(r.retryable)


class RateLimitsTests(unittest.TestCase):
    def test_captured_result_uses_codex_bucket_and_labels_by_duration(self):
        outcome, windows, notes = parse_rate_limits(response("subscription", 3)["result"])
        self.assertEqual(outcome, "ok")
        self.assertEqual(len(windows), 1)  # secondary is null
        w = windows[0]
        self.assertEqual((w.id, w.duration_seconds, w.used_percent, w.raw_used_percent), ("primary", 604800, 40.0, 40))
        self.assertEqual(w.resets_at, "2026-01-08T00:00:00Z")  # 1767830400 Unix seconds
        self.assertEqual(notes, ())

    def test_five_hour_and_weekly_sorted_by_duration(self):
        obj = by_id(codex=snap("codex", win(21, 10080), win(54, 300)))
        outcome, windows, _ = parse_rate_limits(obj)
        self.assertEqual(outcome, "ok")
        self.assertEqual([(w.id, w.duration_seconds) for w in windows], [("secondary", 18000), ("primary", 604800)])

    def test_short_windows_without_weekly_keep_their_real_durations(self):
        outcome, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(25, 15), win(40, 60))))
        self.assertEqual(outcome, "ok")
        self.assertEqual([w.duration_seconds for w in windows], [900, 3600])
        self.assertEqual([w.used_percent for w in windows], [25.0, 40.0])
        self.assertNotIn(18000, [w.duration_seconds for w in windows])
        self.assertNotIn(604800, [w.duration_seconds for w in windows])

    def test_model_only_buckets_without_codex_are_not_shown(self):
        model = snap("gpt_model_only", win(77, 300), win(88, 10080))
        outcome, windows, notes = parse_rate_limits({"rateLimits": model, "rateLimitsByLimitId": {"gpt_model_only": model}})
        self.assertEqual((outcome, windows), ("unavailable", ()))
        self.assertIn("no_common_bucket", notes)
        self.assertIn("ignored_model_buckets:1", notes)

    def test_other_buckets_are_counted_not_copied(self):
        obj = by_id(codex=snap("codex", win(1, 300), None), secret_bucket_name=snap("secret_bucket_name", win(100, 300), None))
        outcome, windows, notes = parse_rate_limits(obj)
        self.assertEqual(outcome, "ok")
        self.assertEqual([w.used_percent for w in windows], [1.0])
        self.assertEqual(notes, ("ignored_model_buckets:1",))
        self.assertNotIn("secret_bucket_name", repr(notes))

    def test_legacy_bucket_used_only_when_identifiably_codex(self):
        legacy = {"rateLimits": snap("codex", win(54, 300), None), "rateLimitsByLimitId": None}
        outcome, windows, notes = parse_rate_limits(legacy)
        self.assertEqual(outcome, "ok")
        self.assertEqual(notes, ("legacy_bucket_used",))
        for lid in (None, "other"):
            o, w, n = parse_rate_limits({"rateLimits": snap(lid, win(54, 300), None)})
            self.assertEqual((o, w), ("unavailable", ()), lid)
            self.assertIn("no_common_bucket", n)

    def test_by_id_map_wins_over_legacy_bucket(self):
        obj = {"rateLimits": snap("codex", win(54, 300), None), "rateLimitsByLimitId": {"other": snap("other", win(1, 300), None)}}
        self.assertEqual(parse_rate_limits(obj)[0], "unavailable")

    def test_unknown_duration_window_is_omitted(self):
        for mins in (None, 0, -5, 1.5, "300", True):
            outcome, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(10, mins), win(21, 10080))))
            self.assertEqual(outcome, "ok", mins)
            self.assertEqual([w.duration_seconds for w in windows], [604800])
            self.assertIn("unknown_duration", notes)

    def test_integral_float_duration_accepted(self):
        _, windows, _ = parse_rate_limits(by_id(codex=snap("codex", win(10, 300.0), None)))
        self.assertEqual(windows[0].duration_seconds, 18000)

    def test_bucket_without_usable_windows_is_unavailable(self):
        self.assertEqual(parse_rate_limits(by_id(codex=snap("codex", None, None)))[:2], ("unavailable", ()))
        o, w, n = parse_rate_limits(by_id(codex=snap("codex", win(1, None), None)))
        self.assertEqual((o, w), ("unavailable", ()))

    def test_percent_values_validated_without_clamping(self):
        for v, used in ((0, 0.0), (99.9, 99.9), (100, 100.0)):
            _, windows, _ = parse_rate_limits(by_id(codex=snap("codex", win(v, 300), None)))
            self.assertEqual(windows[0].used_percent, used)
        _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(130, 300), None)))
        self.assertEqual((windows[0].used_percent, windows[0].raw_used_percent), (None, 130))
        self.assertIn("percent_out_of_range", notes)
        for bad in (float("nan"), "5", None, False):
            _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(bad, 300), None)))
            self.assertIsNone(windows[0].used_percent)
            self.assertIsNone(windows[0].raw_used_percent)
            self.assertIn("invalid_percent", notes)

    def test_resets_at_seconds_and_invalid_values(self):
        _, windows, _ = parse_rate_limits(by_id(codex=snap("codex", win(5, 300, None), None)))
        self.assertIsNone(windows[0].resets_at)
        self.assertEqual(windows[0].used_percent, 5.0)
        for bad in ("1767830400", 1767830400000, -1, float("inf"), True):
            _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(5, 300, bad), None)))
            self.assertIsNone(windows[0].used_percent, bad)
            self.assertIn("invalid_reset", notes)
            self.assertNotIn("invalid_percent", notes)

    def test_huge_json_numbers_are_invalid_values_never_errors(self):
        huge = 10 ** 400
        for bad in (huge, -huge, float("1e400"), 2 ** 53 + 1):
            outcome, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(bad, 300), win(1, 10080))))
            self.assertEqual(outcome, "ok", bad)
            self.assertIsNone(windows[0].used_percent)
            self.assertIn("invalid_percent", notes)
            _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(5, 300, bad), None)))
            self.assertIsNone(windows[0].used_percent, bad)
            self.assertIn("invalid_reset", notes)
            _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(5, bad), win(1, 10080))))
            self.assertEqual([w.duration_seconds for w in windows], [604800], bad)
            self.assertIn("unknown_duration", notes)
            for w in windows:
                Window.from_json(w.to_json())
        _, windows, _ = parse_rate_limits(by_id(codex=snap("codex", win(5, float(2 ** 60)), None)))
        self.assertEqual(windows, ())

    def test_unexpected_exception_while_parsing_is_protocol_unsupported(self):
        with mock.patch.object(codex, "_codex_window", side_effect=OverflowError):
            self.assertEqual(parse_rate_limits(by_id(codex=snap("codex", win(1, 300), None))), ("protocol_unsupported", (), ()))

    def test_changed_shapes_are_protocol_unsupported(self):
        for obj in (None, [], {"rateLimitsByLimitId": []}, {"rateLimitsByLimitId": {"codex": 5}},
                    by_id(codex=snap("codex", 42, None)), {"rateLimits": "codex"}):
            self.assertEqual(parse_rate_limits(obj), ("protocol_unsupported", (), ()), obj)

    def test_notes_and_windows_follow_the_contract(self):
        _, windows, notes = parse_rate_limits(by_id(codex=snap("codex", win(130, 300), win(1, None)), other=snap("o", None, None)))
        self.assertTrue(all(is_valid_note(n) for n in notes))
        for w in windows:
            Window.from_json(w.to_json())


class RateLimitErrorTests(unittest.TestCase):
    def test_only_explicit_markers_count_as_rate_limited(self):
        self.assertTrue(_is_rate_limit_error({"code": -32603, "message": "x", "data": {"codexErrorInfo": "rateLimitExceeded"}}))
        self.assertTrue(_is_rate_limit_error({"code": 1, "message": "x", "data": {"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}}))
        for err in (
            {"code": -32600, "message": "codex account authentication required to read rate limits"},
            {"code": 429, "message": "rate limited"},
            {"code": -32603, "message": "x", "data": {"codexErrorInfo": "serverOverloaded"}},
            None,
        ):
            self.assertFalse(_is_rate_limit_error(err), err)


class ServerMessageTests(unittest.TestCase):
    def test_safe_server_request_ids(self):
        for ok in (0, 7, -1, 2 ** 53, -(2 ** 53), "srv-1", "a" * 64, "id.with:colons_and-dash"):
            self.assertTrue(_valid_server_id(ok), ok)
        for bad in (True, False, None, 2 ** 53 + 1, 10 ** 400, 1.0, "", "a" * 65, "has space", "ü", "x\n", {"a": 1}, [1]):
            self.assertFalse(_valid_server_id(bad), bad)

    def test_turn_notifications_and_requests_are_rejected(self):
        for method in ("turn/started", "turn/completed", "item/started", "item/agentMessage/delta", "thread/started",
                       "rawResponseItem/completed", "model/rerouted", "hook/started", "command/exec/outputDelta",
                       "process/exited", "applyPatchApproval", "execCommandApproval"):
            with self.assertRaises(_Violation) as cm:
                _check_message({"method": method, "params": {}})
            self.assertEqual(cm.exception.code, "unexpected_turn", method)
        with self.assertRaises(_Violation):
            _check_message({"id": 3, "method": "item/commandExecution/requestApproval", "params": {}})

    def test_account_notifications_and_safe_requests_are_accepted(self):
        for m in ({"method": "account/updated", "params": {}}, {"method": "remoteControl/status/changed"},
                  {"method": "account/rateLimits/updated"}, {"id": "srv-1", "method": "account/chatgptAuthTokens/refresh"},
                  {"id": 1, "result": {}}, {"id": {"odd": 1}, "result": {}}):
            _check_message(m)

    def test_unsafe_request_id_or_method_is_malformed(self):
        for m in ({"id": {"x": 1}, "method": "account/chatgptAuthTokens/refresh"}, {"id": "x" * 65, "method": "m"},
                  {"method": 5}, {"method": None}):
            with self.assertRaises(_Violation) as cm:
                _check_message(m)
            self.assertEqual(cm.exception.code, "malformed_output", m)


if __name__ == "__main__":
    unittest.main()
