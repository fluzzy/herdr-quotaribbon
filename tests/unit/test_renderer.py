"""Renderer: display rules, freshness, identity checks, and the lean tab-bar tick."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from quotaribbon import binding, cache, renderer, scheduler
from quotaribbon import constants as C
from quotaribbon import settings as settings_mod
from quotaribbon import types as t
from tests.helpers.core_fakes import ACCT_A, ACCT_B, T0, temp_paths, window
from tests.helpers.core_records import GEN, conn, pview, settings, snap, stat, state

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
E = "…"
BOTH = "Claude 5h 32% | 7d 68%  ·  Codex 5h 54% | 7d 21%"


def render(view, s=None, now=T0, max_wall_seen=None):
    v = view.to_json()
    v["max_wall_seen"] = max_wall_seen
    return renderer.render_dicts((settings() if s is None else s).to_json(), v, now)


class LayoutTests(unittest.TestCase):
    def test_both_connected_and_on(self):
        self.assertEqual(render(state(pview("claude"), pview("codex"))), BOTH)

    def test_a_hidden_provider_leaves_no_name_or_separator(self):
        cases = (
            ("codex not connected", state(pview("claude"), pview("codex", connection=None)), settings(),
             "Claude 5h 32% | 7d 68%"),
            ("claude not connected", state(pview("claude", connection=None), pview("codex")), settings(),
             "Codex 5h 54% | 7d 21%"),
            ("codex off", state(pview("claude"), pview("codex")), settings(codex=False), "Claude 5h 32% | 7d 68%"),
        )
        for label, view, s, line in cases:
            with self.subTest(label):
                self.assertEqual(render(view, s), line)

    def test_nothing_connected_or_everything_off_prints_empty(self):
        self.assertEqual(render(state(pview("claude", connection=None), pview("codex", connection=None))), "")
        self.assertEqual(render(state(pview("claude"), pview("codex")), settings(False, False)), "")
        self.assertEqual(render(state()), "")

    def test_invalid_settings_print_empty(self):
        self.assertEqual(renderer.render_dicts(None, state(pview("claude")).to_json(), T0), "")

    def test_never_connected_record_is_hidden_even_by_name(self):
        v = state(pview("claude", connection=conn(state="connected", ever_connected=False)))
        self.assertEqual(render(v), "")

    def test_hidden_states(self):
        for st in ("logged_out", "api_auth", "unsupported_auth", "profile_mismatch", "cli_missing", "not_connected", "unverified"):
            with self.subTest(st):
                v = state(pview("claude", connection=conn(state=st)), pview("codex"))
                self.assertEqual(render(v), "Codex 5h 54% | 7d 21%")
                self.assertEqual(renderer.hidden_reason("claude", settings(), v), f"state:{st}")

    def test_hidden_reasons(self):
        v = state(pview("claude"), pview("codex", connection=None))
        self.assertIsNone(renderer.hidden_reason("claude", settings(), v))
        self.assertEqual(renderer.hidden_reason("claude", settings(claude=False), v), "disabled")
        self.assertEqual(renderer.hidden_reason("codex", settings(), v), "never_connected")
        self.assertEqual(renderer.hidden_reason("claude", None, v), "settings_invalid")

    def test_missing_canonical_window_shows_dashes(self):
        w = [window("five_hour", 18000, 32.0), window("seven_day", 604800, None)]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w)))), "Claude 5h 32% | 7d --")

    def test_actual_window_durations_are_used_as_labels(self):
        w = [window("primary", 900, 25.0)]
        self.assertEqual(render(state(None, pview("codex", snapshot=snap("codex", windows=w)))), "Codex 15m 25%")
        w = [window("primary", 900, 25.0), window("secondary", 3600, 40.5)]
        self.assertEqual(render(state(None, pview("codex", snapshot=snap("codex", windows=w)))), "Codex 15m 25% | 1h 40%")

    def test_percentages_are_truncated_and_zero_is_valid(self):
        for used, text in ((0.0, "0%"), (99.9, "99%"), (100.0, "100%"), (0.4, "0%")):
            with self.subTest(used):
                w = [window("five_hour", 18000, used)]
                self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w)))), f"Claude 5h {text}")

    def test_snapshot_without_renderable_windows_shows_dashes(self):
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=[])))), "Claude --")
        no_duration = [window("five_hour", None, 10.0)]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=no_duration)))), "Claude --")

    def test_render_provider_returns_one_segment(self):
        v = state(pview("claude"), pview("codex")).to_json()
        self.assertEqual(renderer.render_provider_dicts("codex", settings().to_json(), v, T0), "Codex 5h 54% | 7d 21%")
        self.assertIsNone(renderer.render_provider_dicts("codex", settings(codex=False).to_json(), v, T0))

    def test_output_is_one_sanitised_line_of_at_most_200_chars(self):
        self.assertEqual(renderer.sanitize_line("a\nb\x1b[31mc d\x85"), "ab[31mcd")
        self.assertEqual(len(renderer.sanitize_line("x" * 500)), 200)

    def test_render_never_raises(self):
        self.assertEqual(renderer.render_dicts(settings().to_json(), object(), T0), "")
        self.assertEqual(renderer.render_dicts(settings().to_json(), {"providers": {"claude": {"connection": 5}}}, T0), "")


class VerificationTests(unittest.TestCase):
    def test_new_server_epoch_shows_ellipsis_until_reverified(self):
        v = state(pview("claude"), pview("codex"), epoch="epoch-2")
        self.assertEqual(render(v), f"Claude {E}  ·  Codex {E}")

    def test_missing_epoch_metadata_fails_closed(self):
        self.assertEqual(render(state(pview("claude"), epoch=None)), f"Claude {E}")
        self.assertEqual(render(state(pview("claude", connection=conn(verified_server_epoch=None)))), f"Claude {E}")
        self.assertEqual(render(state(pview("claude", connection=conn(verified_at=None)))), f"Claude {E}")

    def test_generation_change_after_off_on_shows_ellipsis(self):
        self.assertEqual(render(state(pview("claude", generation=GEN + 1))), f"Claude {E}")

    def test_settings_fingerprint_mismatch_shows_ellipsis(self):
        s = settings()
        v = state(pview("claude")).to_json()
        v["max_wall_seen"] = None
        fps = settings_mod.settings_fingerprints(s)
        v["providers"]["claude"]["settings_fingerprint"] = fps["claude"]
        self.assertEqual(renderer.render_dicts(s.to_json(), v, T0, fps), "Claude 5h 32% | 7d 68%")
        v["providers"]["claude"]["settings_fingerprint"] = "something-else"
        self.assertEqual(renderer.render_dicts(s.to_json(), v, T0, fps), f"Claude {E}")

    def test_unsupported_protocol_shows_dashes(self):
        v = state(pview("claude", status=stat(data_state="protocol_unsupported")))
        self.assertEqual(render(v), "Claude --")


class SnapshotValidityTests(unittest.TestCase):
    def test_first_collection_in_progress_or_never_attempted_shows_ellipsis(self):
        self.assertEqual(render(state(pview("claude", snapshot=None, status=None))), f"Claude {E}")
        st = stat(attempt_started_at=T0 - 5, attempt_finished_at=None, last_success_at=None)
        self.assertEqual(render(state(pview("claude", snapshot=None, status=st))), f"Claude {E}")

    def test_no_snapshot_after_an_attempt_shows_dashes(self):
        self.assertEqual(render(state(pview("claude", snapshot=None))), "Claude --")
        stuck = stat(attempt_started_at=T0 - C.LEASE_TTL - 1, attempt_finished_at=None)
        self.assertEqual(render(state(pview("claude", snapshot=None, status=stuck))), "Claude --")

    def test_snapshot_from_another_account_or_auth_epoch_is_rejected(self):
        for name, sn in {
            "auth_epoch": snap(auth_epoch=1),
            "account": snap(account_key=ACCT_B),
            "profile": snap(profile_key="other"),
            "no_account": snap(account_key=None),
        }.items():
            with self.subTest(name):
                self.assertEqual(render(state(pview("claude", snapshot=sn))), "Claude --")

    def test_account_change_on_same_profile_never_shows_old_numbers(self):
        c = conn(account_key=ACCT_B, auth_epoch=3)
        self.assertEqual(render(state(pview("claude", connection=c, snapshot=snap(account_key=ACCT_A, auth_epoch=2)))), "Claude --")

    def test_unproven_identity_requires_same_epoch_and_fetch_after_verification(self):
        c = conn(account_key=None, verified_at=T0 - 100)
        ok = snap(account_key=None, age=50)
        self.assertEqual(render(state(pview("claude", connection=c, snapshot=ok))), "Claude 5h 32% | 7d 68%")
        cases = {
            "fetched_before_verification": snap(account_key=None, age=150),
            "other_epoch": snap(account_key=None, age=50, server_epoch="epoch-0"),
            "no_epoch": snap(account_key=None, age=50, server_epoch=None),
            "identified_snapshot": snap(account_key=ACCT_A, age=50),
        }
        for name, sn in cases.items():
            with self.subTest(name):
                self.assertEqual(render(state(pview("claude", connection=c, snapshot=sn))), "Claude --")

    def test_freshness_boundaries(self):
        def line(age):
            return render(state(pview("claude", snapshot=snap(age=age))))

        self.assertEqual(line(299), "Claude 5h 32% | 7d 68%")
        self.assertEqual(line(300), "Claude 5h 32% | 7d 68% [stale 5m]")
        self.assertEqual(line(899.5), "Claude 5h 32% | 7d 68% [stale 14m]")
        self.assertEqual(line(900), "Claude --")  # hidden once the age reaches the cutoff

    def test_custom_freshness_settings_apply(self):
        s = settings(interval_seconds=60, stale_after_seconds=120, hide_values_after_seconds=200)
        self.assertEqual(render(state(pview("claude", snapshot=snap(age=130))), s), "Claude 5h 32% | 7d 68% [stale 2m]")
        self.assertEqual(render(state(pview("claude", snapshot=snap(age=201))), s), "Claude --")

    def test_failure_after_the_snapshot_marks_it_stale(self):
        st = stat(last_failure_at=T0 - 10)
        v = state(pview("claude", snapshot=snap(age=240), status=st), pview("codex"))
        self.assertEqual(render(v), "Claude 5h 32% | 7d 68% [stale 4m]  ·  Codex 5h 54% | 7d 21%")
        st_old = stat(last_failure_at=T0 - 300)
        self.assertEqual(render(state(pview("claude", snapshot=snap(age=240), status=st_old))), "Claude 5h 32% | 7d 68%")

    def test_future_snapshot_is_invalid(self):
        self.assertEqual(render(state(pview("claude", snapshot=snap(age=-1)))), "Claude --")
        self.assertEqual(render(state(pview("claude", snapshot=snap(age=-0.001)))), "Claude --")

    def test_passed_reset_shows_dashes_for_that_window_only(self):
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 - 1)), window("seven_day", 604800, 68.0, t.iso_utc(T0 + 3600))]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w)))), "Claude 5h -- | 7d 68% (1h)")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0))]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w)))), "Claude 5h --")

    def test_remaining_mode_shows_what_is_left_rounded_down(self):
        rem = settings_mod.parse_settings({"schema_version": 1, "display": {"mode": "remaining"},
                                           "providers": {"claude": {"enabled": True}, "codex": {"enabled": False}}})
        w = [window("five_hour", 18000, 17.4), window("seven_day", 604800, 100.0)]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w))), rem), "Claude 5h 82% | 7d 0%")
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w)))), "Claude 5h 17% | 7d 100%")
        w = [window("five_hour", 18000, 0.0), window("seven_day", 604800, 99.9)]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w))), rem), "Claude 5h 100% | 7d 0%")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 - 1)), window("seven_day", 604800, 17.0, t.iso_utc(T0 + 3600))]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w))), rem), "Claude 5h -- | 7d 83% (1h)")
        self.assertEqual(render(state(pview("claude"), pview("codex")), settings()), BOTH)

    def test_window_value_modes(self):
        w = {"used_percent": 17.4, "resets_at": None}
        self.assertEqual(renderer._window_value(w, T0), "17%")
        self.assertEqual(renderer._window_value(w, T0, "used"), "17%")
        self.assertEqual(renderer._window_value(w, T0, "remaining"), "82%")
        self.assertEqual(renderer._window_value({"used_percent": None}, T0, "remaining"), "--")
        self.assertEqual(renderer._window_value({"used_percent": 101.0}, T0, "remaining"), "--")

    def test_wall_clock_rollback_invalidates_every_snapshot(self):
        v = state(pview("claude"), pview("codex"))
        self.assertEqual(render(v, max_wall_seen=T0 + 5), BOTH)
        self.assertEqual(render(v, max_wall_seen=T0 + 5.5), "Claude --  ·  Codex --")


def compact(claude: bool = True, codex: bool = True, mode: str = "used", **refresh) -> t.Settings:
    obj = {"schema_version": 1, "display": {"mode": mode, "format": "compact"},
           "providers": {"claude": {"enabled": claude}, "codex": {"enabled": codex}}}
    if refresh:
        obj["refresh"] = refresh
    return t.Settings.from_json(obj)


class CompactFormatTests(unittest.TestCase):
    def test_default_full_output_is_unchanged(self):
        v = state(pview("claude"), pview("codex"))
        self.assertEqual(render(v), "Claude 5h 32% | 7d 68%  ·  Codex 5h 54% | 7d 21%")
        explicit = t.Settings.from_json({"schema_version": 1, "display": {"format": "full"},
                                         "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}})
        self.assertEqual(render(v, explicit), render(v))

    def test_both_providers_both_windows(self):
        self.assertEqual(render(state(pview("claude"), pview("codex")), compact()), "C 5h 32% 7d 68% · X 5h 54% 7d 21%")

    def test_one_window_each(self):
        cw = [window("seven_day", 604800, 17.0)]
        xw = [window("secondary", 604800, 6.0)]
        v = state(pview("claude", snapshot=snap(windows=cw)), pview("codex", snapshot=snap("codex", windows=xw)))
        self.assertEqual(render(v, compact()), "C 7d 17% · X 7d 6%")
        self.assertEqual(render(v), "Claude 7d 17%  ·  Codex 7d 6%")

    def test_single_provider_has_no_outer_separator(self):
        self.assertEqual(render(state(pview("claude"), pview("codex")), compact(codex=False)), "C 5h 32% 7d 68%")
        self.assertEqual(render(state(None, pview("codex")), compact()), "X 5h 54% 7d 21%")

    def test_ellipsis(self):
        self.assertEqual(render(state(pview("claude", snapshot=None, status=None)), compact(codex=False)), f"C {E}")
        self.assertEqual(render(state(pview("claude", generation=GEN + 1), pview("codex", generation=GEN + 1)), compact()),
                         f"C {E} · X {E}")

    def test_dashes(self):
        self.assertEqual(render(state(pview("claude", snapshot=None)), compact(codex=False)), "C --")
        v = state(pview("claude", status=stat(data_state="protocol_unsupported")))
        self.assertEqual(render(v, compact(codex=False)), "C --")
        self.assertEqual(render(state(pview("claude"), pview("codex")), compact(), max_wall_seen=T0 + 5.5), "C -- · X --")
        w = [window("five_hour", 18000, 32.0), window("seven_day", 604800, None)]
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=w))), compact(codex=False)), "C 5h 32% 7d --")

    def test_stale(self):
        st = stat(last_failure_at=T0 - 10)
        v = state(pview("claude", snapshot=snap(age=240), status=st), pview("codex"))
        self.assertEqual(render(v, compact()), "C 5h 32% 7d 68% [stale 4m] · X 5h 54% 7d 21%")

    def test_failing(self):
        st = stat(data_state="transient_error", last_success_at=T0 - 720, last_failure_at=T0 - 5, consecutive_failures=3)
        v = state(pview("claude", snapshot=None, status=st))
        self.assertEqual(render(v, compact(codex=False)), "C -- [failing 12m]")

    def test_remaining_and_compact_combine(self):
        w = [window("five_hour", 18000, 17.4), window("seven_day", 604800, 100.0)]
        v = state(pview("claude", snapshot=snap(windows=w)), pview("codex"))
        self.assertEqual(render(v, compact(mode="remaining")), "C 5h 82% 7d 0% · X 5h 46% 7d 79%")

    def test_render_provider_segment_is_compact(self):
        v = state(pview("claude"), pview("codex")).to_json()
        self.assertEqual(renderer.render_provider_dicts("codex", compact().to_json(), v, T0), "X 5h 54% 7d 21%")

    def test_compact_render_never_raises(self):
        self.assertEqual(renderer.render_dicts(compact().to_json(), object(), T0), "")
        odd = {**settings().to_json(), "display": "compact"}  # malformed dict never reaches here, but must not raise
        self.assertEqual(renderer.render_dicts(odd, state(pview("claude")).to_json(), T0), "")


class TickTests(unittest.TestCase):
    """The tab-bar tick reads files only through the lean dict layer."""

    def setUp(self):
        self.paths = temp_paths(self)
        self.paths.ensure_private_dirs()
        self.socket = os.path.join(self.paths.state_dir, "herdr.sock")
        with open(self.socket, "w"):
            pass
        self.env = {"HOME": self.paths.state_dir, "CLAUDE_CONFIG_DIR": "/profiles/claude", "HERDR_CONFIG_PATH": "/cfg/config.toml",
                    "HERDR_SOCKET_PATH": self.socket}

    def populate(self):
        s = t.Settings.default()
        fps = settings_mod.settings_fingerprints(s)
        gens = cache.sync_fingerprints(self.paths, fps)
        epoch = cache.rotate_server_epoch(self.paths, now=T0 - 1000)
        b = binding.binding_for("claude", s, self.env)
        path, ident = binding.server_identity(self.env)
        c = conn(binding_key=b.binding_key, profile_key=b.profile_key, verified_server_epoch=epoch,
                 verified_generation=gens["claude"], verified_servers={path: ident})
        cache.write_connection(self.paths, c)
        cache.write_status(self.paths, stat(), b.binding_key)
        cache.write_snapshot(self.paths, snap(profile_key=b.profile_key, server_epoch=epoch), b.binding_key)
        return b

    def test_tick_renders_from_files_and_reports_due_work(self):
        self.populate()
        res = renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)
        self.assertEqual(res["line"], "Claude 5h 32% | 7d 68%")
        self.assertEqual(res["settings_source"], "default")
        self.assertEqual(res["due"], {"claude": None, "codex": "never_verified"})
        self.assertTrue(res["dispatch"])

    def test_tick_in_another_environment_sees_nothing(self):
        self.populate()
        env = dict(self.env, HERDR_CONFIG_PATH="/cfg/other.toml")
        self.assertEqual(renderer.tick(self.paths.config_dir, self.paths.state_dir, env, T0)["line"], "")

    def test_off_hides_at_the_next_tick(self):
        self.populate()
        settings_mod.set_enabled(self.paths, "claude", False)
        res = renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)
        self.assertEqual(res["line"], "")
        self.assertIsNone(res["due"].get("claude"))

    def test_direct_settings_edit_shows_ellipsis_until_refresh(self):
        self.populate()
        with open(self.paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "providers": {"claude": {"enabled": True, "binary": "/x/claude"}, "codex": {"enabled": True}}}, fh)
        res = renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)
        self.assertEqual(res["line"], f"Claude {E}")
        self.assertEqual(res["due"]["claude"], "reverify")

    def test_corrupt_state_never_produces_numbers(self):
        b = self.populate()
        with open(self.paths.snapshot("claude", b.binding_key), "w") as fh:
            fh.write('{"schema_version": 1, "windows": [NaN]}')
        self.assertEqual(renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)["line"], "Claude --")
        with open(self.paths.control, "w") as fh:
            fh.write("[]")
        self.assertEqual(renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)["line"], f"Claude {E}")

    def test_restarted_server_shows_no_numbers_until_verified_again(self):
        # The startup hook never ran (same epoch), but the socket is a new one.
        self.populate()
        os.unlink(self.socket)
        with open(self.socket, "w") as fh:
            fh.write("new server")
        res = renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)
        self.assertEqual(res["line"], f"Claude {E}")
        self.assertEqual(res["due"]["claude"], "reverify")

    def test_missing_socket_shows_no_numbers(self):
        self.populate()
        env = dict(self.env)
        del env["HERDR_SOCKET_PATH"]
        self.assertEqual(renderer.tick(self.paths.config_dir, self.paths.state_dir, env, T0)["line"], f"Claude {E}")

    def test_other_verified_sessions_do_not_evict_each_other(self):
        b = self.populate()
        c = cache.read_connection(self.paths, "claude", b.binding_key)
        c.verified_servers = {"/tmp/other-session.sock": "1:2:3", **c.verified_servers}
        cache.write_connection(self.paths, c)
        self.assertEqual(renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0)["line"],
                         "Claude 5h 32% | 7d 68%")

    def test_dispatch_is_not_suggested_while_a_lease_is_held(self):
        self.populate()
        self.assertIsNotNone(scheduler.acquire_dispatch_at(self.paths.state_dir, T0))
        self.assertFalse(renderer.tick(self.paths.config_dir, self.paths.state_dir, self.env, T0 + 1)["dispatch"])

    def test_tick_imports_no_heavy_modules(self):
        self.populate()
        code = (
            "import sys, json\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from quotaribbon import renderer, scheduler\n"
            "env = json.loads(sys.argv[4])\n"
            "res = renderer.tick(sys.argv[2], sys.argv[3], env, float(sys.argv[5]))\n"
            "lease = scheduler.acquire_dispatch_at(sys.argv[3], float(sys.argv[5]))\n"
            "forbidden = {'dataclasses', 'logging', 'logging.handlers', 'threading', 'tempfile', 'pathlib',\n"
            "             'subprocess', 'uuid', 'hashlib', 'typing', 'shlex', 'quotaribbon.types', 'quotaribbon.paths',\n"
            "             'quotaribbon.fsutil', 'quotaribbon.log'}\n"
            "print(json.dumps({'line': res['line'], 'lease': bool(lease), 'loaded': sorted(forbidden & set(sys.modules))}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code, REPO_ROOT, self.paths.config_dir, self.paths.state_dir, json.dumps(self.env), str(T0)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        got = json.loads(out.stdout)
        self.assertEqual(got["line"], "Claude 5h 32% | 7d 68%")
        self.assertTrue(got["lease"])
        self.assertEqual(got["loaded"], [])


def with_reset(fmt: str = "full", mode: str = "used", claude: bool = True, codex: bool = True) -> t.Settings:
    return t.Settings.from_json({"schema_version": 1, "display": {"mode": mode, "format": fmt, "show_reset": True},
                                 "providers": {"claude": {"enabled": claude}, "codex": {"enabled": codex}}})


DAY, HOUR = 86400, 3600


class ShowResetTests(unittest.TestCase):
    def one_window_each(self, claude_left=6 * DAY + 5, codex_left=6 * DAY + 3 * HOUR):
        cw = [window("seven_day", 604800, 18.0, t.iso_utc(T0 + claude_left))]
        xw = [window("secondary", 604800, 9.0, t.iso_utc(T0 + codex_left))]
        return state(pview("claude", snapshot=snap(windows=cw)), pview("codex", snapshot=snap("codex", windows=xw)))

    def test_label_units_and_boundaries(self):
        cases = ((0.4, "1m"), (30, "1m"), (59, "1m"), (60, "1m"), (119, "1m"), (120, "2m"), (HOUR - 1, "59m"),
                 (HOUR, "1h"), (HOUR + 59, "1h"), (HOUR + 60, "1h 1m"), (5 * HOUR + 30, "5h"),
                 (5 * HOUR + 12 * 60, "5h 12m"), (5 * HOUR + 12 * 60 + 59, "5h 12m"), (DAY - 60, "23h 59m"),
                 (DAY - 1, "23h 59m"), (DAY, "1d"), (DAY + 5 * HOUR, "1d"), (2 * DAY - 1, "1d"),
                 (6 * DAY + 23 * HOUR, "6d"))
        for secs, label in cases:
            with self.subTest(secs):
                self.assertEqual(renderer.reset_label(secs), label)

    def test_suffix_is_none_when_unknown_past_or_invalid(self):
        for resets in (None, t.iso_utc(T0), t.iso_utc(T0 - 1), "soon", "", 5, "2027-13-99T00:00:00Z"):
            with self.subTest(resets):
                self.assertIsNone(renderer._reset_suffix({"duration_seconds": 604800, "resets_at": resets}, T0))
        self.assertEqual(renderer._reset_suffix({"duration_seconds": 604800, "resets_at": t.iso_utc(T0 + 30)}, T0), "1m")

    def test_suffix_bounded_by_window_duration_plus_tolerance(self):
        week = 604800
        inside = {"duration_seconds": week, "resets_at": t.iso_utc(T0 + week + HOUR)}
        self.assertEqual(renderer._reset_suffix(inside, T0), "7d")
        beyond = {"duration_seconds": week, "resets_at": t.iso_utc(T0 + week + HOUR + 1)}
        self.assertIsNone(renderer._reset_suffix(beyond, T0))
        far = {"duration_seconds": week, "resets_at": t.iso_utc(T0 + 2_900_000 * DAY)}
        self.assertIsNone(renderer._reset_suffix(far, T0))
        for d in (None, 0, -5, True, 1.5, "7d"):
            with self.subTest(d):
                self.assertIsNone(renderer._reset_suffix({"duration_seconds": d, "resets_at": t.iso_utc(T0 + 60)}, T0))
        self.assertIsNone(renderer._reset_suffix({"resets_at": t.iso_utc(T0 + 60)}, T0))

    def test_far_future_reset_renders_without_suffix(self):
        w = [window("seven_day", 604800, 68.0, t.iso_utc(T0 + 2_900_000 * DAY))]
        v = state(pview("claude", snapshot=snap(windows=w))).to_json()
        self.assertEqual(renderer.render_dicts(with_reset(codex=False).to_json(), v, T0), "Claude 7d 68%")

    def test_unknown_value_with_future_reset_still_shows_time_left(self):
        w = [window("seven_day", 604800, None, t.iso_utc(T0 + 2 * DAY + 5))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 7d -- (2d)")
        self.assertEqual(render(v, with_reset("compact", codex=False)), "C 7d -- (2d)")
        self.assertEqual(render(v, with_reset(mode="remaining", codex=False)), "Claude 7d -- (2d)")
        self.assertEqual(render(v, with_reset("compact", "remaining", codex=False)), "C 7d -- (2d)")

    def test_full_and_compact_one_window_each(self):
        v = self.one_window_each()
        self.assertEqual(render(v, with_reset()), "Claude 7d 18% (6d)  ·  Codex 7d 9% (6d)")
        self.assertEqual(render(v, with_reset("compact")), "C 7d 18% (6d) · X 7d 9% (6d)")

    def test_both_windows(self):
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 + 3 * HOUR + 59 * 60)),
             window("seven_day", 604800, 68.0, t.iso_utc(T0 + 6 * DAY + 1))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 5h 32% (3h 59m) | 7d 68% (6d)")
        self.assertEqual(render(v, with_reset("compact", codex=False)), "C 5h 32% (3h 59m) 7d 68% (6d)")
        self.assertEqual(render(v, with_reset(mode="remaining", codex=False)), "Claude 5h 68% (3h 59m) | 7d 32% (6d)")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 + 5 * HOUR - 30)),
             window("seven_day", 604800, 68.0, t.iso_utc(T0 + 42 * 60 + 59))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 5h 32% (4h 59m) | 7d 68% (42m)")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 + 4 * HOUR + 30)),
             window("seven_day", 604800, 68.0, t.iso_utc(T0 + DAY + 5 * HOUR))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset("compact", codex=False)), "C 5h 32% (4h) 7d 68% (1d)")

    def test_missing_or_past_reset_gets_no_suffix(self):
        w = [window("five_hour", 18000, 32.0), window("seven_day", 604800, 68.0, t.iso_utc(T0 + 2 * DAY))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 5h 32% | 7d 68% (2d)")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 - 60)), window("seven_day", 604800, 68.0, t.iso_utc(T0 + 90))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 5h -- | 7d 68% (1m)")
        self.assertEqual(render(v, with_reset("compact", codex=False)), "C 5h -- 7d 68% (1m)")

    def test_invalid_reset_never_raises_and_adds_nothing(self):
        w = [window("seven_day", 604800, 68.0, t.iso_utc(T0 + DAY))]
        v = state(pview("claude", snapshot=snap(windows=w))).to_json()
        v["providers"]["claude"]["snapshot"]["windows"][0]["resets_at"] = "garbage"
        self.assertEqual(renderer.render_dicts(with_reset(codex=False).to_json(), v, T0), "Claude 7d --")

    def test_remaining_mode_with_reset(self):
        v = self.one_window_each()
        self.assertEqual(render(v, with_reset(mode="remaining")), "Claude 7d 82% (6d)  ·  Codex 7d 91% (6d)")
        self.assertEqual(render(v, with_reset("compact", "remaining")), "C 7d 82% (6d) · X 7d 91% (6d)")

    def test_stale_suffix_stays_after_the_windows(self):
        cw = [window("seven_day", 604800, 18.0, t.iso_utc(T0 + 2 * DAY))]
        v = state(pview("claude", snapshot=snap(age=400, windows=cw)))
        self.assertEqual(render(v, with_reset(codex=False)), "Claude 7d 18% (2d) [stale 6m]")

    def test_on_by_default_and_explicit_false_restores_the_plain_line(self):
        v = self.one_window_each()
        off = t.Settings.from_json({"schema_version": 1, "display": {"show_reset": False},
                                    "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}})
        self.assertEqual(render(v), "Claude 7d 18% (6d)  ·  Codex 7d 9% (6d)")
        self.assertEqual(render(v), render(v, with_reset()))
        self.assertEqual(render(v, off), "Claude 7d 18%  ·  Codex 7d 9%")
        self.assertEqual(render(v, compact()), "C 7d 18% (6d) · X 7d 9% (6d)")
        self.assertEqual(render(v, compact(mode="remaining")), "C 7d 82% (6d) · X 7d 91% (6d)")
        self.assertEqual(render(state(pview("claude"), pview("codex"))), BOTH)  # no reset times, no suffix
        missing = state(pview("claude", snapshot=snap(windows=[window("seven_day", 604800, 18.0)]))).to_json()
        settings_dict = settings(codex=False).to_json()
        del settings_dict["display"]["show_reset"]
        self.assertEqual(renderer.render_dicts(settings_dict, missing, T0), "Claude 7d 18%")
        cw = [window("seven_day", 604800, 18.0, t.iso_utc(T0 + 2 * DAY))]
        stale = state(pview("claude", snapshot=snap(age=400, windows=cw)))
        self.assertEqual(render(stale, settings(codex=False)), "Claude 7d 18% (2d) [stale 6m]")

    def test_fallback_from_a_file_form_last_good_shows_the_reset_suffix(self):
        paths = temp_paths(self)
        os.makedirs(paths.config_dir)
        os.makedirs(paths.state_dir)
        with open(paths.settings, "w") as fh:
            json.dump({"schema_version": 1, "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}}, fh)
        settings_mod.load(paths, update_last_good=True)
        with open(paths.settings_last_good) as fh:
            self.assertNotIn("show_reset", json.load(fh).get("display", {}))
        with open(paths.settings, "w") as fh:
            fh.write("{not json")
        v = self.one_window_each().to_json()
        v["max_wall_seen"] = None
        tick_settings, source = settings_mod.load_tick(paths.config_dir, paths.state_dir)
        self.assertEqual(source, "last_good")
        self.assertEqual(renderer.render_dicts(tick_settings, v, T0), "Claude 7d 18% (6d)  ·  Codex 7d 9% (6d)")
        loaded = settings_mod.load(paths, update_last_good=False)
        self.assertEqual(loaded.source, "last_good")
        self.assertEqual(renderer.render_dicts(loaded.settings.to_json(), v, T0),
                         "Claude 7d 18% (6d)  ·  Codex 7d 9% (6d)")


def labels_off(fmt: str = "full", mode: str = "used", show_reset: bool | None = False, codex: bool = True) -> t.Settings:
    display = {"mode": mode, "format": fmt, "window_labels": False}
    if show_reset is not None:  # None leaves the key out so the default applies
        display["show_reset"] = show_reset
    return t.Settings.from_json({"schema_version": 1, "display": display,
                                 "providers": {"claude": {"enabled": True}, "codex": {"enabled": codex}}})


class WindowLabelsTests(unittest.TestCase):
    def one_window_each(self):
        cw = [window("seven_day", 604800, 19.0, t.iso_utc(T0 + 6 * DAY + 5))]
        xw = [window("secondary", 604800, 13.0, t.iso_utc(T0 + 6 * DAY + 3 * HOUR))]
        return state(pview("claude", snapshot=snap(windows=cw)), pview("codex", snapshot=snap("codex", windows=xw)))

    def test_single_window_drops_its_label(self):
        v = self.one_window_each()
        self.assertEqual(render(v, labels_off(show_reset=True)), "Claude 19% (6d)  ·  Codex 13% (6d)")
        self.assertEqual(render(v, labels_off("compact", show_reset=True)), "C 19% (6d) · X 13% (6d)")
        self.assertEqual(render(v, labels_off()), "Claude 19%  ·  Codex 13%")
        self.assertEqual(render(v, labels_off("compact")), "C 19% · X 13%")
        self.assertEqual(render(v, labels_off(show_reset=None)), "Claude 19% (6d)  ·  Codex 13% (6d)")
        self.assertEqual(render(v, labels_off("compact", show_reset=None)), "C 19% (6d) · X 13% (6d)")

    def test_remaining_mode(self):
        v = self.one_window_each()
        self.assertEqual(render(v, labels_off(mode="remaining", show_reset=True)), "Claude 81% (6d)  ·  Codex 87% (6d)")
        self.assertEqual(render(v, labels_off("compact", "remaining")), "C 81% · X 87%")

    def test_two_windows_keep_their_labels(self):
        v = state(pview("claude"), pview("codex"))
        self.assertEqual(render(v, labels_off()), BOTH)
        self.assertEqual(render(v, labels_off("compact")), "C 5h 32% 7d 68% · X 5h 54% 7d 21%")
        w = [window("five_hour", 18000, 32.0, t.iso_utc(T0 + 3 * HOUR + 59 * 60)),
             window("seven_day", 604800, 68.0, t.iso_utc(T0 + 6 * DAY + 1))]
        v = state(pview("claude", snapshot=snap(windows=w)))
        self.assertEqual(render(v, labels_off(show_reset=True, codex=False)), "Claude 5h 32% (3h 59m) | 7d 68% (6d)")

    def test_mixed_providers_decide_independently(self):
        xw = [window("secondary", 604800, 13.0)]
        v = state(pview("claude"), pview("codex", snapshot=snap("codex", windows=xw)))
        self.assertEqual(render(v, labels_off()), "Claude 5h 32% | 7d 68%  ·  Codex 13%")

    def test_only_rendered_windows_are_counted(self):
        v = state(pview("claude", snapshot=snap(windows=[window("seven_day", 604800, 19.0)]))).to_json()
        bad = {"name": "odd", "duration_seconds": 0, "used_percent": 50.0, "resets_at": None}
        v["providers"]["claude"]["snapshot"]["windows"].insert(0, bad)
        self.assertEqual(renderer.render_dicts(labels_off(codex=False).to_json(), v, T0), "Claude 19%")
        self.assertEqual(renderer.render_dicts(settings(codex=False).to_json(), v, T0), "Claude 7d 19%")

    def test_other_states_are_unchanged(self):
        self.assertEqual(render(state(pview("claude", snapshot=None, status=None)), labels_off(codex=False)), f"Claude {E}")
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=[])))), "Claude --")
        self.assertEqual(render(state(pview("claude", snapshot=snap(windows=[]))), labels_off(codex=False)), "Claude --")
        cw = [window("seven_day", 604800, 18.0, t.iso_utc(T0 + 2 * DAY))]
        v = state(pview("claude", snapshot=snap(age=400, windows=cw)))
        self.assertEqual(render(v, labels_off(show_reset=True, codex=False)), "Claude 18% (2d) [stale 6m]")

    def test_default_keeps_labels_and_never_raises(self):
        v = self.one_window_each()
        self.assertEqual(render(v), "Claude 7d 19% (6d)  ·  Codex 7d 13% (6d)")
        on = t.Settings.from_json({"schema_version": 1, "display": {"window_labels": True},
                                   "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}})
        self.assertEqual(render(v, on), render(v))
        odd = {**labels_off().to_json(), "display": {"window_labels": False, "mode": object()}}
        self.assertIsInstance(renderer.render_dicts(odd, v.to_json(), T0), str)


if __name__ == "__main__":
    unittest.main()
