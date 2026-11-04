# Decision: backup stack and server-management engine

- Date: 2026-11-04
- Tier: T2 (new runtime dependencies, cross-module pattern)
- Status: accepted and implemented.

## Problem

The server needs database backups with tiered retention, local and S3 copies, encryption, integrity checks and restore tests. It also needs one consistent way to install, configure, update and check the status of every server module, using gum for the CLI.

## Decision

- **Backups:** restic (dedup, encryption, S3, `copy`, `check`) plus `pg_dump -Fc` and `pg_dumpall --globals-only`, driven by a small Python stdlib runner (`synk-backup`) on a systemd timer every 6 hours. restic is the source of truth, and `state.json` is a rebuildable cache.
- **Engine:** `softsynk-server` in Python stdlib, with data-only TOML manifests (a registry plus one manifest per module). Entry points are argv arrays inside the release, and there are no module-specific branches in the engine.
- **Shell UI:** a shared `lib/ui.sh` gum wrapper with the same helper signatures as today, a strict non-interactive mode, and cancel distinct from No.
- **Layout:** everything managed lives under `/opt/softsynk-server-management`, with versioned releases and a `current` symlink for rollback.

## Rejected alternatives

- **pgBackRest / WAL-G:** point-in-time recovery is more than the brief needs, and pgBackRest's maintenance status is uncertain (archived in April 2026).
- **autopostgresqlbackup:** local only, with no encryption or remote copies.
- **Databasus / pgBackWeb:** web-UI services, which add an attack surface and a running daemon.
- **Bash for the engine and runner:** JSON, TOML, locking and retention logic would be fragile in Bash.
- **One combined local and remote repo through rclone:** adds a dependency; restic `copy` handles two repos natively.

## Review

A fresh Opus review found 4 blockers: dump file permissions, unsafe database names, secrets in argv, and copy-by-ID breaking after an outage. It also found 7 major issues. All of them were fixed before release. The reviewer's cuts were applied except where the brief required the feature (dependency blocking, staged update and rollback, in-progress status, restore from remote, custom paths).

## Consequences

- New server packages: `gum` and `restic` (both from the Ubuntu archive).
- The existing Redis and auto-updates scripts change their prompt helpers and gain a read-only `--status-json`. Their behaviour stays the same.
- Rollback is limited to the code. Changes a module made to system config are not reverted automatically, and the docs say so.
