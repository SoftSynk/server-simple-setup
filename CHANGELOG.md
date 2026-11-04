# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-04

### Added

- `notify` module (`synk-notify`): one mail setup (SMTP, Resend, Mailgun or sendmail) for every module, plus `softsynk-alert@.service` so any systemd unit can mail you when it fails.
- `monitor` module (`synk-monitor`, `synk-dash`): checks disk, inodes, memory, swap, CPU, chosen services and failed units, keeps 7 days of history, mails when something needs attention and when it is over, and shows the data live in the terminal.
- `housekeeping` module (`synk-clean`): finds what fills the disk, previews and deletes safe-to-remove files after a yes, caps the journal and rotates Laravel logs.
- `optimizer` module (`synk-tune`): audits PHP, PHP-FPM, Nginx, Apache, Redis and Meilisearch settings against RAM and CPUs; applies only an OPcache drop-in, with validation and rollback.
- `hardening` module (`synk-harden`): audits SSH and ufw, refuses changes that could lock you out, and reverts itself unless confirmed from a new session.
- Shared Python library `lib/softsynk/` (core, sysinfo, ui, change, notify) used by the new modules.
- `softsynk-server commands` lists the commands modules ship; `bootstrap.sh` links all of them into `/usr/local/bin`.

### Changed

- `backup` 1.1.0: a failed backup is mailed through the `notify` module (`softsynk-alert@`). `mail_to` and `--mail-to` are deprecated and ignored with a warning; the `softsynk-notify@` unit is removed on the next setup run. Run `synk-notify configure` once to keep getting failure mail.
- Renamed the product to Synk Server Manager in docs. The `softsynk-server` command, paths and file names are unchanged.

## [0.1.0] - 2026-11-04

First public release.

### Added

- `softsynk-server` engine: manifest-driven install, configure, status, update and rollback of modules, with a gum terminal UI and a non-interactive mode.
- `backup` module (`synk-backup`): PostgreSQL dumps into an encrypted restic repository with an S3-compatible off-site copy, tiered retention, integrity checks, restore tests, restores and failure e-mail.
- `redis` module: single or cache-and-queue Redis setup and tuning.
- `auto-updates` module: unattended-upgrades setup with third-party repos, scheduled reboots and verification.
