# Mail notifications (module `notify`)

One mail setup for every softsynk tool. You enter the SMTP settings once; other modules (housekeeping, monitor,
hardening) send their mail through it, and any systemd unit can mail you when it fails. Plain-text mail only;
webhooks are not built.

Requires: Ubuntu 24.04 or newer and the python3 that ships with it (3.12). Nothing is installed with apt. The
module declares `min_version = "24.04"` because the shared library needs Python 3.11 or newer, which is the only
requirement; the backup module asks for 26.04 for its own reason (restic).

## Set it up

As root, on the server, after `softsynk-server install notify` or directly:

```
/usr/local/bin/synk-notify configure
/usr/local/bin/synk-notify test
```

`/usr/local/bin/synk-notify` exists once `bootstrap.sh` has linked the module's commands. Until then use the full
path of the release, `/opt/softsynk-server-management/current/deployment/notify/synk-notify`.

`configure` asks for a preset, the recipients and the sender, and for the SMTP password. It writes nothing until
every answer validates, then stores the settings, the password and the alert unit. Running it again is safe and
changes nothing when the answers are the same; a stored password is kept when you leave the prompt empty (or, when
nobody can be asked, when no new one is supplied).

| Preset | Server | Login | Password |
| --- | --- | --- | --- |
| `resend` | `smtp.resend.com`, port 465, implicit TLS | `resend` | your Resend API key |
| `mailgun` | `smtp.mailgun.org`, port 587, STARTTLS | the domain's SMTP login (asked) | its SMTP password |
| `custom` | host, port, encryption and login are asked | optional | optional |
| `sendmail` | this server's own `sendmail` | none | none |

Encryption is `starttls` or `tls` (implicit TLS) and the certificate and host name are always verified. `none` is
accepted only for `localhost`. A sender address is required for every SMTP transport; providers reject mail
without one.

Scripted (no prompts):

```
env SOFTSYNK_ANSWER_SMTP_PASSWORD='<key>' /usr/local/bin/synk-notify configure --non-interactive \
  --preset resend --to ops@example.com --from alerts@example.com
```

The password is never an option: it comes from the hidden prompt or from `SOFTSYNK_ANSWER_SMTP_PASSWORD`.

## Where things live

| Item | Path under the management root |
| --- | --- |
| Settings (no secrets) | `etc/notify/config.json` |
| SMTP password | `secrets/notify/smtp.env` (mode 0600, folder 0700), key `SMTP_PASSWORD` |
| Last send attempt | `state/notify/last.json` (time, result, error class; the password is redacted) |
| Failure-mail record | `state/notify/failures.json` (when each unit was last mailed about) |
| Log | `log/notify/YYYY-MM.jsonl`, kept 90 days |
| Alert unit | `/etc/systemd/system/softsynk-alert@.service` |

## Failure mail for any unit

`configure` installs the template unit `softsynk-alert@.service`. Point another unit at it:

```
[Unit]
OnFailure=softsynk-alert@%n.service
```

When that unit fails, the alert unit runs `synk-notify unit-failed <unit>`, which mails `systemctl status` and a
`journalctl` hint. The same unit is mailed about at most once every 6 hours; a failed send is not counted, so the
next failure tries again. `softsynk-backup.service` already does this.

## Other commands

| Command | What it does |
| --- | --- |
| `synk-notify test` | sends a test mail, exit 2 and the reason when it cannot |
| `synk-notify send --subject TEXT` | mails the text on stdin |
| `synk-notify --status` | health, exit 0 healthy, 1 degraded, 2 unhealthy, 3 not configured |
| `synk-notify --status-json` | the same as JSON for `softsynk-server status` (always exits 0) |

Status is `not_installed` until `etc/notify/config.json` exists. It is `unhealthy` when that file is invalid and
`degraded` when the last mail failed, the SMTP password is missing, `smtp.env` is readable by other users, or
`softsynk-alert@.service` is not installed.

## Limits

- The providers' SMTP endpoints and your credentials are not checked until a mail is sent: run `synk-notify test`.
- One send is tried once, with a 20 second timeout per SMTP step. There is no queue and no retry; the next failure
  or the next run of the calling module tries again.
- Webhooks (Slack and the like) are not supported.
- The shared library is `lib/softsynk/notify.py`; other tools call `notify.send(root, subject, body)`.
