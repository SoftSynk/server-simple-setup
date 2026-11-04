"""Shared setup for the monitor tests: import paths, the two scripts, a fake /proc, stubs for commands, disks and mail.

Nothing here touches the real host: /proc comes from tests/fixtures/proc, statvfs and systemctl are stubs,
mail is a recorder, the clock is SOFTSYNK_NOW.
"""
import copy
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
MON = REPO / "deployment" / "monitor"
PROC = REPO / "tests" / "fixtures" / "proc"
sys.dont_write_bytecode = True
sys.path[:0] = [str(REPO / "lib"), str(REPO / "tests" / "lib"), str(MON)]

import monitorlib as ml  # noqa: E402
from libtest import Base, rd  # noqa: E402

from softsynk import core, ui  # noqa: E402

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
MOUNTS = ("/", "/mnt/data disk", "/boot/efi")  # what the mountinfo fixture lists


def script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(MON / name))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


def cfgof(**over):
    """The default config with `over` deep-merged on top (nested dicts merge, anything else replaces)."""
    def merge(base, o):
        for k, v in o.items():
            base[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
        return base
    return merge(copy.deepcopy(ml.DEFAULTS), over)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


class Runner:
    """Stands in for core.run: answers by command prefix (longest first; an int is an exit code, str is stdout)."""
    def __init__(self, answers=None):
        self.answers, self.calls = dict(answers or {}), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        line = " ".join(argv)
        for prefix in sorted(self.answers, key=len, reverse=True):
            if line.startswith(prefix):
                r = self.answers[prefix]
                return r if isinstance(r, core.Result) else core.Result(r, "") if isinstance(r, int) else core.Result(0, r)
        return core.Result(0)

    def ran(self, prefix):
        return any(" ".join(c).startswith(prefix) for c in self.calls)


class Mail:
    """Records what would be mailed; `ok` False makes every send fail."""
    def __init__(self):
        self.sent, self.ok = [], True

    def __call__(self, root, subject, body, run=None):
        self.sent.append((subject, body))
        return (True, "sent") if self.ok else (False, "SMTPServerDisconnected: gone")

    @property
    def subjects(self):
        return [s for s, _ in self.sent]


def statvfs_for(disk=None, inodes=None):
    """A statvfs stub: mount -> percent used (default 50 / 10); 1000 blocks of 1 MiB, 1000 inodes."""
    disk, inodes = disk or {}, inodes or {}

    def fake(mount):
        used = round(disk.get(mount, 50.0) * 10)
        iused = round(inodes.get(mount, 10.0) * 10)
        return types.SimpleNamespace(f_blocks=1000, f_bfree=1000 - used, f_bavail=1000 - used, f_frsize=1 << 20,
                                     f_files=1000, f_ffree=1000 - iused)
    return fake


def make_proc(dest):
    """A /proc-shaped directory from the recorded fixtures."""
    dest = Path(dest)
    for src, name in (("meminfo", "meminfo"), ("stat", "stat"), ("loadavg", "loadavg"), ("uptime", "uptime"),
                      ("mountinfo", "self/mountinfo"), ("net-dev", "net/dev")):
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROC / src, dest / name)
    shutil.copytree(PROC / "pids", dest, dirs_exist_ok=True)
    return str(dest)


RUNNING = (PROC / "systemctl-running").read_text()


class MBase(Base):
    """A temp management root, a fake /proc and an Env of stubs; `at()` pins the clock, `cpu()` sets the next CPU load."""
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        env.update(SOFTSYNK_NONINTERACTIVE="1", SOFTSYNK_SYSTEMD_DIR=str(self.tmp / "units"))
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        ui.setup()
        self.addCleanup(ui.setup)
        self.ctx = core.Ctx("monitor", str(self.root))
        self.proc = make_proc(self.tmp / "proc")
        self.mail = Mail()
        self.runner = Runner({"systemctl is-active": "active\nactive\nactive\n", "systemctl list-units --failed": ""})
        self.disk, self.inodes = {}, {}
        self.dirs = mock.Mock(return_value={"entries": [("var", 40 << 30), ("home", 5 << 30)], "total": 50 << 30, "complete": True, "errors": 0})
        self.top = mock.Mock(return_value=[{"pid": 7, "name": "php-fpm8.3", "rss": 900 << 20, "cpu": 55.0}])
        self.busy, self.idle = 22785 - 16770, 16770
        self.cpu(10)

    def env(self, **over):
        kw = dict(proc=self.proc, run=self.runner, statvfs=lambda m: statvfs_for(self.disk, self.inodes)(m), send=self.mail,
                  run_dir=str(self.tmp / "run"), host="web1", dir_sizes=self.dirs, top_processes=self.top)
        kw.update(over)
        return ml.Env(**kw)

    def cpu(self, pct, total=1000):
        """The next /proc/stat shows `pct` percent busy over `total` jiffies since the previous one."""
        self.busy += round(total * pct / 100)
        self.idle += total - round(total * pct / 100)
        (Path(self.proc) / "stat").write_text(f"cpu  {self.busy} 0 0 {self.idle} 0 0 0 0\n")

    def config(self, **over):
        cfg = cfgof(services=["nginx.service", "php8.3-fpm.service", "redis-server.service"], **over)
        core.write_json(self.ctx.config_path, cfg)
        return cfg

    def check(self, minutes, **kw):
        """One timer run at T0 + minutes."""
        self.at(core.iso(at(minutes)))
        return ml.check_once(self.ctx, self.env(**kw))

    def latest(self):
        return core.read_json(self.ctx.path("state", "latest.json"))

    def alerts(self):
        return core.read_json(self.ctx.path("state", "alerts.json"))


__all__ = ["MBase", "Base", "ml", "core", "ui", "rd", "cfgof", "at", "T0", "MOUNTS", "Runner", "Mail", "script", "statvfs_for",
           "make_proc", "RUNNING", "mock", "unittest", "Path", "timedelta", "datetime", "timezone"]
