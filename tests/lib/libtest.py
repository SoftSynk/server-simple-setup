"""Shared test setup for lib/softsynk: import path, fixtures, a temp-dir base class."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROC = REPO / "tests" / "fixtures" / "proc"
sys.dont_write_bytecode = True  # releases are read-only; keep the tree clean
sys.path.insert(0, str(REPO / "lib"))


def fixture(name):
    return (PROC / name).read_text()


def rd(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def wr(path, data, mode="w"):
    with open(path, mode) as f:
        f.write(data)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="softsynk-lib-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"

    def at(self, iso):
        """Pin the clock (core.now reads SOFTSYNK_NOW) for the rest of the test."""
        patcher = unittest.mock.patch.dict(os.environ, {"SOFTSYNK_NOW": iso})
        patcher.start()
        self.addCleanup(patcher.stop)
