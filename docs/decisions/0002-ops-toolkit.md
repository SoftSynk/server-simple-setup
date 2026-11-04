# Decision: ops toolkit (notify, monitor, housekeeping, hardening, optimizer)

- Date: 2026-11-04
- Tier: T2 (shared library, cross-module pattern)
- Status: accepted and implemented in 0.2.0.

## Problem

Backup, Redis and automatic updates leave a small server without alerts, without disk care, with SSH and the firewall left to hand editing, and with PHP and web-server defaults that ignore the machine's RAM. Mail also had one hard-coded path (`sendmail`), and many hosts have no mail system.

## Decision

- **Five modules in the existing format:** `notify`, `monitor` (with `synk-dash`), `housekeeping`, `hardening`, `optimizer`. Each is a folder with a data-only manifest, registered in `modules.toml`; the engine gains only a read-only `commands` listing so `bootstrap.sh` can link every module's commands.
- **Python, standard library only,** with one shared library, `lib/softsynk/` (core, sysinfo, ui, change, notify).
- **One mail path.** `notify` owns the SMTP/Resend/Mailgun/sendmail settings and one failure unit, `softsynk-alert@`. Backup now mails through it, so its own `mail_to` is deprecated (backup 1.1.0).
- **One JSON config per module** at `etc/<module>/config.json`, loaded and checked by one function. Mail settings live only in `notify`.
- **`change.apply` is the only way system files are edited:** check the current config, back up, write atomically, validate, reload, verify; any failure restores every file and reloads again.
- **Monitor history is tiered:** one record per hour while healthy, one per 5 minutes while not, 7 days kept, capped at 1 GB. Alert and state files fall back to `/run/softsynk-monitor` when the disk is full.
- **Hardening cannot strand the operator:** it refuses without proof of a key login, opens every ssh port before enabling ufw, and arms a timer that reverts the change unless confirmed from a new session.
- **Optimizer reports widely, applies narrowly:** only an OPcache drop-in is applied.
- **Scope cuts** are listed with triggers in `docs/workflow/backlog/ops-toolkit-deferred.md`.

## Rejected alternatives

- **Bash with `lib/ui.sh` for the new tools:** the decision logic (alert states, parsers, sizing formulas, path safety) needs fast in-process tests, and the existing suite already used about 108 of its roughly 120 seconds.
- **psutil, rich, textual or any other dependency:** `/proc`, `curses` and the standard library are enough.
- **One global TOML config:** the standard library cannot write TOML, and one file with many writers lets one module break another.
- **Webhooks or a plugin interface in `notify`:** no one asked for a non-mail channel yet; it is one more branch when needed.
- **A generic flow engine for Inspect → Report:** three tools use the sequence; each writes it out in about 30 lines.
- **A system-info cache or a collector daemon:** detection reads `/proc` and two `systemctl` calls in well under a second.

## Consequences

- Behaviour against live sshd, ufw, systemd, PHP-FPM, logrotate and SMTP providers is not verified; the READMEs say what to check on a disposable server first.
- Backup failure mail now needs `synk-notify configure` once; the old `mail_to` is ignored with a warning.
- Rollback of the framework still reverts code only; each tool prints its own undo command for the files it changed.
