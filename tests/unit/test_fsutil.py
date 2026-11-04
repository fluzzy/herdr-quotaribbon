"""Tests for quotaribbon.fsutil: atomic private writes, strict reads, flock locks."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from quotaribbon import fsutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_json_is_private_and_readable(self):
        path = os.path.join(self.dir, "a.json")
        fsutil.atomic_write_json(path, {"k": [1, 2.5, None, "한글"]})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(fsutil.read_json(path), {"k": [1, 2.5, None, "한글"]})
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_overwrite_replaces_and_tightens_mode(self):
        path = os.path.join(self.dir, "a.json")
        with open(path, "w") as fh:
            fh.write("old")
        os.chmod(path, 0o644)
        fsutil.atomic_write_json(path, {"v": 2})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(fsutil.read_json(path), {"v": 2})

    def test_nan_rejected_before_touching_disk(self):
        path = os.path.join(self.dir, "a.json")
        with self.assertRaises(ValueError):
            fsutil.atomic_write_json(path, {"v": float("nan")})
        with self.assertRaises(ValueError):
            fsutil.atomic_write_json(path, {"v": object()})
        self.assertEqual(os.listdir(self.dir), [])

    def test_failed_replace_leaves_no_temp_and_old_content(self):
        path = os.path.join(self.dir, "a.json")
        fsutil.atomic_write_json(path, {"v": 1})
        with mock.patch("quotaribbon.fsutil.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                fsutil.atomic_write_json(path, {"v": 2})
        self.assertEqual(os.listdir(self.dir), ["a.json"])
        self.assertEqual(fsutil.read_json(path), {"v": 1})

    def test_write_text(self):
        path = os.path.join(self.dir, "t.txt")
        fsutil.atomic_write_text(path, "hello\n")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class ReadJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data: bytes):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_missing_is_none(self):
        self.assertIsNone(fsutil.read_json(os.path.join(self.dir, "nope.json")))

    def test_corrupt_variants(self):
        cases = {
            "partial.json": b'{"a": 1',
            "nan.json": b'{"a": NaN}',
            "inf.json": b'{"a": Infinity}',
            "dup.json": b'{"a": 1, "a": 2}',
            "latin1.json": b'{"a": "\xff"}',
            "empty.json": b"",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(fsutil.CorruptFile):
                    fsutil.read_json(self.write(name, data))

    def test_too_large(self):
        path = self.write("big.json", b'"' + b"x" * (fsutil.MAX_JSON_BYTES) + b'"')
        with self.assertRaises(fsutil.CorruptFile):
            fsutil.read_json(path)
        small = self.write("small.json", b'"' + b"x" * 10 + b'"')
        with self.assertRaises(fsutil.CorruptFile):
            fsutil.read_json(small, max_bytes=5)

    def test_directory_is_corrupt(self):
        os.mkdir(os.path.join(self.dir, "d.json"))
        with self.assertRaises(fsutil.CorruptFile):
            fsutil.read_json(os.path.join(self.dir, "d.json"))

    def test_corrupt_is_value_error(self):
        self.assertTrue(issubclass(fsutil.CorruptFile, ValueError))


_HOLDER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from quotaribbon import fsutil
    with fsutil.file_lock(sys.argv[2]):
        print("locked", flush=True)
        sys.stdin.readline()
    print("released", flush=True)
    """
)


class FileLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self._tmp.name, "locks", "x.lock")

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_creates_private_file_and_dir(self):
        with fsutil.file_lock(self.lock):
            pass
        self.assertEqual(stat.S_IMODE(os.stat(self.lock).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.lock)).st_mode), 0o700)
        self.assertTrue(os.path.exists(self.lock), "lock files are never deleted")

    def test_same_process_exclusion(self):
        with fsutil.file_lock(self.lock):
            with self.assertRaises(fsutil.LockBusy):
                with fsutil.file_lock(self.lock):
                    self.fail("entered a busy lock")
        with fsutil.file_lock(self.lock):
            pass

    def test_cross_process_exclusion(self):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER, REPO_ROOT, self.lock],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            with self.assertRaises(fsutil.LockBusy):
                with fsutil.file_lock(self.lock):
                    self.fail("entered a lock held by another process")
            proc.stdin.write("\n")
            proc.stdin.flush()
            self.assertEqual(proc.stdout.readline().strip(), "released")
            proc.wait(timeout=10)
            with fsutil.file_lock(self.lock):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdin.close()
            proc.stdout.close()

    def test_lock_released_when_holder_dies(self):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _HOLDER, REPO_ROOT, self.lock],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            proc.kill()
            proc.wait(timeout=10)
            with fsutil.file_lock(self.lock):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()


class HashTests(unittest.TestCase):
    def test_sha256_hex(self):
        self.assertEqual(len(fsutil.sha256_hex("a")), 64)
        self.assertNotEqual(fsutil.sha256_hex("a|b", "c"), fsutil.sha256_hex("a", "b|c"))
        self.assertEqual(fsutil.sha256_hex("a", 1, b"x"), fsutil.sha256_hex("a", "1", b"x"))
        with self.assertRaises(TypeError):
            fsutil.sha256_hex(True)


class PrivateDirTests(unittest.TestCase):
    def test_ensure_private_dir_tightens(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "a", "b")
            fsutil.ensure_private_dir(d)
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(d)).st_mode), 0o700)  # created parent
            self.assertNotEqual(stat.S_IMODE(os.stat(tmp).st_mode) & 0o077, 0o700)  # pre-existing untouched
            os.chmod(d, 0o755)
            fsutil.ensure_private_dir(d)
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_directory_created_concurrently_by_another_process_is_accepted(self):
        real_mkdir = os.mkdir

        def racing_mkdir(path, mode=0o777):
            real_mkdir(path, 0o700)  # another process wins the race
            raise FileExistsError(path)

        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "a", "b")
            with mock.patch.object(fsutil.os, "mkdir", racing_mkdir):
                fsutil.ensure_private_dir(d)
            self.assertTrue(os.path.isdir(d))
            # A file that wins the race is still an error.
            f = os.path.join(tmp, "c")

            def racing_file(path, mode=0o777):
                open(path, "w").close()
                raise FileExistsError(path)

            with mock.patch.object(fsutil.os, "mkdir", racing_file), self.assertRaises(FileExistsError):
                fsutil.ensure_private_dir(f)


if __name__ == "__main__":
    unittest.main()
