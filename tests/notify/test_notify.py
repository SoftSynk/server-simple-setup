"""notify: the shared mail library (lib/softsynk/notify.py) and the synk-notify tool.

In-process: smtplib is replaced by a recording fake, systemctl and sendmail by stubs, the clock by
SOFTSYNK_NOW. One subprocess smoke test runs the real script. No network, no real mail.
"""
import glob
import importlib.machinery
import importlib.util
import io
import json
import os
import smtplib
import ssl
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from libtest import REPO, Base, rd  # noqa: E402

from softsynk import core, notify, ui  # noqa: E402

SCRIPT = str(REPO / "deployment" / "notify" / "synk-notify")
PW = "hunter2pass"


def load_script():
    loader = importlib.machinery.SourceFileLoader("synk_notify", SCRIPT)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("synk_notify", loader))
    loader.exec_module(mod)
    return mod


synk = load_script()


class Server:
    """A fake smtplib: every call lands in .calls; failures are injected through the constructor."""

    def __init__(self, connect_error=None, starttls_error=None, login_error=None):
        self.calls, self.connect_error, self.starttls_error, self.login_error = [], connect_error, starttls_error, login_error

    def smtp(self, host, port, **kw):
        return self._connect("SMTP", host, port, kw)

    def smtp_ssl(self, host, port, **kw):
        return self._connect("SMTP_SSL", host, port, kw)

    def _connect(self, kind, host, port, kw):
        if self.connect_error:
            raise self.connect_error
        self.calls.append(("connect", kind, host, port, kw))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self, context=None):
        if self.starttls_error:
            raise self.starttls_error
        self.calls.append(("starttls", context))

    def login(self, user, password):
        if self.login_error:
            raise self.login_error
        self.calls.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", msg, from_addr, to_addrs))
        return {}

    def names(self):
        return [c[0] for c in self.calls]

    def first(self, name):
        return next(c for c in self.calls if c[0] == name)


class NBase(Base):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
        env.update(SOFTSYNK_NONINTERACTIVE="1", SOFTSYNK_SYSTEMD_DIR=str(self.tmp / "units"))
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        ui.setup()
        self.addCleanup(ui.setup)
        self.addCleanup(core._SECRETS.clear)
        self.ctx = core.Ctx("notify", str(self.root))

    def put(self, path, text, mode=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)

    def config(self, smtp=None, **over):
        cfg = {"transport": "smtp", "to": ["ops@example.com"], "from": "alerts@example.com",
               "smtp": dict({"host": "smtp.example.com", "port": 587, "security": "starttls", "user": "apikey"}, **(smtp or {}))}
        cfg.update(over)
        self.put(self.ctx.config_path, json.dumps(cfg))
        return cfg

    def secret(self, pw=PW):
        core.write_env_file(notify.secret_path(self.ctx), {"SMTP_PASSWORD": pw})

    def unit_file(self):
        self.put(self.tmp / "units" / "softsynk-alert@.service", "[Service]\n")

    def last(self):
        return json.loads(rd(self.ctx.path("state", "last.json")))

    def fails(self, fn, code=core.USAGE):
        with self.assertRaises(core.Fail) as c:
            fn()
        self.assertEqual(c.exception.code, code)
        return str(c.exception)


# ---------------------------------------------------------------- config

class ConfigTest(NBase):
    def test_missing_config_means_not_configured(self):
        self.assertIsNone(notify.load(self.ctx))

    def test_valid_smtp_and_sendmail(self):
        self.config()
        self.assertEqual(notify.load(self.ctx)["smtp"]["host"], "smtp.example.com")
        self.config(transport="sendmail", **{"from": ""})
        self.assertEqual(notify.load(self.ctx)["transport"], "sendmail")

    def test_from_is_required_for_smtp_only(self):
        self.config(**{"from": ""})
        self.assertIn("from", self.fails(lambda: notify.load(self.ctx)))
        self.config(transport="sendmail", **{"from": ""})
        notify.load(self.ctx)

    def test_recipients_are_required_and_checked(self):
        self.config(to=[])
        self.assertIn("recipient", self.fails(lambda: notify.load(self.ctx)))
        self.config(to=["ops@example.com", "not-an-address"])
        self.assertIn("not-an-address", self.fails(lambda: notify.load(self.ctx)))

    def test_plaintext_only_for_localhost(self):
        self.config(smtp={"host": "mail.example.com", "security": "none", "port": 25, "user": ""})
        self.assertIn("localhost", self.fails(lambda: notify.load(self.ctx)))
        self.config(smtp={"host": "localhost", "security": "none", "port": 25, "user": ""})
        notify.load(self.ctx)

    def test_bad_values_and_types(self):
        for smtp in ({"security": "ssl"}, {"port": 0}, {"port": 70000}, {"host": ""}, {"host": "a b"}, {"port": "587"}, {"nope": 1}):
            with self.subTest(smtp=smtp):
                self.config(smtp=smtp)
                self.fails(lambda: notify.load(self.ctx))
        self.config(transport="webhook")
        self.fails(lambda: notify.load(self.ctx))


# ---------------------------------------------------------------- send: smtp

class SmtpTest(NBase):
    def send(self, server, **kw):
        with mock.patch.object(notify.smtplib, "SMTP", server.smtp), mock.patch.object(notify.smtplib, "SMTP_SSL", server.smtp_ssl):
            return notify.send(str(self.root), kw.pop("subject", "Disk full"), kw.pop("body", "body text"), **kw)

    def test_starttls_login_send(self):
        self.config()
        self.secret()
        srv = Server()
        ok, detail = self.send(srv)
        self.assertTrue(ok, detail)
        self.assertEqual(srv.names(), ["connect", "starttls", "login", "send", "quit"])
        _, kind, host, port, kw = srv.first("connect")
        self.assertEqual((kind, host, port, kw["timeout"]), ("SMTP", "smtp.example.com", 587, 20))
        ctx = srv.first("starttls")[1]
        self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertEqual(srv.first("login")[1:], ("apikey", PW))
        _, msg, sender, rcpt = srv.first("send")
        self.assertEqual((sender, rcpt), ("alerts@example.com", ["ops@example.com"]))
        self.assertTrue(msg["Subject"].startswith("[softsynk ") and msg["Subject"].endswith("] Disk full"))
        self.assertEqual(msg.get_content().strip(), "body text")
        self.assertEqual(self.last()["ok"], True)

    def test_implicit_tls_uses_smtp_ssl_without_starttls(self):
        self.config(smtp={"port": 465, "security": "tls"})
        self.secret()
        srv = Server()
        self.assertTrue(self.send(srv)[0])
        self.assertEqual(srv.names(), ["connect", "login", "send", "quit"])
        _, kind, _, port, kw = srv.first("connect")
        self.assertEqual((kind, port, kw["timeout"]), ("SMTP_SSL", 465, 20))
        self.assertEqual(kw["context"].verify_mode, ssl.CERT_REQUIRED)

    def test_localhost_plain_without_user_does_not_login(self):
        self.config(smtp={"host": "localhost", "port": 25, "security": "none", "user": ""})
        srv = Server()
        self.assertTrue(self.send(srv)[0])
        self.assertEqual(srv.names(), ["connect", "send", "quit"])

    def test_user_without_stored_password_connects_to_nothing(self):
        self.config()
        srv = Server()
        ok, detail = self.send(srv)
        self.assertFalse(ok)
        self.assertIn("password", detail)
        self.assertEqual(srv.calls, [])
        self.assertEqual(self.last()["ok"], False)

    def test_no_silent_downgrade_when_starttls_fails(self):
        self.config()
        self.secret()
        srv = Server(starttls_error=smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."))
        ok, _ = self.send(srv)
        self.assertFalse(ok)
        self.assertNotIn("send", srv.names())
        self.assertNotIn("login", srv.names())
        self.assertEqual(self.last()["error_class"], "SMTPNotSupportedError")

    def test_password_never_reaches_record_log_or_result(self):
        self.config()
        self.secret()
        srv = Server(login_error=smtplib.SMTPAuthenticationError(535, ("5.7.8 rejected " + PW).encode()))
        ok, detail = self.send(srv)
        self.assertFalse(ok)
        self.assertEqual(self.last()["error_class"], "SMTPAuthenticationError")
        files = [self.ctx.path("state", "last.json")] + glob.glob(self.ctx.path("log", "*.jsonl"))
        self.assertEqual(len(files), 2)
        for text in [detail] + [rd(f) for f in files]:
            self.assertNotIn(PW, text)
            self.assertIn("***", text)

    def test_password_in_body_is_redacted_in_the_mail(self):
        self.config()
        self.secret()
        srv = Server()
        self.send(srv, body=f"the key is {PW}")
        self.assertNotIn(PW, srv.first("send")[1].get_content())

    def test_network_errors_become_a_result_not_an_exception(self):
        self.config()
        self.secret()
        ok, detail = self.send(Server(connect_error=TimeoutError("timed out")))
        self.assertFalse(ok)
        self.assertEqual(self.last()["error_class"], "TimeoutError")
        self.assertIn("timed out", detail)

    def test_subject_is_one_line(self):
        self.config(smtp={"host": "localhost", "port": 25, "security": "none", "user": ""})
        srv = Server()
        self.send(srv, subject="a\r\nBcc: evil@example.com")
        self.assertNotIn("\n", srv.first("send")[1]["Subject"])
        self.assertIsNone(srv.first("send")[1]["Bcc"])

    def test_invalid_config_is_reported_and_recorded(self):
        self.config(smtp={"security": "none"})
        ok, detail = self.send(Server())
        self.assertFalse(ok)
        self.assertIn("localhost", detail)
        self.assertEqual(self.last()["error_class"], "ConfigError")


# ---------------------------------------------------------------- send: sendmail and unconfigured

class SendmailTest(NBase):
    def runner(self, rc=0):
        calls = []

        def run(argv, timeout=None, input=None, env=None):
            calls.append((argv, input))
            return core.Result(rc, "", "boom" if rc else "")
        return run, calls

    def test_sendmail_transport(self):
        self.config(transport="sendmail")
        run, calls = self.runner()
        with mock.patch.object(notify, "sendmail_path", return_value="/usr/sbin/sendmail"):
            ok, detail = notify.send(str(self.root), "Hi", "body", run=run)
        self.assertTrue(ok, detail)
        argv, text = calls[0]
        self.assertEqual(argv, ["/usr/sbin/sendmail", "-t", "-i"])
        self.assertIn("To: ops@example.com", text)
        self.assertIn("Subject: [softsynk ", text)
        self.assertEqual(self.last()["transport"], "sendmail")

    def test_sendmail_failure(self):
        self.config(transport="sendmail")
        run, _ = self.runner(rc=75)
        with mock.patch.object(notify, "sendmail_path", return_value="/usr/sbin/sendmail"):
            ok, detail = notify.send(str(self.root), "Hi", "body", run=run)
        self.assertFalse(ok)
        self.assertIn("boom", detail)
        self.assertEqual(self.last()["ok"], False)

    def test_unconfigured_sends_nothing(self):
        run, calls = self.runner()
        with mock.patch.object(notify, "sendmail_path", return_value="/usr/sbin/sendmail"):
            ok, detail = notify.send(str(self.root), "Hi", "body", run=run)
        self.assertFalse(ok)
        self.assertIn("not configured", detail)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.ctx.path("state", "last.json")))


# ---------------------------------------------------------------- synk-notify unit-failed

class UnitFailedTest(NBase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.result = (True, "sent")
        self.enter(mock.patch.object(notify, "send", side_effect=self._send))
        self.enter(mock.patch.object(core, "run", side_effect=lambda argv, **kw: core.Result(3, "x.service - failed\nActive: failed (Result: exit-code)\n")))

    def enter(self, cm):
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def _send(self, root, subject, body, **kw):
        self.sent.append((subject, body))
        return self.result

    def run_unit(self, unit="x.service"):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return synk.cmd_unit_failed(self.ctx, types.SimpleNamespace(unit=unit))

    def test_mails_the_unit_with_its_status(self):
        self.at("2026-11-04T10:00:00+00:00")
        self.assertEqual(self.run_unit(), core.OK)
        subject, body = self.sent[0]
        self.assertIn("x.service", subject)
        self.assertIn("Active: failed", body)
        self.assertIn("journalctl", body)
        self.assertIn("x.service", json.loads(rd(self.ctx.path("state", "failures.json"))))

    def test_same_unit_at_most_once_per_six_hours(self):
        self.at("2026-11-04T10:00:00+00:00")
        self.run_unit()
        self.at("2026-11-04T15:59:00+00:00")
        self.assertEqual(self.run_unit(), core.OK)
        self.assertEqual(len(self.sent), 1)
        self.at("2026-11-04T16:00:00+00:00")
        self.run_unit()
        self.assertEqual(len(self.sent), 2)

    def test_other_units_are_independent(self):
        self.at("2026-11-04T10:00:00+00:00")
        self.run_unit("x.service")
        self.run_unit("y.service")
        self.assertEqual(len(self.sent), 2)

    def test_failed_send_is_not_recorded_so_the_next_failure_retries(self):
        self.at("2026-11-04T10:00:00+00:00")
        self.result = (False, "SMTP down")
        self.assertEqual(self.run_unit(), core.FAILED)
        self.assertFalse(os.path.exists(self.ctx.path("state", "failures.json")))
        self.result = (True, "sent")
        self.assertEqual(self.run_unit(), core.OK)
        self.assertEqual(len(self.sent), 2)

    def test_corrupt_failures_file_is_tolerated(self):
        self.at("2026-11-04T10:00:00+00:00")
        self.put(self.ctx.path("state", "failures.json"), "{not json")
        self.assertEqual(self.run_unit(), core.OK)
        self.assertEqual(len(self.sent), 1)

    def test_unit_names_are_validated(self):
        for bad in ("--help", "../x", "a b", "x;y", ""):
            with self.subTest(unit=bad):
                self.fails(lambda: self.run_unit(bad))
        self.assertEqual(self.sent, [])


# ---------------------------------------------------------------- status

class StatusTest(NBase):
    def status(self):
        return synk.build_status(self.ctx)

    def good(self):
        self.config()
        self.secret()
        self.unit_file()

    def test_not_installed_without_config(self):
        doc = self.status()
        self.assertEqual((doc["module"], doc["install_state"], doc["health"]), ("notify", "not_installed", "unknown"))
        self.assertIn("synk-notify configure", doc["actions"])

    def test_healthy_before_and_after_a_send(self):
        self.good()
        doc = self.status()
        self.assertEqual(doc["health"], "healthy")
        self.assertIn("no mail sent yet", doc["summary"])
        core.write_json(self.ctx.path("state", "last.json"), {"at": "2026-11-04T10:00:00+00:00", "ok": True, "transport": "smtp"})
        self.assertEqual(self.status()["health"], "healthy")

    def test_degraded_when_the_last_send_failed_without_leaking_the_password(self):
        self.good()
        core.write_json(self.ctx.path("state", "last.json"),
                        {"at": "2026-11-04T10:00:00+00:00", "ok": False, "error_class": "SMTPAuthenticationError", "error": "535 bad " + PW})
        doc = self.status()
        self.assertEqual(doc["health"], "degraded")
        self.assertIn("SMTPAuthenticationError", " ".join(doc["warnings"]))
        self.assertNotIn(PW, json.dumps(doc))

    def test_degraded_for_missing_password_open_secret_or_missing_alert_unit(self):
        self.good()
        os.chmod(notify.secret_path(self.ctx), 0o644)
        self.assertIn("readable", " ".join(self.status()["warnings"]))
        os.unlink(notify.secret_path(self.ctx))
        self.assertIn("password", " ".join(self.status()["warnings"]))
        os.unlink(self.tmp / "units" / "softsynk-alert@.service")
        doc = self.status()
        self.assertEqual(doc["health"], "degraded")
        self.assertIn("softsynk-alert@.service", " ".join(doc["warnings"]))

    def test_invalid_config_is_unhealthy(self):
        self.config(to=[])
        doc = self.status()
        self.assertEqual((doc["install_state"], doc["health"]), ("installed", "unhealthy"))

    def test_status_exit_codes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(synk.main(["--root", str(self.root), "--status"]), 3)  # not installed
            self.good()
            self.assertEqual(synk.main(["--root", str(self.root), "--status"]), 0)
            os.unlink(self.tmp / "units" / "softsynk-alert@.service")
            self.assertEqual(synk.main(["--root", str(self.root), "--status"]), 1)
            self.assertEqual(synk.main(["--root", str(self.root), "--status-json"]), 0)  # module contract: always 0


# ---------------------------------------------------------------- configure

class ConfigureTest(NBase):
    def setUp(self):
        super().setUp()
        self.reloads = []

        def run(argv, **kw):
            self.reloads.append(argv)
            return core.Result(0)
        cm = mock.patch.object(core, "run", side_effect=run)
        cm.start()
        self.addCleanup(cm.stop)
        cm = mock.patch.object(notify, "sendmail_path", return_value="/usr/sbin/sendmail")
        cm.start()
        self.addCleanup(cm.stop)
        self.out = io.StringIO()

    def configure(self, *args, password=PW):
        os.environ.pop("SOFTSYNK_ANSWER_SMTP_PASSWORD", None)
        if password:
            os.environ["SOFTSYNK_ANSWER_SMTP_PASSWORD"] = password
        argv = ["configure", "--root", str(self.root), "--non-interactive", "--to", "ops@example.com", "--from", "alerts@example.com", *args]
        with redirect_stdout(self.out), redirect_stderr(self.out):
            return synk.main(argv)

    def test_resend_preset_writes_config_secret_and_unit(self):
        self.assertEqual(self.configure("--preset", "resend"), core.OK)
        cfg = json.loads(rd(self.ctx.config_path))
        self.assertEqual(cfg["smtp"], {"host": "smtp.resend.com", "port": 465, "security": "tls", "user": "resend"})
        self.assertEqual((cfg["to"], cfg["from"], cfg["transport"]), (["ops@example.com"], "alerts@example.com", "smtp"))
        self.assertNotIn(PW, rd(self.ctx.config_path))
        sp = notify.secret_path(self.ctx)
        self.assertEqual(oct(os.stat(sp).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.dirname(sp)).st_mode & 0o777), "0o700")
        self.assertEqual(core.read_env_file(sp), {"SMTP_PASSWORD": PW})
        unit = rd(self.tmp / "units" / "softsynk-alert@.service")
        self.assertIn(f"ExecStart={SCRIPT} --root {self.root} unit-failed %i", unit)
        self.assertNotIn("@ROOT@", unit)
        self.assertNotIn("@BIN@", unit)
        self.assertIn(["systemctl", "daemon-reload"], self.reloads)
        self.assertNotIn(PW, self.out.getvalue())

    def test_mailgun_needs_its_login_and_custom_takes_every_field(self):
        self.assertEqual(self.configure("--preset", "mailgun", "--smtp-user", "postmaster@mg.example.com"), core.OK)
        smtp = json.loads(rd(self.ctx.config_path))["smtp"]
        self.assertEqual((smtp["host"], smtp["port"], smtp["security"], smtp["user"]), ("smtp.mailgun.org", 587, "starttls", "postmaster@mg.example.com"))
        self.assertEqual(self.configure("--preset", "custom", "--smtp-host", "mail.example.org", "--smtp-port", "2525", "--smtp-security", "starttls", "--smtp-user", "u"), core.OK)
        smtp = json.loads(rd(self.ctx.config_path))["smtp"]
        self.assertEqual((smtp["host"], smtp["port"], smtp["user"]), ("mail.example.org", 2525, "u"))

    def test_sendmail_preset_stores_no_secret(self):
        self.assertEqual(self.configure("--preset", "sendmail", password=None), core.OK)
        self.assertEqual(json.loads(rd(self.ctx.config_path))["transport"], "sendmail")
        self.assertFalse(os.path.exists(notify.secret_path(self.ctx)))

    def test_switching_to_sendmail_removes_the_stored_password(self):
        self.configure("--preset", "resend")
        self.configure("--preset", "sendmail", password=None)
        self.assertFalse(os.path.exists(notify.secret_path(self.ctx)))

    def test_sendmail_preset_refused_without_sendmail(self):
        with mock.patch.object(notify, "sendmail_path", return_value=None):
            self.assertEqual(self.configure("--preset", "sendmail", password=None), core.USAGE)
        self.assertFalse(os.path.exists(self.ctx.config_path))

    def test_rerun_is_idempotent(self):
        self.configure("--preset", "resend")
        before = rd(self.ctx.config_path)
        self.reloads.clear()
        self.assertEqual(self.configure("--preset", "resend"), core.OK)
        self.assertEqual(rd(self.ctx.config_path), before)
        self.assertEqual(self.reloads, [])

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.configure("--preset", "resend", "--dry-run"), core.OK)
        self.assertFalse(os.path.exists(self.ctx.config_path))
        self.assertFalse(os.path.exists(notify.secret_path(self.ctx)))
        self.assertFalse(os.path.exists(self.tmp / "units"))
        self.assertEqual(self.reloads, [])

    def test_invalid_answers_write_nothing(self):
        self.assertEqual(self.configure("--preset", "custom", "--smtp-host", "mail.example.org", "--smtp-port", "25", "--smtp-security", "none", "--smtp-user", ""), core.USAGE)
        self.assertEqual(self.configure("--preset", "resend", "--to", "nobody"), core.USAGE)
        self.assertEqual(self.configure("--preset", "resend", password=None), core.USAGE)  # no way to ask for the password
        self.assertFalse(os.path.exists(self.ctx.config_path))
        self.assertFalse(os.path.exists(notify.secret_path(self.ctx)))

    def test_root_with_special_characters_is_refused(self):
        argv = ["configure", "--root", str(self.tmp / "a b"), "--non-interactive", "--preset", "sendmail", "--to", "o@example.com", "--from", "a@example.com"]
        with redirect_stdout(self.out), redirect_stderr(self.out):
            self.assertEqual(synk.main(argv), core.USAGE)
        self.assertIn("special characters", self.out.getvalue())
        self.assertFalse(os.path.exists(self.tmp / "units"))


# ---------------------------------------------------------------- send and test commands, smoke

class CommandTest(NBase):
    def main(self, *argv, stdin=""):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            return synk.main([*argv, "--root", str(self.root)]), out.getvalue()

    def test_send_reads_the_body_from_stdin_and_test_sends_a_test_mail(self):
        with mock.patch.object(notify, "send", return_value=(True, "sent via smtp")) as send:
            rc, out = self.main("send", "--subject", "Backup done", stdin="all good\n")
            self.assertEqual(rc, core.OK)
            self.assertEqual(send.call_args.args[1:3], ("Backup done", "all good\n"))
            rc, out = self.main("test")
            self.assertEqual(rc, core.OK)
            self.assertIn("test", send.call_args.args[1].lower())
            self.assertIn("sent via smtp", out)

    def test_failed_send_exits_2_and_says_why(self):
        with mock.patch.object(notify, "send", return_value=(False, "SMTPAuthenticationError: nope")):
            rc, out = self.main("test")
        self.assertEqual(rc, core.FAILED)
        self.assertIn("SMTPAuthenticationError", out)


class SmokeTest(unittest.TestCase):
    def test_status_json_on_an_empty_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            env = {k: v for k, v in os.environ.items() if not k.startswith("SOFTSYNK_")}
            r = subprocess.run([sys.executable, SCRIPT, "--root", root, "--status-json"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual((doc["schema"], doc["module"], doc["install_state"]), (1, "notify", "not_installed"))


if __name__ == "__main__":
    unittest.main()
