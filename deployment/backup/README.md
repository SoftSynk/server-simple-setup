# Database backups (module `backup`)

Backs up every PostgreSQL database on the server with `pg_dump`, stores the dumps in a local
[restic](https://restic.net) repository, and copies each run to an S3-compatible bucket. A systemd timer
runs it every 6 hours. Only PostgreSQL is backed up: no application files, no Redis.

Requires: Ubuntu 26.04 or newer, restic 0.17.1 or newer, gum, the PostgreSQL client tools
(`pg_dump`, `pg_dumpall`, `pg_restore`, `psql`, at least as new as the server), python3, and the `postgres`
OS user (the module reads PostgreSQL through `runuser -u postgres`, so no database password is needed).

## What it does

| Item | Value |
| --- | --- |
| Schedule | 00:15, 06:15, 12:15, 18:15 server time (`softsynk-backup.timer`, `Persistent=true`) |
| What is dumped | each database (custom format, compressed by restic) plus the cluster globals (roles, tablespaces) |
| Skipped | templates, the `postgres` maintenance database, leftover `softsynk_restore_*` databases, and anything in `exclude_databases` |
| Local repository | `<root>/data/backup/restic` by default (`--local-repo` to change) |
| Remote repository | S3-compatible bucket, optional but strongly recommended |
| Repository password | `<root>/secrets/backup/restic.pass` (0600) |
| S3 keys | `<root>/secrets/backup/s3.env` (0600) |
| State | `<root>/state/backup/state.json` |
| Logs | `<root>/log/backup/YY/MM/db-backup/<time>-<runid>.jsonl`, kept 90 days |

`<root>` is the management root, `/opt/softsynk-server-management` by default (`--root` or
`SOFTSYNK_ROOT` to change it).

Every run, the dump goes to a private temporary file (0600, written by root), is checked with
`pg_restore --list` (a readable table of contents with data entries), is added to the local repository, and is
then copied to the remote with `restic copy`. The copy happens on every run, so a snapshot that failed to reach
the remote last time is sent next time. A failed backup is retried on the next run.

**Consistency.** Each database dump is a consistent snapshot of that one database. Dumps of different
databases are taken one after another, so they are not consistent with each other. An application that spans
several databases and needs cross-database consistency must be stopped (or quiesced) for the backup, or restored
to a matching point by hand.

## Retention

A snapshot belongs to one tier. Per database, each run creates the snapshots that are missing for the current
period:

| Tier | Created | Kept (default) |
| --- | --- | --- |
| `daily` | first successful backup of each calendar day | last 3 |
| `weekly` | first successful backup of each ISO week | last 1 |
| `twice_monthly` | first successful backup of each half-month (1st to 14th, 15th to month end) | last 2 |

The cluster globals (roles) are saved as their own snapshots in the same tiers. They are taken on every run in
which any database has a tier due, and cover the union of those tiers, so a database created in the middle of a
day gets its roles saved with it. Only one globals snapshot per tier and period is kept; an older one from the
same period is forgotten on both repositories.

Only successful snapshots count. A failed run creates nothing and removes nothing, so a failure never
shortens your history. Change the counts with `--retention-daily`, `--retention-weekly`,
`--retention-twice-monthly`. `forget` runs per tier on both repositories; `prune` runs locally only when
`forget` removed something, and on the remote at most once a week. If the remote copy failed in a run, the
remote retention steps are skipped and retried in the next run.

Weekly, `restic check --read-data-subset=5%` runs on both repositories. Every 30 days a restore test restores
the newest snapshot of one database into a throwaway database (`softsynk_restore_test_*`) and drops it again.

## Setup

Run as root, on the server, from the management folder:

```sh
sudo softsynk-server install backup        # interactive: asks, then installs
sudo softsynk-server configure backup      # change settings later; the timer stays as it is
```

(or run `deployment/backup/setup-backup.sh` and `deployment/backup/setup-backup.sh --configure` directly).
The wizard asks for:

1. The local repository folder.
2. The remote: a preset (`r2`, `b2`, `hetzner`, `custom`) or `none`. A preset only fills in the endpoint
   pattern and region; every value stays editable.
   - `r2`: endpoint `https://<id>.r2.cloudflarestorage.com`, region `auto` (`<id>` is the account id)
   - `b2`: endpoint `https://s3.<id>.backblazeb2.com`, region `<id>` (for example `us-west-004`)
   - `hetzner`: endpoint `https://<id>.your-objectstorage.com`, region `<id>` (for example `fsn1`)
   - `custom`: you give the endpoint; region defaults to `us-east-1`
3. Bucket, optional prefix, and the S3 access key id and secret.
4. The restic repository password: generate one or type your own (at least 12 characters).
5. Retention counts and databases to exclude.

Failure mail is not set up here: run `synk-notify configure` once (the `notify` module) and every failed backup is
mailed through it.

Nothing is written or enabled until all answers validate: the tools, PostgreSQL access, the bucket and both
repositories are checked first. `--dry-run` runs only those checks.

**The repository password is the only key to your backups.** If the wizard generates it, it shows it once and
makes you retype its last 4 characters before continuing. It is also stored on the server in
`secrets/backup/restic.pass`, which is lost together with the server. Keep a copy somewhere else (a password
manager). Without it the remote copy cannot be read. Changing the password after the repositories exist is
refused.

**Bucket protection.** The S3 keys live on the server, so anyone who gets root there can delete the bucket
contents with them. Turn on bucket versioning or Object Lock (retention mode) at the provider, and use keys that
cannot change bucket settings. The module cannot protect the remote from an attacker who holds its own keys.

### Non-interactive setup

Answers come from options or from `SOFTSYNK_ANSWER_<NAME>` environment variables. The repository password and
both S3 keys are credentials: they are accepted only through the environment (or the prompts), never as options,
because options show up in `ps`:

```sh
sudo env SOFTSYNK_ANSWER_REPO_PASSWORD='...' SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID='...' \
  SOFTSYNK_ANSWER_S3_SECRET_ACCESS_KEY='...' \
  deployment/backup/setup-backup.sh --non-interactive \
  --remote-preset r2 --remote-id ACCOUNT_ID --remote-bucket my-bucket
```

`--s3-access-key-id`, `--repo-password` and `--s3-secret-access-key` are rejected with exit 64. The remote
repository URL (`remote.repo`, for a `rest:` or `sftp:` remote) must not contain credentials either
(`user:password@host`): put `RESTIC_REST_USERNAME` and `RESTIC_REST_PASSWORD` as `KEY=VALUE` lines into
`<root>/secrets/backup/s3.env` (0600) instead.

`--non-interactive` fails with exit 64 and a message for any question that has no answer; add
`--accept-defaults` to use the defaults instead. Other options: `--local-repo`, `--remote-endpoint`,
`--remote-region`, `--remote-prefix`, `--retention-daily|weekly|twice-monthly`, `--exclude-databases a,b`,
`--root`, `--dry-run`, `--help`.

## Day to day

```sh
synk-backup --status               # health: 0 healthy, 1 degraded, 2 unhealthy, 3 unknown/stale/not installed
synk-backup --status --json        # same document as JSON, same exit codes
synk-backup --status-json          # same JSON, but always exits 0 (what Synk Server Manager reads)
synk-backup snapshots              # local repository; --from remote for the bucket; --json
synk-backup run                    # back up what is due now (the timer does this)
synk-backup run --db NAME          # one database only (skips globals, check and restore test)
synk-backup run --dry-run          # show what would happen
synk-backup validate               # read-only re-check of tools, PostgreSQL and both repositories
synk-backup check                  # restic check (5% data subset) on both repositories
synk-backup restore-test           # restore the newest snapshot into a throwaway database
journalctl -u softsynk-backup.service
```

`--status` never takes the backup lock and never calls restic, so it is safe to run while a backup is running.
Health, worst first:

- `stale` (exit 3): no backup attempt for more than 36 hours, so the timer is not running. This wins over the others.
- `unhealthy` (exit 2): the latest backup of a database failed, a tier is overdue (daily 30 hours, weekly
  8 days, twice-monthly 17 days), the integrity check or the last restore test failed, or the last run never finished.
- `degraded` (exit 1): the remote copy is failing, no remote is configured, no restore test has passed in
  40 days (or ever), or the systemd timer is not active.
- `unknown` (exit 3): configured, but no backup has run yet. `not installed` also exits 3.
- `healthy` (exit 0).

`softsynk-server --status` shows the same information for all modules.

Only one backup command runs at a time (a single lock). If one is already running, `run` prints
`synk-backup: already running` and exits 0, and other commands exit 75. Stale restic locks left by a crashed
run are cleared before each run.

Exit codes of `run`: 0 all done, 1 failed, 2 partial (some databases failed; the others are saved). 64 means a
usage or configuration error, 130 cancelled.

`softsynk-backup.service` has `OnFailure=softsynk-alert@%n.service`: when it fails, the notify module mails the
status to the recipients set with `synk-notify configure`. Without that unit nothing is mailed; `setup-backup.sh`
warns when `softsynk-alert@.service` is missing. `mail_to` and `--mail-to` from version 1.0.0 are still accepted
but ignored (a warning says so); the old `softsynk-notify@.service` is removed on the next setup run.

## Restore

Restores are never run by the timer. Look at the snapshots first:

```sh
synk-backup snapshots --from local      # or --from remote after losing the server
```

**Into a new database (safe, nothing is overwritten):**

```sh
synk-backup restore --db NAME --to-db NAME_restored                    # newest local snapshot
synk-backup restore --db NAME --to-db NAME_restored --from remote --snapshot 1a2b3c4d
```

The target must not exist. Names starting with `softsynk_restore_` are reserved.

**To a file (inspect with `pg_restore --list`, restore by hand):**

```sh
synk-backup restore --db NAME --to-file /root/NAME.dump
synk-backup restore --globals --to-file /root/globals.sql
```

The file is created with mode 0600 and must not exist yet.

**Replace the live database** (this deletes the current data):

```sh
synk-backup restore --db NAME --replace-existing --confirm-restore=NAME
```

Stop the application first: the command refuses if anything is connected. The snapshot is restored into a
temporary database first, owned by the original owner and with the original object owners and privileges, and
checked. If a role named in the dump no longer exists, the command fails before anything is changed: restore the
globals first (see below). Only then are the two databases swapped by renaming. The previous database is **not
dropped**: it stays as `softsynk_restore_old_<NAME>_<UTC time>` and keeps using disk space. The command prints
its name and the exact `DROP DATABASE` command; run that when you have checked the result. Without
`--confirm-restore`, an interactive terminal asks you to type the database name; a script must pass the flag.

**Rebuilding a lost server.** Install PostgreSQL and the module, point it at the same remote bucket with the same
repository password (`--remote-...` options, S3 keys, and the saved password), then:

1. `synk-backup snapshots --from remote`
2. Restore the globals first, so roles exist: `synk-backup restore --globals --from remote --to-file
   /root/globals.sql`, review it, then `runuser -u postgres -- psql -f /root/globals.sql postgres`. The file
   contains **password hashes of every database role**; treat it as a secret and delete it afterwards.
3. Create each database from its snapshot: `synk-backup restore --db NAME --from remote --to-db NAME`.

Caveats of a dump restore:

- The tool creates the target database from `template0` with the server's default encoding and locale, and
  restores with `--exit-on-error`, so it stops at the first error, for example a `GRANT` to a role that does not
  exist yet (hence globals first).
- `--replace-existing` restores the original owners and privileges. `--to-db` and the restore test use
  `--no-owner`: objects end up owned by the `postgres` user (re-assign them with `ALTER ... OWNER TO` if the
  application needs it). Privileges are restored.
- A `pg_dump` archive does not contain database-level settings: `ALTER DATABASE ... SET`, `GRANT CONNECT` on the
  database, and the encoding and locale. Re-apply those by hand after a restore. If the original database used a
  different encoding or locale, use `--to-file` and run `pg_restore --create` by hand.
- Extensions used by a database must be installed on the new server, in a compatible version, before the restore.

**Restore test.** The module proves the backups restore by itself: a restore test runs automatically every 30
days, rotating through the databases, and `synk-backup restore-test [--db NAME] [--from remote]` runs one on
demand. It restores the newest snapshot of one database into `softsynk_restore_test_*`, checks that the number
of restored tables matches the dump's table of contents, and always drops the test database again. Databases
named `softsynk_restore_*` (a test database that could not be dropped, or a kept `softsynk_restore_old_*`) are
never backed up.

## Files in this folder

| File | Purpose |
| --- | --- |
| `module.toml` | Manifest for the Synk Server Manager engine (`install`, `configure`, `status`, command `synk-backup`) |
| `setup-backup.sh` | Install and configure wizard (also the non-interactive entry point) |
| `synk-backup` | The tool: backup, restore, status (Python 3 standard library only) |
| `systemd/*.in` | Templates for the service, timer and failure-notice units (`@ROOT@`, `@SYNK_BACKUP@` are filled in at install) |

The configuration is stored as JSON in `<root>/etc/backup/config.json` (not TOML: the Python standard library
cannot write TOML). It holds no secrets. `synk-backup config` prints it.

## Not covered

- Application files, uploads, Redis, and the operating system.
- Point-in-time recovery (no WAL archiving): you can restore only to the time of a snapshot, up to 6 hours old.
- Cross-database consistency (see above).
- Notification beyond a failure e-mail (the `notify` module sends it; webhooks are not built).
