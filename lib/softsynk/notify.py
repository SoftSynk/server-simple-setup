"""Mail for every softsynk tool: one place for the transport, the config and the SMTP password.

send() reads etc/notify/config.json and secrets/notify/smtp.env of the management root and mails through
SMTP (STARTTLS or implicit TLS; Resend and Mailgun are presets of it) or through the local sendmail.
It never raises: callers sit in failure paths. The answer is (ok, detail); every attempt is recorded in
state/notify/last.json and log/notify/ with the password redacted.

Plain text only; webhooks are not built yet (one more branch in send() when wanted).
"""
import email.utils
import os
import re
import shutil
import smtplib
import socket
import ssl
from email.message import EmailMessage

from . import core

MODULE = "notify"
TIMEOUT = 20
TRANSPORTS = ("smtp", "sendmail")
SECURITY = ("starttls", "tls", "none")  # tls = implicit TLS (port 465)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:-]+$")
DEFAULTS = {"transport": "smtp", "to": [], "from": "",
            "smtp": {"host": "", "port": 587, "security": "starttls", "user": ""}}
PRESETS = {  # defaults offered by `synk-notify configure`; every value stays editable
    "resend": {"host": "smtp.resend.com", "port": 465, "security": "tls", "user": "resend"},
    "mailgun": {"host": "smtp.mailgun.org", "port": 587, "security": "starttls", "user": ""},
}


def secret_path(ctx):
    return ctx.path("secrets", "smtp.env")


def _addr(a):
    return isinstance(a, str) and bool(MAIL_RE.match(a))


def check(cfg):
    """Value rules for a config already type-checked by core.load_config."""
    p = []
    if cfg["transport"] not in TRANSPORTS:
        p.append("transport must be " + " or ".join(TRANSPORTS))
    if not cfg["to"]:
        p.append("to: at least one recipient is required")
    p += [f"to: {a!r} is not an email address" for a in cfg["to"] if not _addr(a)]
    smtp = cfg["transport"] == "smtp"
    if smtp and not cfg["from"]:
        p.append("from: a sender address is required for smtp")  # providers reject mail without one
    elif cfg["from"] and not _addr(cfg["from"]):
        p.append(f"from: {cfg['from']!r} is not an email address")
    if smtp:
        s = cfg["smtp"]
        if not HOST_RE.match(s["host"]):
            p.append("smtp.host: a host name or address is required")
        if not 1 <= s["port"] <= 65535:
            p.append("smtp.port must be 1-65535")
        if s["security"] not in SECURITY:
            p.append("smtp.security must be " + ", ".join(SECURITY))
        elif s["security"] == "none" and s["host"] not in LOCAL_HOSTS:
            p.append("smtp.security 'none' (no encryption) is only allowed for localhost")
        if re.search(r"[\r\n]", s["user"]):
            p.append("smtp.user must be one line")
    return p


def load(ctx):
    """The validated notify config, or None when notify is not configured. Invalid -> Fail(..., 64)."""
    return core.load_config(ctx.config_path, DEFAULTS, check)


def last(ctx):
    """The record of the latest send attempt (state/notify/last.json), or None."""
    try:
        r = core.read_json(ctx.path("state", "last.json"))
    except core.Corrupt:
        return None
    return r if isinstance(r, dict) else None


def sendmail_path():
    return shutil.which("sendmail") or ("/usr/sbin/sendmail" if os.access("/usr/sbin/sendmail", os.X_OK) else None)


def _message(sender, rcpt, subject, body):
    m = EmailMessage()
    if sender:
        m["From"] = sender
    m["To"] = ", ".join(rcpt)
    m["Subject"] = f"[softsynk {socket.gethostname()}] " + " ".join(subject.split())  # one line: no header injection
    m["Date"] = email.utils.format_datetime(core.now())
    m.set_content(body)
    return m


def password(ctx):
    """The stored SMTP password ("" when absent), registered so that logs and records never show it."""
    pw = core.read_env_file(secret_path(ctx)).get("SMTP_PASSWORD", "")
    core.register_secret(pw)
    return pw


def _smtp(ctx, cfg, msg, rcpt, pw):
    s = cfg["smtp"]
    if s["user"] and not pw:
        raise ValueError(f"SMTP password missing: run synk-notify configure (expected in {secret_path(ctx)})")
    tls = ssl.create_default_context()  # verifies the certificate and the host name
    kw = {"timeout": TIMEOUT, "local_hostname": socket.gethostname()}  # not getfqdn(): it can block on DNS
    srv = smtplib.SMTP_SSL(s["host"], s["port"], context=tls, **kw) if s["security"] == "tls" else smtplib.SMTP(s["host"], s["port"], **kw)
    with srv:
        if s["security"] == "starttls":
            srv.starttls(context=tls)  # a server without STARTTLS raises: never a silent downgrade
        if s["user"]:
            srv.login(s["user"], pw)
        refused = srv.send_message(msg, from_addr=cfg["from"], to_addrs=rcpt)
    return "sent via smtp to " + ", ".join(rcpt) + (f" (refused: {', '.join(refused)})" if refused else "")


def _sendmail(msg, run):
    path = sendmail_path()
    if not path:
        raise ValueError("sendmail not found")
    r = run([path, "-t", "-i"], timeout=60, input=msg.as_string())
    if not r.ok:
        raise ValueError(f"sendmail failed: {r.why()}")
    return "sent via sendmail to " + msg["To"]


def _finish(ctx, transport, ok, detail, error_class=None):
    detail = core.redact(detail)
    rec = {"at": core.iso(core.now()), "ok": ok, "transport": transport,
           "error_class": error_class, "error": None if ok else detail[:500]}
    try:
        core.write_json(ctx.path("state", "last.json"), rec)
    except OSError:
        pass
    core.log(ctx, "notify.sent" if ok else "notify.failed", transport=transport, error=rec["error"])
    return ok, detail


def send(root, subject, body, run=core.run):
    """Mail `body` under `subject` to the configured recipients. Returns (ok, detail)."""
    ctx = core.Ctx(MODULE, root)
    try:
        cfg = load(ctx)
    except core.Fail as f:
        return _finish(ctx, None, False, str(f), "ConfigError")
    if cfg is None:
        return False, "not configured: run synk-notify configure"
    rcpt = cfg["to"]
    pw = password(ctx)  # before the message is built, so that a password in the body is redacted too
    msg = _message(cfg["from"], rcpt, subject, core.redact(body))
    try:
        detail = _smtp(ctx, cfg, msg, rcpt, pw) if cfg["transport"] == "smtp" else _sendmail(msg, run)
    except (OSError, smtplib.SMTPException, ValueError) as e:  # OSError covers timeouts and ssl.SSLError
        return _finish(ctx, cfg["transport"], False, f"{type(e).__name__}: {e}", type(e).__name__)
    return _finish(ctx, cfg["transport"], True, detail)
