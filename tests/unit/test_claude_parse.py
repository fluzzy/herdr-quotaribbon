"""Parsing of Claude Code ``auth status --json`` and ``get_usage`` (redacted 2.1.316 captures)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from quotaribbon.providers import claude
from quotaribbon.providers.claude import (
    _check_message,
    _init_identity,
    _parse_auth_full,
    parse_auth_status,
    parse_usage_response,
)
from quotaribbon.types import Window, is_valid_note

FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "claude")
SALT = b"\x01" * 32
HOME = os.path.join(tempfile.gettempdir(), "qr-home")
PROFILE = os.path.join(HOME, ".claude")


def _init_gate(body, salt):
    return _init_identity(body, salt)[1]


def load(name: str):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.loads(fh.read().replace("{HOME}", HOME))


def usage_payload(name: str = "get-usage.subscription.stream.json") -> dict:
    """The ``control_response.response.response`` body from a captured stream."""
    for msg in load(name):
        if msg.get("type") == "control_response":
            return msg["response"]["response"]
    raise AssertionError("no control_response in fixture")


def win(util, resets="2026-01-08T00:00:00.500000+00:00"):
    return {"utilization": util, "resets_at": resets, "limit_dollars": None}


def body(five=None, seven=None, **extra) -> dict:
    rl = {"five_hour": five, "seven_day": seven}
    rl.update(extra)
    return {"subscription_type": "pro", "rate_limits_available": True, "rate_limits": rl}


class AuthStatusTests(unittest.TestCase):
    def test_subscription_profile_is_subscription_with_hashed_account(self):
        r = parse_auth_status(load("auth-status.subscription.json"), profile_dir=PROFILE, salt=SALT, env={})
        self.assertEqual(r.state, "subscription")
        self.assertEqual(r.detail, "auth_method:claude.ai")
        self.assertFalse(r.retryable)
        self.assertRegex(r.account_key, r"^acct:[0-9a-f]{24}$")
        self.assertEqual(r.config_dir, os.path.realpath(PROFILE))
        self.assertNotIn("example.invalid", repr(r))
        self.assertNotIn("org-REDACTED", repr(r))

    def test_account_key_depends_on_salt_and_identity(self):
        obj = load("auth-status.subscription.json")
        a = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={}).account_key
        b = parse_auth_status(obj, profile_dir=PROFILE, salt=b"\x02" * 32, env={}).account_key
        other = dict(obj, email="someone-else@example.invalid")
        c = parse_auth_status(other, profile_dir=PROFILE, salt=SALT, env={}).account_key
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, parse_auth_status(copy.deepcopy(obj), profile_dir=PROFILE, salt=SALT, env={}).account_key)

    def test_env_api_key_over_subscription_profile_is_api_auth_and_reports_variable_name(self):
        obj = load("auth-status.subscription-plus-env-api-key.json")
        env = {"ANTHROPIC_API_KEY": "sk-ant-api03-CANARY-SECRET", "HOME": HOME}
        r = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env=env)
        self.assertEqual(r.state, "api_auth")
        self.assertEqual(r.env_conflicts, ("ANTHROPIC_API_KEY",))
        self.assertNotIn("CANARY-SECRET", repr(r))

    def test_non_subscription_logins_are_classified_by_method_without_an_account(self):
        cases = (
            ("auth-status.logged-out.json", "logged_out", "auth_method:none"),
            ("auth-status.api-key.json", "api_auth", None),
            ("auth-status.bedrock.json", "api_auth", "auth_method:third_party"),
            ("auth-status.auth-token.json", "unsupported_auth", "auth_method:oauth_token"),
        )
        for name, state, detail in cases:
            with self.subTest(name):
                obj = load(name)
                r = parse_auth_status(obj, profile_dir=obj["configDirectory"], salt=SALT, env={})
                self.assertEqual(r.state, state)
                self.assertIsNone(r.account_key)
                if detail is not None:
                    self.assertEqual(r.detail, detail)

    def test_unknown_auth_method_is_unsupported_and_sanitised(self):
        obj = dict(load("auth-status.subscription.json"), authMethod="weird method\x1b[31m")
        r = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={})
        self.assertEqual(r.state, "unsupported_auth")
        self.assertEqual(r.detail, "auth_method:other")

    def test_reported_config_directory_must_match_selected_profile(self):
        r = parse_auth_status(load("auth-status.subscription.json"), profile_dir=os.path.join(HOME, "other"), salt=SALT, env={})
        self.assertEqual(r.state, "profile_mismatch")
        self.assertIsNone(r.account_key)

    def test_config_directory_compared_by_realpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.mkdir(real)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            obj = dict(load("auth-status.subscription.json"), configDirectory=link + "/")
            self.assertEqual(parse_auth_status(obj, profile_dir=real, salt=SALT, env={}).state, "subscription")
            rel = dict(obj, configDirectory="./real")
            self.assertEqual(parse_auth_status(rel, profile_dir=real, salt=SALT, env={}, cwd=tmp).state, "subscription")

    def test_missing_config_directory_is_not_accepted_as_subscription(self):
        obj = load("auth-status.subscription.json")
        del obj["configDirectory"]
        self.assertEqual(parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={}).state, "profile_mismatch")

    def test_malformed_auth_status_is_retryable_error(self):
        for obj in (None, [], "x", {"loggedIn": "yes", "authMethod": "claude.ai"}, {"loggedIn": True}):
            r = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={})
            self.assertEqual(r.state, "error", obj)
            self.assertTrue(r.retryable)

    def test_subscription_missing_email_or_org_id_has_no_account_key(self):
        for field in ("email", "orgId"):
            with self.subTest(field):
                obj = load("auth-status.subscription.json")
                del obj[field]
                r = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={})
                self.assertEqual(r.state, "subscription")
                self.assertIsNone(r.account_key)

    def test_account_key_uses_org_id_not_org_name(self):
        obj = load("auth-status.subscription.json")
        k0 = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={}).account_key
        renamed = dict(obj, orgName="Renamed Org")
        other_org = dict(obj, orgId="org-OTHER")
        self.assertEqual(k0, parse_auth_status(renamed, profile_dir=PROFILE, salt=SALT, env={}).account_key)
        self.assertNotEqual(k0, parse_auth_status(other_org, profile_dir=PROFILE, salt=SALT, env={}).account_key)

    def test_unexpected_values_never_raise(self):
        obj = dict(load("auth-status.subscription.json"), configDirectory="/tmp/with\x00nul")
        r = parse_auth_status(obj, profile_dir=PROFILE, salt=SALT, env={})
        self.assertEqual((r.state, r.detail, r.retryable), ("error", "auth_status_unparsable", True))

    def test_env_conflicts_list_only_present_variable_names(self):
        env = {"ANTHROPIC_BASE_URL": "https://proxy.invalid", "CLAUDE_CODE_USE_VERTEX": "", "UNRELATED": "1"}
        r = parse_auth_status(load("auth-status.subscription.json"), profile_dir=PROFILE, salt=SALT, env=env)
        self.assertEqual(r.env_conflicts, ("ANTHROPIC_BASE_URL",))


class InitializeIdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.auth = _parse_auth_full(load("auth-status.subscription.json"), profile_dir=PROFILE, salt=SALT, env={})

    def test_same_email_and_org_name_pass_the_gate(self):
        init = load("initialize.subscription.json")["response"]["response"]
        self.assertIsNotNone(self.auth.gate)
        self.assertEqual(_init_gate(init, SALT), self.auth.gate)

    def test_different_account_fails_the_gate(self):
        init = load("initialize.subscription.json")["response"]["response"]
        init["account"]["email"] = "other@example.invalid"
        self.assertNotEqual(_init_gate(init, SALT), self.auth.gate)

    def test_logged_out_or_api_key_session_never_passes(self):
        for name in ("initialize.logged-out.json", "initialize.api-key.json"):
            self.assertIsNone(_init_gate(load(name)["response"]["response"], SALT), name)

    def test_api_key_source_blocks_even_with_matching_identity(self):
        init = load("initialize.subscription.json")["response"]["response"]
        init["account"]["apiKeySource"] = "ANTHROPIC_API_KEY"
        self.assertIsNone(_init_gate(init, SALT))

    def test_missing_account_fails_closed(self):
        self.assertIsNone(_init_gate({}, SALT))
        self.assertIsNone(_init_gate({"account": {"email": "user@example.invalid"}}, SALT))

    def test_identity_classification_separates_rejected_from_unproven(self):
        init = load("initialize.subscription.json")["response"]["response"]
        self.assertEqual(_init_identity(init, SALT), ("ok", self.auth.gate))
        for name in ("initialize.logged-out.json", "initialize.api-key.json"):
            self.assertEqual(_init_identity(load(name)["response"]["response"], SALT), ("rejected", None), name)
        for body in (None, [], {}, {"account": None}, {"account": {"email": "user@example.invalid"}},
                     {"account": {"organization": "Org"}}, {"account": {"email": "", "organization": "Org"}}):
            self.assertEqual(_init_identity(body, SALT), ("unproven", None), body)


class MessageCheckTests(unittest.TestCase):
    def test_model_turn_messages_are_rejected(self):
        for t in ("assistant", "user", "result", "stream_event"):
            with self.assertRaises(claude._Abort) as cm:
                _check_message({"type": t})
            self.assertEqual((cm.exception.code, cm.exception.note), ("unexpected_turn", "unexpected_turn"))

    def test_other_messages_are_accepted(self):
        for m in ({"type": "system", "subtype": "ui_invalidate"}, {"type": "control_request"}, {"type": "control_response"}, {}):
            _check_message(m)


class UsageResponseTests(unittest.TestCase):
    def test_captured_response_reads_only_named_windows(self):
        outcome, windows, notes = parse_usage_response(usage_payload())
        self.assertEqual(outcome, "ok")
        # five_hour is null in the capture: omitted, never a number or a placeholder.
        self.assertEqual([w.id for w in windows], ["seven_day"])
        (seven,) = windows
        self.assertEqual(seven.used_percent, 40.0)
        self.assertEqual(seven.raw_used_percent, 40)
        self.assertEqual(seven.duration_seconds, 604800)
        self.assertEqual(seven.resets_at, "2026-01-08T00:00:00Z")
        self.assertIn("window_not_provided:five_hour", notes)
        # The unknown bucket at 100 % is ignored and only counted.
        self.assertIn("ignored_model_buckets:1", notes)
        self.assertTrue(all(w.used_percent != 100 for w in windows))
        self.assertTrue(all(is_valid_note(n) for n in notes))

    def test_unknown_bucket_at_100_percent_never_becomes_a_window(self):
        b = body(win(5), win(6), extra_bucket_a=win(100), extra_bucket_b=win(100), seven_day_opus=win(99))
        outcome, windows, notes = parse_usage_response(b)
        self.assertEqual(outcome, "ok")
        self.assertEqual([w.used_percent for w in windows], [5.0, 6.0])
        self.assertIn("ignored_model_buckets:3", notes)

    def test_zero_ninety_nine_point_nine_and_hundred_are_kept_exactly(self):
        for v in (0, 99.9, 100):
            outcome, windows, _ = parse_usage_response(body(win(v), win(v)))
            self.assertEqual(outcome, "ok")
            self.assertEqual([w.used_percent for w in windows], [float(v), float(v)])
            self.assertEqual(windows[0].raw_used_percent, v)

    def test_null_rate_limits_with_available_true_is_transient(self):
        outcome, windows, notes = parse_usage_response({"rate_limits_available": True, "rate_limits": None, "subscription_type": "pro"})
        self.assertEqual((outcome, windows), ("transient_error", ()))
        self.assertEqual(notes, ("rate_limits_null",))

    def test_rate_limits_unavailable_is_unavailable_not_unlimited(self):
        outcome, windows, notes = parse_usage_response(usage_payload("get-usage.logged-out.stream.json"))
        self.assertEqual((outcome, windows), ("unavailable", ()))
        self.assertEqual(notes, ("rate_limits_unavailable",))
        outcome2, _, _ = parse_usage_response(usage_payload("get-usage.subscription-plus-env-api-key.stream.json"))
        self.assertEqual(outcome2, "unavailable")

    def test_only_five_hour_present_omits_seven_day(self):
        outcome, windows, notes = parse_usage_response({"rate_limits_available": True, "rate_limits": {"five_hour": win(32)}})
        self.assertEqual(outcome, "ok")
        self.assertEqual([(w.id, w.used_percent) for w in windows], [("five_hour", 32.0)])
        self.assertIn("window_not_provided:seven_day", notes)

    def test_neither_window_provided_gives_no_windows(self):
        outcome, windows, notes = parse_usage_response(body(None, {"utilization": None, "resets_at": None}))
        self.assertEqual((outcome, windows), ("ok", ()))
        self.assertIn("window_not_provided:five_hour", notes)
        self.assertIn("window_not_provided:seven_day", notes)

    def test_out_of_range_percent_is_not_clamped(self):
        outcome, windows, notes = parse_usage_response(body(win(150), win(-5)))
        self.assertEqual(outcome, "ok")
        self.assertEqual([w.used_percent for w in windows], [None, None])
        self.assertEqual([w.raw_used_percent for w in windows], [150, -5])
        self.assertIn("percent_out_of_range", notes)

    def test_nan_infinite_string_and_bool_percent_are_invalid(self):
        for bad in (float("nan"), float("inf"), "32", True, [32], {"v": 1}):
            outcome, windows, notes = parse_usage_response(body(win(bad), win(10)))
            self.assertEqual(outcome, "ok")
            self.assertIsNone(windows[0].used_percent, bad)
            self.assertIsNone(windows[0].raw_used_percent, bad)
            self.assertIn("invalid_percent", notes)
            self.assertEqual(windows[1].used_percent, 10.0)

    def test_window_with_null_utilization_is_not_provided(self):
        outcome, windows, notes = parse_usage_response(body({"utilization": None, "resets_at": None}, win(10)))
        self.assertEqual(outcome, "ok")
        self.assertEqual([(w.id, w.used_percent) for w in windows], [("seven_day", 10.0)])
        self.assertIn("window_not_provided:five_hour", notes)

    def test_resets_at_formats(self):
        cases = {
            "2026-01-08T00:00:00.500000+00:00": "2026-01-08T00:00:00Z",
            "2026-01-09T00:00:00+00:00": "2026-01-09T00:00:00Z",
            "2026-01-09T09:00:00+09:00": "2026-01-09T00:00:00Z",
            "2026-01-09T00:00:00Z": "2026-01-09T00:00:00Z",
        }
        for raw, iso in cases.items():
            _, windows, _ = parse_usage_response(body(win(10, raw), None))
            self.assertEqual(windows[0].resets_at, iso, raw)

    def test_null_reset_keeps_value(self):
        _, windows, _ = parse_usage_response(body(win(10, None), None))
        self.assertEqual(windows[0].used_percent, 10.0)
        self.assertIsNone(windows[0].resets_at)

    def test_invalid_reset_hides_value(self):
        for bad in ("tomorrow", "2026-01-09 00:00:00", "2026-01-09T00:00:00", 1767830400, ""):
            _, windows, notes = parse_usage_response(body(win(10, bad), None))
            self.assertIsNone(windows[0].used_percent, bad)
            self.assertEqual(windows[0].raw_used_percent, 10)
            self.assertIn("invalid_reset", notes)
            self.assertNotIn("invalid_percent", notes)

    def test_reset_with_non_ascii_digits_is_invalid(self):
        _, windows, notes = parse_usage_response(body(win(10, "２０２６-01-08T00:00:00Z"), None))
        self.assertIsNone(windows[0].used_percent)
        self.assertIn("invalid_reset", notes)

    def test_changed_shapes_are_protocol_unsupported(self):
        bad_bodies = [
            None,
            [],
            {"rate_limits": {}},
            {"rate_limits_available": "yes", "rate_limits": {}},
            {"rate_limits_available": True, "rate_limits": []},
            {"rate_limits_available": True, "rate_limits": {"five_hour": 32}},
            {"rate_limits_available": True, "rate_limits": {"renamed_five_hour": win(1)}},
            {"rate_limits_available": True, "subscription_type": 5, "rate_limits": {"five_hour": win(1)}},
        ]
        for b in bad_bodies:
            self.assertEqual(parse_usage_response(b), ("protocol_unsupported", (), ()), b)

    def test_huge_json_numbers_are_invalid_values_never_errors(self):
        huge = 10 ** 400
        for bad in (huge, -huge, float("1e400"), 2 ** 53 + 1):
            outcome, windows, notes = parse_usage_response(body(win(bad), win(10)))
            self.assertEqual(outcome, "ok", bad)
            self.assertIsNone(windows[0].used_percent)
            self.assertIsNone(windows[0].raw_used_percent)
            self.assertIn("invalid_percent", notes)
            for w in windows:
                Window.from_json(w.to_json())
        _, windows, notes = parse_usage_response(body(win(10, huge), None))
        self.assertIsNone(windows[0].used_percent)
        self.assertIn("invalid_reset", notes)

    def test_huge_number_arriving_as_json_text_is_handled(self):
        text = '{"subscription_type":"pro","rate_limits_available":true,"rate_limits":{"five_hour":{"utilization":1%s,"resets_at":null},"seven_day":null}}' % ("0" * 400)
        outcome, windows, notes = parse_usage_response(json.loads(text))
        self.assertEqual(outcome, "ok")
        self.assertIsNone(windows[0].used_percent)

    def test_unexpected_exception_while_parsing_is_protocol_unsupported(self):
        with mock.patch.object(claude, "_claude_window", side_effect=OverflowError):
            self.assertEqual(parse_usage_response(body(win(1), win(2))), ("protocol_unsupported", (), ()))

    def test_every_window_validates_against_the_contract(self):
        for b in (usage_payload(), body(win(0), win(100)), body(win(150), None)):
            _, windows, _ = parse_usage_response(b)
            for w in windows:
                Window.from_json(w.to_json())


class ManagedPolicyTests(unittest.TestCase):
    def test_absent_managed_settings_allow_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(claude.managed_policy_present(tmp))
            self.assertFalse(claude.managed_policy_present(os.path.join(tmp, "missing")))
            os.mkdir(os.path.join(tmp, "managed-settings.d"))
            self.assertFalse(claude.managed_policy_present(tmp))

    def test_managed_settings_file_or_drop_in_blocks_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "managed-settings.json"), "w") as fh:
                fh.write('{"hooks": {}}')
            self.assertTrue(claude.managed_policy_present(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "managed-settings.d"))
            with open(os.path.join(tmp, "managed-settings.d", "10-org.json"), "w") as fh:
                fh.write("{}")
            self.assertTrue(claude.managed_policy_present(tmp))

    def test_default_dir_is_platform_specific(self):
        self.assertTrue(os.path.isabs(claude.default_managed_settings_dir()))


if __name__ == "__main__":
    unittest.main()
