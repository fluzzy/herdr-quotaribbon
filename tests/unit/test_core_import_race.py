"""Lazy sibling imports never hand a half-initialised module to a second thread."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

import quotaribbon
from quotaribbon import connection

SLOW_MODULE = """
import time
time.sleep(0.3)  # another thread asks for the module while this one is still importing
def ready():
    return True
"""


class LazyImportRaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.name = f"_qr_slow_{os.getpid()}"
        with open(os.path.join(tmp.name, self.name + ".py"), "w") as fh:
            fh.write(SLOW_MODULE)
        quotaribbon.__path__.append(tmp.name)
        self.addCleanup(quotaribbon.__path__.remove, tmp.name)
        self.addCleanup(sys.modules.pop, f"quotaribbon.{self.name}", None)

    def test_concurrent_callers_all_get_the_fully_imported_module(self):
        results, errors = [], []

        def use():
            try:
                results.append(connection._core(self.name).ready())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=use) for _ in range(4)]
        threads[0].start()
        time.sleep(0.1)  # the first import is now executing the module body
        for th in threads[1:]:
            th.start()
        for th in threads:
            th.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results, [True] * 4)

    def test_modules_placed_in_sys_modules_are_still_used(self):
        fake = type(sys)("quotaribbon._qr_injected")
        fake.marker = "fake"
        sys.modules["quotaribbon._qr_injected"] = fake
        self.addCleanup(sys.modules.pop, "quotaribbon._qr_injected", None)
        self.assertIs(connection._core("_qr_injected"), fake)


if __name__ == "__main__":
    unittest.main()
