# Server tuning (module `optimizer`)

`synk-tune` compares the settings of PHP, PHP-FPM, Nginx, Apache, Redis and Meilisearch with what the server has:
RAM, CPUs and the memory the running processes really use. It reports what to change, why, and what it costs.
It changes exactly one thing on its own: an OPcache drop-in for PHP-FPM. Everything else is report only; you make
those changes by hand.

Requires: Ubuntu 24.04 or newer and the python3 that ships with it (3.12). Nothing is installed with apt. Services
that are not running (no Apache, no Meilisearch, ...) are skipped without a word.

## Use it

As root, on the server. `/usr/local/bin/synk-tune` exists once `bootstrap.sh` has linked the module's commands;
until then use the full path of the release, `/opt/softsynk-server-management/current/deployment/optimizer/synk-tune`.

```
/usr/local/bin/synk-tune audit
```

`audit` reads and prints; it writes nothing, not even a log. Each finding shows the current value, the proposed
value, the benefit and the risk. Findings marked "report only" cannot be applied by the tool.

```
/usr/local/bin/synk-tune
```

With no command it runs the audit, then shows the OPcache changes it can apply and asks which to take, shows the
resulting file, and applies it. Add `--dry-run` to stop after the preview. For scripts,
`synk-tune --non-interactive --accept-defaults` applies every recommended OPcache value without asking.

## What it applies: the OPcache drop-in

`/etc/php/<version>/fpm/conf.d/99-softsynk-opcache.ini`, one per running PHP-FPM version. It raises, and only
raises, three values so that the cache fits the PHP files of the apps on this server (folders under `/var/www`
and `/home/*/*` with a framework marker file, vendor code included, `current` only for zero-downtime layouts):

| Setting | Rule |
| --- | --- |
| `opcache.memory_consumption` | 128 MB, doubled until it holds about 12 KB per file; never more than an eighth of RAM (but not below 128) |
| `opcache.interned_strings_buffer` | 8 MB up to 10,000 files, 16 MB up to 30,000, 32 MB above |
| `opcache.max_accelerated_files` | the next size PHP really allocates (a prime) for the file count plus 30% |

A value you already set higher is left alone. A later file in `conf.d` (name sorting after `99-softsynk-opcache.ini`)
that sets a lower value would win over the drop-in, so that case is reported, not applied. A switched-off OPcache
(`opcache.enable=0`) is offered as a one-line change; a missing OPcache extension and
`opcache.validate_timestamps=0` are only mentioned.

Every apply goes through the safe-change engine: `php-fpm<version> -t` must pass before and after the write,
`php<version>-fpm` is reloaded (never restarted, no downtime), the new values are read back, and any failure
restores the old file and reloads again. A failure for one PHP version does not stop the others. After each
success it prints the undo command.

## Undo

```
/usr/local/bin/synk-tune rollback
/usr/local/bin/synk-tune rollback --stamp 20261104-120000
```

Restores the backup (the newest without `--stamp`) and reloads PHP-FPM. There is one backup, and one stamp, per
PHP version and run. Backups live in `/opt/softsynk-server-management/data/optimizer/backups`; the last 20 are kept.

## What it only reports

| Area | Finding | How it is decided |
| --- | --- | --- |
| PHP-FPM pool | `pm.max_children` too high for RAM (warn) | RAM minus a reserve (10%, at least 512 MB) minus 1.2 times what the other services use, shared between all running pools by their current size; divided by the biggest measured worker plus 25% (64 MB assumed when fewer than 2 workers run). Reported only when the current value is more than 20% above that. |
| PHP-FPM pool | `pm.max_children` reached | Only with log evidence in the last 7 days. A raise is never more than double; with no RAM to spare it says so. `pm.start_servers` and the spare-server values are proposed for dynamic pools. |
| PHP-FPM pool | `pm.max_requests` unset | Proposes 500. |
| php.ini | `memory_limit = -1`, `post_max_size` below `upload_max_filesize` | Proposes 256M (128M under 1.5 GB RAM) and upload size plus 2M. |
| Nginx | `worker_processes` below the CPU count, gzip off or without `gzip_types`, `client_max_body_size` below the PHP upload limit, `worker_connections` ran out (log, 7 days), open-file limit below twice the connections | Proposes the exact directive. |
| Apache | `MaxRequestWorkers` worst case above the RAM budget | Proposes the value that fits (multiplied by `ThreadsPerChild` for event and worker MPMs). |
| Redis | no `maxmemory` in `/etc/redis` | Points to `/opt/softsynk-server-management/current/softsynk-server configure redis`. |
| Meilisearch | `max_indexing_memory` not set | Proposes a quarter of RAM, at least 256 MiB. |

The memory table at the top of `audit` also lists database servers (MySQL, MariaDB, PostgreSQL) so the shares add
up; the tool makes no recommendation for them.

Recommendations follow capacity and favour stable over aggressive: nothing is raised without evidence, and no raise
is more than double the current value.

## Status

`synk-tune --status-json` (used by `softsynk-server status`): `not_installed` until an OPcache drop-in exists,
then `healthy`; `degraded` when any PHP-FPM pool reached `pm.max_children` in the last 24 hours (action:
`synk-tune`). It reads files only and always exits 0.

## Exit codes

0 ok, 2 failed (everything restored), 64 usage or not run as root, 130 cancelled.
