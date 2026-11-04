"""Tests for quotaribbon.log: redaction (canary secrets), rotation, permissions."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid

from quotaribbon.log import MAX_MESSAGE_CHARS, cap_length, get_logger, redact, strip_control
from quotaribbon.paths import Paths

CANARY_EMAIL = "canary.user+qr@example.invalid"
CANARY_SK = "sk-ant-oat01-CANARYcanary0123456789abcdefABCDEF"
CANARY_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
CANARY_BEARER = "Bearer abc.DEF-ghi_123"
CANARY_HEX = "0123456789abcdef0123456789abcdef01234567"
CANARY_B64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ0123456789abcdefXYZ="


class RedactTests(unittest.TestCase):
    def test_canaries_are_masked(self):
        for secret in (CANARY_EMAIL, CANARY_SK, CANARY_JWT, CANARY_HEX, CANARY_B64):
            with self.subTest(secret=secret[:12]):
                out = redact(f"before {secret} after")
                self.assertNotIn(secret, out)
                self.assertTrue(out.startswith("before "))
                self.assertTrue(out.endswith(" after"))
        out = redact(f"Authorization: {CANARY_BEARER}")
        self.assertNotIn("abc.DEF-ghi_123", out)

    def test_key_value_secrets(self):
        out = redact('{"access_token": "opaque-value", "token=xyz", password: hunter2}')
        for leaked in ("opaque-value", "xyz", "hunter2"):
            self.assertNotIn(leaked, out)

    def test_ordinary_text_survives(self):
        text = "refresh claude ok in 1.2s path=~/Library/Application Support/herdr/plugins/quotaribbon"
        self.assertEqual(redact(text), text)
        self.assertEqual(redact("acct:0123456789abcdef01234567"), "acct:0123456789abcdef01234567")

    def test_control_characters_are_stripped(self):
        out = redact("line1\nFAKE 2026 ERROR forged\r\x1b[31mred\x1b[0m\x00nul\x7f\x9b\u2028end\tx")
        for ch in "\n\r\x1b\x00\x7f\x9b\u2028\t":
            self.assertNotIn(ch, out)
        self.assertEqual(out, "line1 FAKE 2026 ERROR forged [31mred[0mnul end x")
        self.assertEqual(strip_control("a\r\n\r\nb"), "a b")
        self.assertEqual(strip_control("한글 ok · …"), "한글 ok · …")

    def test_control_characters_cannot_hide_secrets(self):
        out = redact("key sk-ant-\x00oat01CANARYcanary0123456789")
        self.assertNotIn("CANARYcanary", out)

    def test_messages_are_length_capped(self):
        out = redact("x " * 5000)
        self.assertEqual(len(out), MAX_MESSAGE_CHARS)
        self.assertTrue(out.endswith("…[truncated]"))
        self.assertEqual(redact("short"), "short")
        exact = "y " * (MAX_MESSAGE_CHARS // 2)
        self.assertEqual(redact(exact), exact)
        self.assertEqual(len(cap_length("z" * 10, 5)), 5)
        self.assertEqual(MAX_MESSAGE_CHARS, 2000)

    def test_cap_happens_after_redaction(self):
        text = "a " * 995 + CANARY_EMAIL + " tail"
        out = redact(text)
        self.assertLessEqual(len(out), MAX_MESSAGE_CHARS)
        self.assertNotIn("canary.user", out)
        huge = CANARY_SK + " " + "b " * 50000
        out = redact(huge)
        self.assertNotIn(CANARY_SK, out)
        self.assertEqual(len(out), MAX_MESSAGE_CHARS)

    def test_secret_straddling_the_input_bound_never_survives_as_a_fragment(self):
        from quotaribbon import log

        # Long hex runs shrink to "<hex>", so text near the input bound reaches the output.
        filler = ("f" * 299 + " ") * (log._PRE_CAP_CHARS // 300 + 2)
        secret = "sk-" + "Q7" * 20
        for keep in range(1, 12):  # only the first ``keep`` chars of the secret fit under the bound
            out = redact(filler[: log._PRE_CAP_CHARS - keep] + secret + " more text")
            with self.subTest(keep=keep):
                self.assertLessEqual(len(out), MAX_MESSAGE_CHARS)
                self.assertTrue(out.endswith("[truncated]"), out[-40:])
                tail = out[: -len("…[truncated]")].rstrip().rsplit(" ", 1)[-1]
                self.assertEqual(tail, "<hex>")  # the cut token is gone entirely
                self.assertNotIn("sk-", out)
                self.assertNotIn("Q7", out)

    def test_paths_with_uuid_directories_stay_readable(self):
        uid = str(uuid.uuid4())
        tail = uid.rsplit("-", 1)[1]
        path = f"~/.cache/{uid}/herdr/config.toml"
        self.assertEqual(redact(f"config_path: {path}"), f"config_path: {path}")
        self.assertNotIn(tail, redact(f"api_key {uid}"))
        self.assertNotIn(tail, redact(f"id={uid}"))
        self.assertNotIn("SECRETxyz", redact(f"{path} token=SECRETxyz"))
        self.assertNotIn("Zq9" * 12, redact("~/" + "Zq9" * 12 + "/x"))  # a token-like segment is masked

    def test_never_raises(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("no")

        self.assertEqual(redact(Bad()), "<unloggable>")


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = Paths(os.path.join(self._tmp.name, "cfg"), os.path.join(self._tmp.name, "state"))

    def tearDown(self):
        for h in list(get_logger(self.paths).handlers):
            h.close()
        self._tmp.cleanup()

    def read_log(self, suffix=""):
        with open(self.paths.log_file + suffix, encoding="utf-8") as fh:
            return fh.read()

    def test_canary_secrets_are_redacted_in_the_file(self):
        log = get_logger(self.paths)
        log.info("auth ok for %s with %s", CANARY_EMAIL, CANARY_SK)
        try:
            raise ValueError(CANARY_JWT)
        except ValueError:
            log.exception("failure")
        text = self.read_log()
        self.assertIn("auth ok for", text)
        self.assertEqual(text.count("\n"), 2, "each record, traceback included, is one line")
        for secret in (CANARY_EMAIL, CANARY_SK, CANARY_JWT):
            self.assertNotIn(secret, text)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.log_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.logs_dir).st_mode), 0o700)

    def test_same_logger_returned_without_duplicate_handlers(self):
        a = get_logger(self.paths)
        b = get_logger(self.paths)
        self.assertIs(a, b)
        self.assertEqual(len(a.handlers), 1)
        a.info("once")
        self.assertEqual(self.read_log().count("once"), 1)

    def test_file_records_are_capped_and_single_line(self):
        log = get_logger(self.paths)
        log.info("forged\n2026-01-01 ERROR injected %s", "z" * 10000)
        lines = self.read_log().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), MAX_MESSAGE_CHARS)

    def test_rotation_keeps_one_backup(self):
        paths = Paths(os.path.join(self._tmp.name, "cfg2"), os.path.join(self._tmp.name, "state2"))
        log = get_logger(paths, max_bytes=2000, backups=1)
        try:
            for i in range(200):
                log.info("line %03d %s", i, "x" * 40)
            files = sorted(os.listdir(paths.logs_dir))
            self.assertEqual(files, ["quotaribbon.log", "quotaribbon.log.1"])
            self.assertLessEqual(os.path.getsize(paths.log_file), 2000)
            self.assertEqual(stat.S_IMODE(os.stat(paths.log_file + ".1").st_mode), 0o600)
        finally:
            for h in list(log.handlers):
                h.close()

    def test_unwritable_log_dir_never_raises(self):
        os.makedirs(self.paths.state_dir, exist_ok=True)
        with open(self.paths.logs_dir, "w") as fh:  # a file where the dir should be
            fh.write("x")
        paths = Paths(self.paths.config_dir, self.paths.state_dir)
        log = get_logger(paths)
        log.info("dropped")  # must not raise


if __name__ == "__main__":
    unittest.main()
