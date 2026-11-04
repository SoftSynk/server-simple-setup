"""Shared setup for the synk-harden tests: loads the script and scripts sshd, ufw, journalctl and systemd
over a temp tree. Nothing here touches the real machine."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SYNK = REPO / "deployment" / "hardening" / "synk-harden"
NOW = "2026-11-04T12:00:00+00:00"
STAMP = "20261104-120000"
UNIT = "softsynk-harden-revert-" + STAMP
HOST = "web-01"
SSH_CONN = "203.0.113.9 51234 192.0.2.10 22"
JOURNAL_KEY = "Accepted publickey for root from 203.0.113.9 port 51234 ssh2: ED25519 SHA256:abc\n"
JOURNAL_PW = "Accepted password for root from 203.0.113.9 port 51234 ssh2\n"
KEY_LINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample root@laptop\n"
SS = ('LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=812,fd=3))\n'
      'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=900,fd=6))\n'
      'LISTEN 0 4096 127.0.0.1:6379 0.0.0.0:* users:(("redis-server",pid=77,fd=6))\n'
      'LISTEN 0 80 *:3306 *:*\n')
SS6 = 'LISTEN 0 4096 [::]:22 [::]:* users:(("sshd",pid=812,fd=4))\n'
IDS = ("ssh_password", "ssh_rootlogin", "ssh_emptypw", "ssh_maxauth", "ssh_x11", "ssh_grace", "ssh_alive", "ufw_enable")
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "lib"))

from softsynk import core, ui  # noqa: E402


def load():
    loader = importlib.machinery.SourceFileLoader("synk_harden", str(SYNK))
    spec = importlib.util.spec_from_loader("synk_harden", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


harden = load()


class Machine:
    """Stands in for core.run. sshd -T shows the base config overlaid by the drop-in (first match wins), ufw keeps
    its state in files, systemd-run and systemctl track which timers exist. `script[prefix]` forces results."""

    def __init__(self, fs):
        self.fs, self.calls, self.script = Path(fs), [], {}
        self.dropin = self.fs / "etc/ssh/sshd_config.d/10-softsynk.conf"
        self.sshd = dict(port="22", permitrootlogin="yes", passwordauthentication="yes", kbdinteractiveauthentication="yes",
                         pubkeyauthentication="yes", permitemptypasswords="no", maxauthtries="6", x11forwarding="yes",
                         logingracetime="120", clientaliveinterval="0", clientalivecountmax="3", authenticationmethods="any",
                         authorizedkeyscommand="none", authorizedkeysfile=".ssh/authorized_keys .ssh/authorized_keys2", usepam="yes")
        self.ignored, self.reject_dropin = set(), False
        self.journal, self.ss, self.timers = JOURNAL_KEY, SS, set()
        self.estab, self.estab_text = [("203.0.113.9", 51234, 22)], None  # (client ip, client port, server port) of live SSH sockets
        self.added, self.show_added_rc = [], 0  # what `ufw show added` lists (it works while ufw is inactive)
        self.active, self.default_in, self.rules = False, "deny", []
        self.set_ufw(False)

    def set_ufw(self, active, rules=()):
        self.active, self.rules = active, list(rules)
        (self.fs / "etc/ufw").mkdir(parents=True, exist_ok=True)
        (self.fs / "etc/ufw/ufw.conf").write_text("ENABLED=%s\n" % ("yes" if active else "no"))
        (self.fs / "etc/ufw/user.rules").write_text("".join(f"{p} {a}\n" for p, a in self.rules))
        (self.fs / "etc/ufw/user6.rules").write_text("# v6\n")

    def ran(self, prefix):
        return [i for i, c in enumerate(self.calls) if " ".join(c).startswith(prefix)]

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        line = " ".join(argv)
        for prefix in sorted(self.script, key=len, reverse=True):
            if line.startswith(prefix):
                seq = self.script[prefix]
                r = seq.pop(0) if len(seq) > 1 else seq[0]
                return r if isinstance(r, core.Result) else core.Result(r, "", "scripted failure" if r else "")
        h = getattr(self, "do_" + argv[0].replace("-", "_"), None)
        return h(argv) if h else core.Result(0)

    def effective(self):
        over = {}
        if self.dropin.exists():
            for ln in self.dropin.read_text().splitlines():
                p = ln.split()
                if len(p) == 2 and not ln.startswith("#"):
                    over.setdefault(p[0].lower(), p[1])
        out = []
        for k, v in self.sshd.items():
            if k in over and k not in self.ignored:
                v = over[k]
            for x in (v if isinstance(v, list) else [v]):
                out.append(f"{k} {'without-password' if x == 'prohibit-password' else x}")
        return "\n".join(out) + "\n"

    def do_sshd(self, argv):
        if argv[1] == "-t":
            if self.reject_dropin and self.dropin.exists() and "Managed by softsynk" in self.dropin.read_text():
                return core.Result(1, "", "10-softsynk.conf line 3: Bad configuration option\n")
            return core.Result(0)
        return core.Result(0, self.effective())

    def do_journalctl(self, argv):
        return core.Result(0, self.journal)

    def do_ss(self, argv):
        if "established" not in argv:
            return core.Result(0, self.ss)
        if self.estab_text is not None:
            return core.Result(0, self.estab_text)
        host = lambda ip, port: f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"  # noqa: E731
        return core.Result(0, "".join(f"0 0 {host('2001:db8::1' if ':' in ip else '192.0.2.10', sport)} {host(ip, port)}\n"
                                      for ip, port, sport in self.estab))

    def do_systemd_run(self, argv):
        self.timers.add(argv[argv.index("--unit") + 1] + ".timer")
        return core.Result(0, "Running timer as unit\n")

    def do_systemctl(self, argv):
        if argv[1] == "stop":
            self.timers.discard(argv[2])
        if argv[1] == "is-active":
            return core.Result(0 if argv[2] in self.timers else 3, "active\n" if argv[2] in self.timers else "inactive\n")
        return core.Result(0)

    def do_ufw(self, argv):
        a = argv[1:]
        if a[:2] == ["status", "verbose"]:
            return core.Result(0, self.ufw_text())
        if a[:2] == ["show", "added"]:
            if self.show_added_rc:
                return core.Result(self.show_added_rc, "", "ERROR: problem running ufw\n")
            return core.Result(0, "Added user rules (see 'ufw status' for running firewall):\n" + ("\n".join(self.added) or "(None)") + "\n")
        if a[0] in ("allow", "limit"):
            self.set_ufw(self.active, self.rules + [(a[1], a[0].upper())])
        elif a == ["--force", "enable"]:
            self.set_ufw(True, self.rules)
        elif a == ["--force", "disable"]:
            self.set_ufw(False, self.rules)
        return core.Result(0)

    def ufw_text(self):
        if not self.active:
            return "Status: inactive\n"
        rows = "".join(f"{p:<27}{a + ' IN':<12}Anywhere\n" for p, a in self.rules)
        return (f"Status: active\nLogging: on (low)\nDefault: {self.default_in} (incoming), allow (outgoing), disabled (routed)\n"
                f"New profiles: skip\n\nTo                         Action      From\n--                         ------      ----\n{rows}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="softsynk-harden-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"SOFTSYNK_NOW": NOW})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(ui.setup, None)
        self.root, self.fs = self.tmp / "mgmt", self.tmp / "fs"
        self.ctx = core.Ctx("hardening", self.root)
        self.m = Machine(self.fs)
        self.tools, self.sent, self.typed = {"ufw": "/usr/sbin/ufw"}, [], HOST
        self.dropin = self.fs / "etc/ssh/sshd_config.d/10-softsynk.conf"
        self.dropin.parent.mkdir(parents=True)
        (self.fs / "etc/ssh/sshd_config").write_text("Include /etc/ssh/sshd_config.d/*.conf\nUsePAM yes\nPasswordAuthentication yes\n")
        (self.fs / "etc/default").mkdir(parents=True)
        (self.fs / "etc/default/ufw").write_text("DEFAULT_INPUT_POLICY=\"DROP\"\n")
        (self.fs / "home/root/.ssh").mkdir(parents=True)
        (self.fs / "home/root/.ssh/authorized_keys").write_text("# keys\n" + KEY_LINE)
        self.session = {"SSH_CONNECTION": SSH_CONN}  # env of the login shell; None = no SSH session (console)

    def host(self, **over):
        fs = self.fs
        h = dict(run=self.m, which=lambda n: self.tools.get(n), dropin=str(self.dropin), sshd_config=str(fs / "etc/ssh/sshd_config"),
                 ufw_files=(str(fs / "etc/ufw/user.rules"), str(fs / "etc/ufw/user6.rules"), str(fs / "etc/ufw/ufw.conf"),
                            str(fs / "etc/default/ufw")),
                 ancestry=lambda: [self.session] if self.session else [],
                 home=lambda u: str(fs / "home" / u) if u == "root" else None,
                 hostname=HOST, uid=0, send=lambda root, subject, body: self.sent.append((subject, body)) or (True, "sent"))
        h.update(over)
        return harden.Host(**h)

    def answer(self, prompt, secret=False):
        return self.typed if prompt.startswith("Type ") else ""

    def cli(self, *argv, tty=False, env=None, host=None):
        """(rc, stdout, stderr). tty=True plays a person: prompts are answered by self.answer (Enter, or the typed hostname)."""
        out, err = io.StringIO(), io.StringIO()
        full = ["--root", str(self.root), *([] if tty else ["--non-interactive"]), *argv]
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, env or {}))
            if tty:
                st.enter_context(mock.patch.object(ui, "interactive", return_value=True))
                st.enter_context(mock.patch.object(ui, "_gum_ok", return_value=False))
                st.enter_context(mock.patch.object(ui, "_line", side_effect=self.answer))
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            rc = harden.main(full, host=host or self.host())
        return rc, out.getvalue(), err.getvalue()

    def only(self, *ids):
        return {"SOFTSYNK_ANSWER_FINDING_" + i.upper(): "skip" for i in IDS if i not in ids}

    def apply(self, *ids, extra=(), **kw):
        """Run the interactive flow applying only `ids` (all findings when none)."""
        return self.cli("apply", *extra, tty=True, env=self.only(*ids) if ids else None, **kw)

    def pending(self):
        return core.read_json(self.ctx.path("state", "pending.json"))

    def text(self, path):
        p = Path(path)
        return p.read_text() if p.exists() else None

    def applied(self, *ids, **kw):
        """A pending change made through the real flow; returns nothing, fails the test when the flow fails."""
        rc, out, err = self.apply(*ids, **kw)
        self.assertEqual(rc, 0, err)

    def new_session(self, port=60000, logged=True, live=True):
        """Switch to a second SSH login (as the operator does to test): its socket is open (live) and it is in the
        login log (logged), unless said otherwise. The first login stays open."""
        self.session = {"SSH_CONNECTION": f"203.0.113.9 {port} 192.0.2.10 22"}
        if live:
            self.m.estab.append(("203.0.113.9", port, 22))
        if logged:
            self.m.journal += f"Accepted publickey for root from 203.0.113.9 port {port} ssh2: ED25519 SHA256:abc\n"

    def write_cfg(self, cfg):
        core.ensure_dir(os.path.dirname(self.ctx.config_path))
        core.write_json(self.ctx.config_path, cfg)
