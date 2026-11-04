"""An unusable clock record never lets cached values look younger than they are."""

from __future__ import annotations

import os

from quotaribbon import cache
from tests.helpers.core_fakes import T0, result
from tests.integration.test_refresh import CLAUDE, CODEX, SEP, RefreshTestCase


class CorruptClockTests(RefreshTestCase):
    def break_clock(self, content: str | None) -> None:
        if content is None:
            os.unlink(self.paths.clock)
        else:
            with open(self.paths.clock, "w") as fh:
                fh.write(content)

    def test_unusable_clock_hides_cached_values_until_recovered(self):
        self.connect_both()
        for content in ("{not json", '{"max_wall_seen": 1' + "0" * 400 + "}", '{"other": 1}', None):
            with self.subTest(content=(content or "deleted")[:20]):
                self.break_clock(content)
                self.assertEqual(self.line(), "Claude --" + SEP + "Codex --")
                self.assertEqual(self.due(), {"claude": "clock_rollback", "codex": "clock_rollback"})
                self.clock.advance(1)
                self.refresh()
                self.assertEqual(cache.read_clock_state(self.paths)[0], cache.CLOCK_OK)
                self.assertEqual(self.line(), CLAUDE + SEP + CODEX)

    def test_recovery_invalidates_old_snapshots_and_rotates_the_epoch_first(self):
        self.connect_both()  # values fetched at T0
        epoch = cache.read_server_epoch(self.paths)
        self.clock.now = T0 + 600
        self.refresh()
        self.break_clock("{garbage")
        self.clock.now = T0 + 100  # a rollback hidden by the broken clock record
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="timeout")
        self.host.collect_results["codex"] = result("codex", "transient_error", error_code="timeout")
        self.refresh()
        self.assertNotEqual(cache.read_server_epoch(self.paths), epoch)
        self.assertIsNone(self.snapshot("claude"))
        self.assertIsNone(self.snapshot("codex"))
        self.assertEqual(self.line(), "Claude --" + SEP + "Codex --")  # never the T0 values again

    def test_unknown_snapshot_presence_fails_closed(self):
        from unittest import mock

        self.connect_both()
        os.unlink(self.paths.clock)
        pdir = os.path.join(self.paths.state_dir, "providers", "claude")
        real_listdir = os.listdir

        def unreadable(path):
            if os.path.abspath(path) == pdir:
                raise PermissionError(13, "denied")
            return real_listdir(path)

        with mock.patch.object(cache.os, "listdir", side_effect=unreadable):
            self.assertTrue(cache.any_snapshot_exists(self.paths.state_dir))
            self.clock.advance(1)
            rep = self.refresh()
        self.assertEqual({o.detail for o in rep.outcomes}, {"invalidation_incomplete"})
        self.assertFalse(os.path.exists(self.paths.clock))  # no new baseline over unknown values
        # Access is back: the old values are still never shown; recovery replaces them.
        self.assertEqual(self.line(), "Claude --" + SEP + "Codex --")
        self.host.collect_results["claude"] = result("claude", "transient_error", error_code="timeout")
        self.clock.advance(1)
        self.refresh()
        self.assertIsNone(self.snapshot("claude"))
        self.assertEqual(cache.read_clock_state(self.paths)[0], cache.CLOCK_OK)

    def test_missing_clock_without_snapshots_is_a_normal_first_run(self):
        self.assertFalse(os.path.exists(self.paths.clock))
        rep = self.refresh()
        self.assertEqual([o.action for o in rep.outcomes], ["published", "published"])
        self.assertEqual(self.line(), CLAUDE + SEP + CODEX)


if __name__ == "__main__":
    import unittest

    unittest.main()
