"""Shared setup for the housekeeping tests: loads synk-clean, builds a Host over a temp tree, scripts commands."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SYNK = REPO / "deployment" / "housekeeping" / "synk-clean"
NOW = "2026-11-04T12:00:00+00:00"
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "lib"))

from softsynk import core  # noqa: E402


def load():
    loader = importlib.machinery.SourceFileLoader("synk_clean", str(SYNK))
    spec = importlib.util.spec_from_loader("synk_clean", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


clean = load()


class Runner:
    """Stands in for core.run: answers by command prefix (longest first; an int is an exit code), 0 otherwise."""
    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        line = " ".join(argv)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if line.startswith(prefix):
                r = self.answers[prefix]
                return r if isinstance(r, core.Result) else core.Result(r)
        return core.Result(0)

    def ran(self, prefix):
        return any(" ".join(c).startswith(prefix) for c in self.calls)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="softsynk-clean-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = unittest.mock.patch.dict(os.environ, {"SOFTSYNK_NOW": NOW})
        p.start()
        self.addCleanup(p.stop)
        self.root = self.tmp / "mgmt"
        self.ctx = core.Ctx("housekeeping", self.root)
        self.runner = Runner()
        self.fs = self.tmp / "fs"  # the pretend machine
        self.var_log = self.fs / "var" / "log"
        self.var_log.mkdir(parents=True)

    def cutoff(self, days):
        return core.now().timestamp() - days * 86400

    def mk(self, path, age_days=0, size=10):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        t = self.cutoff(age_days)
        os.utime(path, (t, t))
        return path

    def host(self, **over):
        fs, log = self.fs, self.var_log
        h = dict(var_log=str(log), apt_archives=str(fs / "var/cache/apt/archives"),
                 crash_dirs=(str(fs / "var/crash"), str(fs / "var/lib/systemd/coredump")),
                 cache_globs=(str(fs / "home/*/.composer/cache"), str(fs / "root/.npm/_cacache")),
                 protected=clean.protected_paths(self.ctx) + (str(log / "mysql"), str(log / "journal")),
                 journald_conf=str(fs / "etc/systemd/journald.conf.d/50-softsynk.conf"),
                 logrotate_conf=str(fs / "etc/logrotate.d/softsynk-laravel"),
                 run=self.runner, mounts=lambda: [{"mount": str(fs), "fstype": "ext4", "source": "x", "dev": "1:1"}],
                 which=lambda n: None, budget_s=2)
        h.update(over)
        return clean.Host(**h)

    def cfg(self, **over):
        c = clean.copy.deepcopy(clean.DEFAULTS)
        c["app_roots"] = [str(self.fs / "home/*/*"), str(self.fs / "var/www/*")]
        c.update(over)
        return c

    def cli(self, *argv, host=None):
        if not os.path.exists(self.ctx.config_path):  # keep app_roots inside the temp tree
            self.write_config()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = clean.main(["--root", str(self.root), "--non-interactive", *argv], host=host or self.host())
        return rc, out.getvalue(), err.getvalue()

    def write_config(self, **over):
        core.write_json(self.ctx.config_path, dict(app_roots=self.cfg()["app_roots"], **over))
