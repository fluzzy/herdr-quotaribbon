"""setup pins a Homebrew interpreter by its ``opt`` path, which survives ``brew upgrade``."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from quotaribbon import cli


class StablePythonPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.prefix = os.path.realpath(self._tmp.name)
        keg_bin = os.path.join(self.prefix, "Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/bin")
        os.makedirs(keg_bin)
        self.real = os.path.join(keg_bin, "python3.14")
        with open(self.real, "wb") as fh:
            fh.write(b"\x7fELF")
        os.chmod(self.real, 0o755)
        os.makedirs(os.path.join(self.prefix, "opt"))
        self.opt_link = os.path.join(self.prefix, "opt/python@3.14")
        os.symlink("../Cellar/python@3.14/3.14.7", self.opt_link)
        self.stable = os.path.join(self.opt_link, "bin/python3.14")
        keg_top_bin = os.path.join(self.prefix, "Cellar/python@3.14/3.14.7/bin")
        os.makedirs(keg_top_bin)
        os.symlink("../Frameworks/Python.framework/Versions/3.14/bin/python3.14",
                   os.path.join(keg_top_bin, "python3.14"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_keg_path_becomes_the_opt_path(self):
        with mock.patch("quotaribbon.trust.trusted_executable", return_value=True):
            self.assertEqual(cli.stable_python_path(self.real), self.stable)

    def test_opt_path_must_resolve_to_the_same_interpreter(self):
        os.unlink(self.opt_link)
        other = os.path.join(self.prefix, "Cellar/python@3.14/3.14.8")
        os.makedirs(other)
        os.symlink(other, self.opt_link)
        with mock.patch("quotaribbon.trust.trusted_executable", return_value=True):
            self.assertEqual(cli.stable_python_path(self.real), self.real)

    def test_missing_opt_path_keeps_the_realpath(self):
        os.unlink(self.opt_link)
        self.assertEqual(cli.stable_python_path(self.real), self.real)

    def test_untrusted_opt_path_keeps_the_realpath(self):
        def trusted(path):
            return path == self.real
        with mock.patch("quotaribbon.trust.trusted_executable", side_effect=trusted):
            self.assertEqual(cli.stable_python_path(self.real), self.real)

    def test_non_homebrew_paths_are_kept(self):
        for path in ("/usr/bin/python3", "/Users/u/.pyenv/versions/3.12.13/bin/python3.12",
                     "/opt/homebrew/Cellar/pypy3/7.3/bin/python3"):
            self.assertEqual(cli.stable_python_path(path), path)

    def test_check_python_returns_the_stable_path(self):
        with mock.patch("quotaribbon.trust.trusted_executable", return_value=True), \
                mock.patch("quotaribbon.cli.read_regular", return_value=b"\x7fE"):
            self.assertEqual(cli.check_python(self.real, (3, 14, 7)), (self.stable, None))


if __name__ == "__main__":
    unittest.main()
