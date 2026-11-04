# Disk housekeeping (module `housekeeping`)

Finds what fills the disk, shows what can safely be removed and how much space that frees, and deletes it only
after you say yes. It also caps the systemd journal and sets up daily rotation for Laravel logs, so the disk
does not fill again. Nothing runs on a timer: cleanup happens when you run it.

Requires: Ubuntu 24.04 or newer, python3. `journalctl`, `apt-get` and `logrotate` are used when present.

## Commands

Run as root, on the server.

```sh
/usr/local/bin/synk-clean scan                 # read-only: disk use, largest items, large files, what can be cleaned
/usr/local/bin/synk-clean run                  # preview, ask per category, clean, report space freed
/usr/local/bin/synk-clean run --dry-run        # preview only, nothing is asked or changed
/usr/local/bin/synk-clean run --only rotated,laravel   # just these categories, one overall question
/usr/local/bin/synk-clean run --accept-defaults --non-interactive   # unattended: default categories only
/usr/local/bin/synk-clean setup                # journal cap and Laravel log rotation (asks, shows the diff, then writes)
/usr/local/bin/synk-clean --status-json        # what Synk Server Manager reads; always exits 0
```

Through the engine (as root): `softsynk-server install housekeeping` runs `setup`.

Exit codes: 0 ok, 2 something failed, 64 usage or config error, 130 cancelled.

## What can be cleaned

| Id | What | Older than | Selected by default |
| --- | --- | --- | --- |
| `apt` | downloaded `.deb` files in `/var/cache/apt/archives` (`apt-get clean`) | any | yes |
| `journal` | journal data above `journal_max` (`journalctl --vacuum-size`) | oldest first | yes |
| `rotated` | rotated logs in `/var/log` | `keep_days` | yes |
| `laravel` | `laravel-YYYY-MM-DD.log` and `laravel*.gz` in each detected app's `storage/logs` | `keep_days` | yes |
| `crash` | `/var/crash` and `/var/lib/systemd/coredump` | `crash_keep_days` | yes |
| `caches` | Composer and npm caches of root and `/home/*` (downloaded again when needed) | any | no |

`run` measures each category first and prints the table with the recoverable size. Categories with nothing to
remove are not offered. After cleaning it prints the space actually freed next to the estimate and writes
`<root>/state/housekeeping/last-run.json`.

Rotated logs are matched by anchored patterns only: `*.log.[0-9]`, `*.log.[0-9].gz`, `*.[0-9].gz` and `*.old`.
A live log (`syslog`, `auth.log`, `laravel.log`) is never matched. The patterns also match rotated `dpkg.log.N`
and apt history files, which are deleted after `keep_days` too.

Caches are deleted file by file; the empty directories stay.

## Reported only, never deleted

`scan` also lists, without offering to delete them: old softsynk releases under `<root>/releases` (not
`current` or `previous`), packages `apt autoremove` would remove (this includes old kernels), whether Docker
or snap are installed (unused images, volumes and old snap revisions are not touched), and softsynk change
backups older than 90 days.

## Safety

- Only regular files are deleted. Symlinks are never followed or removed, including a symlinked root or a
  directory replaced by a symlink while the walk runs.
- A walk stays on the filesystem it started on.
- Protected paths are never walked: `/etc /boot /usr /bin /sbin /lib /lib32 /lib64 /libx32`, the database
  data in `/var/lib/mysql`, `/var/lib/postgresql`, `/var/lib/redis`, `/var/lib/meilisearch`, `/var/log/mysql`,
  `/var/log/journal` (the journal is trimmed with `journalctl` only), and the management root's `etc`, `data`,
  `secrets` and `state`.
- Each file is checked again right before it is removed (still a regular file, same inode, size and time).
  The removal goes through the open directory, so a swap during the walk is not followed. The estimate in the
  preview is the same walk without the delete, so it matches what is removed.
- A category that fails (for example `apt-get clean` finding the dpkg lock held) is reported and the others
  still run; the exit code is 2.
- `run` and `setup` hold the housekeeping lock. A second one started meanwhile stops at once (exit 2,
  "lock held"). `scan` and `run --dry-run` need no lock.
- Known limit: a Laravel log folder is writable by the app's user. The folder is resolved once at the start,
  so a user who swaps a parent directory within that instant could redirect the walk. Only rotated and Laravel
  log names, older than `keep_days`, can match, which limits what is reachable.

## Laravel apps

An app is a real directory under `app_roots` with an `artisan` file and a `storage/logs` folder inside it.
`setup` writes one `laravel.log` rotation stanza per app (daily, compressed, `copytruncate`, owner taken from
the folder).

Zero-downtime layouts (Forge, Envoyer: the site folder holds `current -> releases/N` and a shared `storage`)
are not detected: the site folder has no `artisan` file, and an app whose `storage` is a symlink to a folder
outside it is skipped with a note. Their logs are not cleaned or rotated by this module; handle them by hand.

Apps with unusual path or owner names (anything but letters, digits and `_ . / @ + -`) are not written into the
logrotate file.

## Setup

`setup` proposes two things and lets you edit each value:

1. Journal cap: `/etc/systemd/journald.conf.d/50-softsynk.conf` with `SystemMaxUse=` (default `500M`);
   `systemd-journald` is restarted and must be active afterwards.
2. Laravel log rotation: `/etc/logrotate.d/softsynk-laravel` (default 14 days kept, only when apps were found);
   `logrotate -d` must accept it.

It shows the diff, asks once, then applies each file with a backup. If a check fails, that file is put back and
the exit code is 2. The chosen values are saved to the config. Running it again with the same answers changes
nothing. Non-interactively, `setup` needs `--accept-defaults`.

Under `softsynk-server install housekeeping --dry-run` nothing is written (the engine sets `SOFTSYNK_DRY_RUN=1`).

## Configuration

`<root>/etc/housekeeping/config.json` (JSON, no secrets). Every key is optional; unknown keys and wrong
types are refused with exit 64.

| Key | Default | Meaning |
| --- | --- | --- |
| `keep_days` | `30` | age limit for `rotated` and `laravel`; minimum 28 |
| `crash_keep_days` | `7` | age limit for `crash`; minimum 1 |
| `journal_max` | `"500M"` | journal cap, a whole number with optional K, M, G or T |
| `laravel_rotate_days` | `14` | days of `laravel.log` kept by logrotate |
| `large_file_mb` | `100` | `scan` lists files of at least this size |
| `app_roots` | `["/home/*/*", "/var/www/*"]` | absolute paths or globs where Laravel apps live |

`<root>` is the management root, `/opt/softsynk-server-management` by default (`--root` or `SOFTSYNK_ROOT`).

## Status

`--status-json` reports `not_installed` until the journal cap or the logrotate file exists, then `healthy`.
It is `degraded` when the config is invalid, the last-run record is unreadable, or the last run had errors.
The summary shows when the last cleanup ran and how much it freed.

## Files in this folder

| File | Purpose |
| --- | --- |
| `module.toml` | Manifest for the Synk Server Manager engine (`install` and `configure` run `setup`, `status`, command `synk-clean`) |
| `synk-clean` | The tool (Python 3 standard library plus `lib/softsynk`) |

## Not covered

- Application data, uploads, databases, backups, Docker images and volumes, snap revisions, old kernels
  (reported only).
- Cleaning on a schedule. Run it by hand or from your own timer.
- Sparse and hard-linked files: sizes are the apparent file size, so a sparse or hard-linked file can free
  less than estimated. `scan` measures large files by allocated size.
