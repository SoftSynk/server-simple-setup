# Health monitor (module `monitor`)

Checks disk, inodes, memory, swap, cpu, the services you name and failed systemd units every 5 minutes, keeps a
week of history, and mails you through `synk-notify` when something needs attention and when it is over. The mail
says what is wrong, which resources are affected, the thresholds, and what to run about it. `synk-dash` shows the
same data live in the terminal.

Requires: Ubuntu 24.04 or newer and the python3 that ships with it (3.12). Nothing is installed with apt. Mail needs
the `notify` module to be configured; without it the monitor still checks and records, and says so in `--status`.

## Set it up

As root, on the server, after `softsynk-server install monitor` or directly:

```
/usr/local/bin/synk-notify configure
/usr/local/bin/synk-monitor setup
/usr/local/bin/synk-monitor test-alert
```

`setup` proposes the services it finds running (nginx, php-fpm, mysql, redis and so on); `--services nginx,mysql`
names them instead and `--interval 300` changes how often it checks (30 to 3600 seconds). Running it again keeps your
config and changes nothing when the answers are the same. It writes the config, installs
`softsynk-monitor.service` and `softsynk-monitor.timer`, enables the timer and runs one first check.
`test-alert` sends a mail marked `TEST:` and changes no state.

`/usr/local/bin/synk-monitor` and `/usr/local/bin/synk-dash` exist once `bootstrap.sh` has linked the module's
commands. Until then use the release folder, `/opt/softsynk-server-management/current/deployment/monitor/`.

## Commands

| Command | What it does |
| --- | --- |
| `synk-monitor setup` | choose the watched services, install and start the timer |
| `synk-monitor check` | one check (the timer runs this) |
| `synk-monitor report [--send]` | the current state as the mail shows it; `--send` mails it |
| `synk-monitor test-alert` | a marked sample alert |
| `synk-monitor --status` | health, read-only; exit 0 healthy, 1 degraded, 2 unhealthy, 3 unknown, stale or not set up |
| `synk-dash` | live view; `q` quits, `r` refreshes, `t` flips 24 hours and 7 days |
| `synk-dash --once` | one plain-text page (also used when stdout is not a terminal) |

`synk-dash` only reads. It samples `/proc` itself and takes services, alerts and history from what `check` wrote;
the monitor line turns `STALE` when the last check is older than three intervals.

## Alerts

A level is raised after `sustain` samples in a row at or above its threshold (disk, inodes and services alarm at
once) and cleared after `recover_checks` samples in a row below the threshold minus a margin (5 points for cpu,
memory and swap, 2 for disk and inodes), so a value hovering around the line cannot flap. You get:

- one mail when trouble starts (`open`), one when it gets worse (`escalate`),
- one reminder every `notify_interval_hours` while it lasts, nothing in between,
- one mail when it is over (`recover`), and a summary every `summary_every_days`.

A mail that could not be sent is tried again every 15 minutes; a newer message replaces an older undelivered one.

## Config

`etc/monitor/config.json` under the management root. Keys you leave out keep their default; a value outside its range
is refused with the reason. All thresholds are percent.

| Key | Default | Meaning |
| --- | --- | --- |
| `check_interval_sec` | 300 | timer interval (re-run `setup` after changing it so the timer follows) |
| `thresholds.cpu` | warn 85, crit 95, sustain 3 | |
| `thresholds.memory` | warn 85, crit 95, sustain 2 | |
| `thresholds.swap` | warn 80, crit 95, sustain 2 | |
| `thresholds.disk`, `thresholds.inodes` | warn 80, crit 90, sustain 1 | per mounted filesystem |
| `thresholds.services.sustain` | 1 | samples a watched service must be down |
| `thresholds.failed_units.sustain` | 2 | samples with failed units before alerting |
| `services` | `[]` | units that must run, e.g. `["nginx.service"]` |
| `ignore_units` | `[]` | failed units that are not counted |
| `exclude_mounts` | `[]` | mount points that are not watched |
| `recover_checks` | 2 | calm samples in a row before a level clears |
| `notify_interval_hours` | 6 | reminder interval while trouble lasts |
| `summary_every_days` | 7 | `0` switches the summary mail off |
| `history.healthy_every_min` | 60 | one snapshot per hour while healthy |
| `history.unhealthy_every_min` | 5 | one every 5 minutes while not |
| `history.retention_days` | 7 | whole days older than this are deleted |
| `history.max_mb` | 1024 | oldest days are deleted first when the folder is bigger |

## Where things live

| Item | Path under the management root |
| --- | --- |
| Settings | `etc/monitor/config.json` |
| Last check (what `synk-dash` and `--status` read) | `state/monitor/latest.json` |
| Alert state (open incident, recent recoveries, mails waiting) | `state/monitor/alerts.json` |
| History | `data/monitor/history/YYYY-MM-DD.jsonl`, one JSON object per line |
| Log | `log/monitor/YYYY-MM.jsonl`, kept 90 days |
| Units | `/etc/systemd/system/softsynk-monitor.service`, `softsynk-monitor.timer` |

If the state folder cannot be written (disk full, read-only), the two state files go to `/run/softsynk-monitor` and
readers use the newer copy; the check never crashes over it. A corrupt history line is skipped. History pauses, and
`--status` warns, when its filesystem has less than 64 MB free or one day alone is over `max_mb`.

## When the monitor itself fails

The service unit carries `OnFailure=softsynk-alert@%n.service` only when `softsynk-alert@.service` already exists,
which `synk-notify configure` creates. If you configure mail after `setup`, run `/usr/local/bin/synk-monitor setup`
again so the unit picks it up; the setup warns about this when mail is not configured. Without it, a crashing check
is visible only in `--status` (`stale`) and in `systemctl status softsynk-monitor.service`.

## Limits

- Percentages come from `/proc` and `statvfs`; there is no per-process history.
- The mail names the biggest directories and processes at the moment it is sent, and recommends commands. It never
  runs anything on its own.
- Plain-text mail only (see the `notify` module).
