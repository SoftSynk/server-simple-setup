# setup-redis.sh

An interactive, idempotent script that installs and configures Redis on Ubuntu and Debian servers that use systemd. It is the `redis` module of softsynk-server: `softsynk-server install redis` runs it, and `softsynk-server --status` reads its `--status-json`.

On the first run it installs Redis (after asking) and sets up either one shared instance or two instances: one for cache and one for queues. On later runs it shows the current state and offers only the actions that still apply. Before each change it shows a plan and asks for approval. It backs up the files, checks the new config, applies the change while Redis keeps running where possible, and then confirms the result.

It never overwrites an existing config, service or data without an explicit "yes".

---

## Contents

1. [Architecture](#architecture)
2. [Supported environments and prerequisites](#supported-environments-and-prerequisites)
3. [Usage](#usage)
4. [First run and reruns](#first-run-and-reruns)
5. [Single vs dual instance](#single-vs-dual-instance)
6. [Decisions and defaults](#decisions-and-defaults)
7. [Service names, paths and ports](#service-names-paths-and-ports)
8. [Connecting Laravel](#connecting-laravel)
9. [Health checks and the summary](#health-checks-and-the-summary)
10. [Module status (`--status-json`)](#module-status---status-json)
11. [Logs, troubleshooting and recovery](#logs-troubleshooting-and-recovery)
12. [Reusing the script on other servers](#reusing-the-script-on-other-servers)
13. [Limitations and testing status](#limitations-and-testing-status)

---

## Architecture

```
            ┌──────────────── one server ────────────────┐
 Laravel ──▶│ CACHE  redis-server.service        :6379   │  cache, no disk writes
 (PHP-FPM,  │        /etc/redis/redis.conf               │
  workers)  │                                            │
         ──▶│ QUEUE  redis-server@queue.service  :6380   │  jobs, sessions, locks
            │        /etc/redis/redis-queue.conf         │  RDB snapshots on disk
            └────────────────────────────────────────────┘
               both bound to 127.0.0.1 / ::1 only
```

Each instance has its own systemd unit, config file, memory limit, eviction policy, persistence setting and data directory. If the cache fills up, it can never block jobs, and cache data never adds disk I/O.

**How the script edits configs.** It never rewrites a stock config. All of its settings go in one marked block at the end of the file:

```
# >>> setup-redis.sh managed block (rerun setup-redis.sh instead of editing by hand)
# role=queue
maxmemory 256mb
maxmemory-policy noeviction
save ""
save 30 10
save 60 1
...
# <<< setup-redis.sh managed block
```

Redis applies the last value of a directive it reads, so this block overrides anything earlier in the file, and the rest of the file is left as it was. On a rerun the block is replaced, never duplicated. The `# role=` line is how the script recognises the instances it manages. The script never runs `CONFIG REWRITE`, so Redis never reformats your file.

**How the script applies a change.**

1. Collect your answers and show a plan: each setting as *current → new*, plus a count of unchanged settings.
2. Ask "Apply this plan?" Nothing has changed up to this point.
3. Check the new config by starting a throwaway `redis-server` as user `redis`. It listens only on a temporary unix socket, has persistence off, and ignores `dir`, `logfile` and `pidfile`. If Redis rejects the file, nothing is written.
4. Back up the existing file to `<file>.bak-YYYYMMDD-HHMMSS` (with `-2`, `-3`… added if the same file changes twice in one run).
5. Write the new config file atomically (write to a temporary file, then rename it). The file goes first, so an interrupted run never leaves a new password only in memory.
6. Change the running Redis with `CONFIG SET`, so no restart is needed. The password is changed last.
7. If a setting can't be changed while Redis runs, ask before restarting. If Redis doesn't come back after the restart, the backup is restored and Redis is restarted again with it.

---

## Supported environments and prerequisites

| | |
|---|---|
| OS | Ubuntu 20.04+ and Debian 11+ (anything with `ID` or `ID_LIKE` of `debian`/`ubuntu`) |
| Init | systemd |
| Shell | Bash 4.4 or newer (the default on all supported releases) |
| Access | root (`sudo`) for setup; `--status-json` works without root. The prompts need an interactive terminal and gum (installed on the first interactive run); a `--non-interactive` run needs neither |
| Redis | 6.0 or newer, from packages.redis.io or the distribution's `redis-server` package. The script refuses older versions |
| Network | Only needed when Redis is not installed yet (for apt) |

Other tools the script uses: `redis-cli`, `systemctl`, `ss`, `flock`, `runuser`, `install`, `stat`, `od`. All of these come with the OS or the Redis package.

A real run refuses other operating systems, a missing systemd, and a non-root user. In `--dry-run` these become warnings, so a dry run also works on a laptop. Without a terminal, a run stops with exit 64 at its first question unless `--non-interactive --accept-defaults` is given.

---

## Usage

```bash
sudo softsynk-server install redis
```

Or run the script directly from a checkout or release. It sources `../../lib/ui.sh` (the shared prompts), so keep the repository layout (`deployment/redis-setup/` next to `lib/`) when you copy it; the folder alone is not enough:

```bash
sudo ./deployment/redis-setup/setup-redis.sh
```

| Option | What it does |
|---|---|
| *(none)* | Interactive setup (first run) or the maintenance menu (later runs) |
| `--dry-run` | Asks every question and shows every plan and check, but changes nothing: no config files, no runtime settings, no services, no sysctl. It takes no lock and writes no log, but still starts the throwaway validation server, so the config check is real |
| `--status-json` | Prints the module status as one JSON document (see [Module status](#module-status---status-json)) and exits 0. Read-only: no lock, no log, no install, no prompts. Works without root and without gum, but root sees the most |
| `--non-interactive` | Never prompts. A question with no pre-supplied answer exits 64, unless `--accept-defaults` is also given |
| `--accept-defaults` | Takes each question's default (a kept value on an existing instance) when the run is non-interactive: `--non-interactive`, or no terminal. It never confirms a data-loss step or supplies a password |
| `--confirm-ACTION=NAME` | Confirms one data-loss step in a non-interactive run: `--confirm-no-persistence=NAME` (no persistence on a queue or shared instance) and `--confirm-eviction=NAME` (an evicting policy on one). `NAME` is the instance name, such as `queue`. Interactively you type the name instead |
| `--root DIR` | Management root for the log and lock (default `$SOFTSYNK_ROOT`, else `/opt/softsynk-server-management`). Must be an absolute path |
| `--self-test` | Checks the script's internal helpers (size parsing, config block handling). Needs no root and no Redis, and runs on macOS too |
| `--help` | Shows the usage text |

An unknown option exits 64. The same switches can come from the environment (`SOFTSYNK_NONINTERACTIVE=1`, `SOFTSYNK_ACCEPT_DEFAULTS=1`, `SOFTSYNK_DRY_RUN=1`, `SOFTSYNK_ROOT`), which is how softsynk-server passes them.

Run it with `--dry-run` first on any server that already has data.

**Non-interactive passwords.** The password prompts take their answers from the environment: `SOFTSYNK_ANSWER_pw` and `SOFTSYNK_ANSWER_pw2` (the same value twice). They are never defaulted, never printed and never put on a command line; use `sudo env` or a protected environment file, not shell history. Other questions have no stable names, so use `--accept-defaults` for them.

**Answering prompts.** Press Enter to take the default, which is shown in brackets or marked with `*`. Yes/no questions show the default in capitals: `[Y/n]` means Enter answers yes. Every question that could lose data or expose Redis defaults to **No**. On an existing instance, pressing Enter at each question keeps its current value, and *Apply this plan?* needs an explicit `y`.

---

## First run and reruns

**First run** (Redis not installed, or no instance managed by the script yet):

1. **Discovery.** Shows the OS, RAM, CPU count, Redis version, each instance (unit, port, state, enabled at boot, role), `vm.overcommit_memory` and the transparent hugepage mode.
2. **Installation**, only if Redis is missing. It asks `Install Redis now? [Y/n]`, adds the official packages.redis.io APT repository (with a signed key) and installs `redis`. If that repository has no build for your release, it offers the distribution's `redis-server` package instead.
3. **Guided setup.** Choose single or dual mode, then answer these questions for each instance: port, memory, eviction, persistence, network, password, three optional groups, and systemd.
4. **Kernel setting.** Offers `vm.overcommit_memory=1`, only if some instance saves data to disk.
5. **Maintenance menu**, then health checks and the final summary.

**Reruns** go straight to the maintenance menu, which only lists actions that apply:

- *Set up instances*: shown only while the cache+queue pair (or a shared instance) is incomplete.
- *Review and tune* each instance. Each question offers *Keep current (…)* as the default, and settings that already match are reported as unchanged.
- *Set vm.overcommit_memory=1*: shown only while it is not already 1.
- *Run health checks*.
- *Finish*: verify everything and print the summary.

A rerun where you keep every answer changes nothing and reports `Changes: 0 applied, N unchanged`.

**Existing instances that the script didn't create** (for example a hand-configured Redis) are adopted, never replaced:

- The script guesses each one's role from its settings: no persistence or an evicting policy suggests *cache*; otherwise *queue*.
- It uses that guess as the default answer.
- Its port is never changed.
- Its data stays where it is.

---

## Single vs dual instance

| | Single (one shared instance) | Dual (recommended) |
|---|---|---|
| Instances | `redis-server` on 6379 | cache on 6379, queue on 6380 |
| Memory | one limit for everything | separate limits |
| Full cache | can block queue writes | affects only the cache |
| Disk I/O | cache data is saved to disk too | only queue data is saved |
| Restarting cache | loses queued jobs too | jobs are not affected |
| Good for | small apps, under about 2 GB RAM, light queues | production apps with real queue traffic |

---

## Decisions and defaults

These defaults suit a typical Laravel server (for example one provisioned by Laravel Forge).

### Memory (`maxmemory`)

| Role | Recommended share | 2 GB server | 4 GB | 8 GB |
|---|---|---|---|---|
| cache | 25% of RAM | 512 MB | 1 GB | 2 GB |
| queue | 12.5% of RAM | 256 MB | 512 MB | 1 GB |
| shared | 25% of RAM | 512 MB | 1 GB | 2 GB |

Values are rounded to the nearest 128 MB, with a minimum of 128 MB. In dual mode this leaves more than 60% of RAM for the OS, PHP-FPM and the database, plus headroom for:

- copy-on-write while Redis forks to save a snapshot;
- fragmentation (real memory use is often 1.2–1.5× Redis's `used_memory`).

You can accept the recommendation, enter your own value (`512mb`, `2gb`, `0` = unlimited) or keep the current one. The script refuses:

- values under 32 MB;
- values over 90% of RAM;
- values below current usage plus 20%, because writes would start failing at once.

It asks for extra confirmation above 50% of RAM, or for `0` (unlimited, which can lead to the kernel's OOM killer stopping Redis).

### Eviction (`maxmemory-policy`)

The default for **both** instances is `noeviction`: Redis never deletes keys on its own.

> **Warning:** with `noeviction`, once an instance reaches its memory limit, **every write fails** with an OOM error. In Laravel, `dispatch()`, `Cache::put()` and session writes throw exceptions. Monitor `used_memory` (the health check warns above 90%), or pick `allkeys-lru` for the cache instance.

- For a **cache-only** instance, `allkeys-lru` is usually the better choice. When full, it deletes the least recently used keys instead of failing. The script explains this and lets you choose.
- On a **queue** or **shared** instance, an evicting policy needs extra confirmation, because it can silently delete queued jobs, sessions or locks.

### Persistence

| Option | Default for | Data lost in a crash | Disk I/O |
|---|---|---|---|
| None | cache | everything (it's a cache) | none |
| RDB snapshots, `save 30 10 60 1` | queue, shared | up to about 60 s of writes | low |
| AOF `everysec` + hourly RDB | optional | up to about 1 s | higher |

`save 30 10 60 1` means: take a snapshot 30 seconds after 10 or more writes, or 60 seconds after any write. A clean restart or reboot loses nothing, because Redis saves on shutdown. Choose AOF when losing up to a minute of jobs after a crash is not acceptable.

Persistence switches are made while Redis keeps running, without losing data:

- **Switching to AOF:** the script waits until the background rewrite of the AOF file has finished.
- **Switching from AOF to RDB:** the script takes a fresh snapshot and waits for it to complete. Only after that does the next step run.

- **Switching to None** on an instance that saved data before: Redis no longer saves on shutdown, so the old `dump.rdb` would be loaded again at the next start and serve stale data. The script offers to rename it to `dump.rdb.disabled-STAMP`.

Choosing *None* on a queue instance needs extra confirmation. If the first AOF rewrite fails or takes longer than 5 minutes, the script will not restart Redis, because a restart could then load an incomplete AOF.

### Security

- **Network:** recommends `bind 127.0.0.1 -::1` (Redis 7+) or `bind 127.0.0.1 ::1` (Redis 6) with `protected-mode yes`, so only processes on this server can connect. If you decline, the script warns you to firewall the port.
  - Redis 7 changes this while running.
  - Redis 6 needs a restart, and the script asks before restarting.
  - If an existing instance already listens on other addresses, the default answer is No, so remote clients are not cut off by accident.
- **Password (`requirepass`):** options are keep, generate (48 hex characters, 192 random bits), enter your own (16+ characters, no spaces, quotes or backslashes, typed without echo) or remove (needs confirmation).
  - The password is stored only in the config file, which is mode 640 and owned by `redis:redis`.
  - It is never printed, logged, or passed as a command-line argument. `redis-cli` gets it through the `REDISCLI_AUTH` environment variable, and `CONFIG SET` is sent on stdin.
  - The plan shows only `(set)` or `(not set)`.
  - Read a generated password with:

    ```bash
    sudo grep '^requirepass' /etc/redis/redis-queue.conf
    ```

### Optional groups (each asked separately, default Yes, no restart needed)

| Group | Settings | Why |
|---|---|---|
| Clients | `timeout 0`, `tcp-keepalive 300` | Keeps idle queue-worker connections open and detects dead peers |
| Background freeing | `lazyfree-lazy-eviction/expire/server-del/user-del yes` | Large deletes don't block Redis |
| Logging | `loglevel notice`, `slowlog-log-slower-than 10000`, `slowlog-max-len 128`, `latency-monitor-threshold 100` | `SLOWLOG GET` and `LATENCY LATEST` give useful data when something gets slow |

### systemd

The script reads the unit's `Restart` and `LimitNOFILE` settings. If restart-on-failure is missing, or `LimitNOFILE` is below 10032 (the number of files `maxclients 10000` needs), it offers a drop-in at `/etc/systemd/system/<unit>.d/setup-redis.conf` with `Restart=always`, `RestartSec=2` and `LimitNOFILE=65535`. It warns if the unit runs as root.

### Kernel

`vm.overcommit_memory=1` is the only kernel change the script makes. It is offered only when some instance saves to disk, because without it the kernel can refuse the fork Redis needs for background saves. It is written to `/etc/sysctl.d/90-redis.conf` and applies at once.

Transparent hugepages are only reported, never changed.

No firewall rules are touched.

---

## Service names, paths and ports

On a fresh server in dual mode, the packaged `redis-server` (port 6379) is adopted as **cache**, and a new `redis-server@queue` is created on 6380.

| | Cache (dual) / shared (single) | Queue (dual) | Any new instance NAME |
|---|---|---|---|
| Unit | `redis-server.service` | `redis-server@queue.service` | `redis-server@NAME.service` |
| Config | `/etc/redis/redis.conf` | `/etc/redis/redis-queue.conf` | `/etc/redis/redis-NAME.conf` |
| Data | `/var/lib/redis` | `/var/lib/redis/queue` | `/var/lib/redis/NAME` |
| Log | `/var/log/redis/redis-server.log` | `/var/log/redis/redis-server-queue.log` | `/var/log/redis/redis-server-NAME.log` |
| Port | 6379 | 6380 | your choice (1024–65535) |

The `redis-server@.service` template ships with Debian and Ubuntu packages. If it is missing (some packages.redis.io builds), the script installs one based on the packaged unit, with the same hardening.

New instance logs follow the `redis-server*.log` naming, so the package's logrotate rule rotates them too.

Script files:

| Path | Purpose |
|---|---|
| `<root>/log/redis/setup-redis.log` | Session log of every real run (never contains secrets). `<root>` is `--root`, `$SOFTSYNK_ROOT` or `/opt/softsynk-server-management`. A dry run writes none |
| `<config>.bak-YYYYMMDD-HHMMSS` | Backup taken before each change |
| `dump.rdb.disabled-YYYYMMDD-HHMMSS` | Old snapshot renamed after persistence was turned off |
| `<config>.failed-YYYYMMDD-HHMMSS` | Config of a new instance that failed to start, kept for inspection |
| `/etc/sysctl.d/90-redis.conf` | `vm.overcommit_memory=1` |
| `<root>/lock/redis.lock` | Prevents two real runs at the same time. A dry run takes no lock |

---

## Connecting Laravel

**Dual mode with the defaults** (cache on 6379, queue on 6380). Laravel's `default` Redis connection carries queues, sessions and locks, so point it at the queue instance:

```dotenv
REDIS_HOST=127.0.0.1
REDIS_PORT=6380            # default connection -> queue instance
REDIS_CACHE_PORT=6379      # cache connection   -> cache instance
REDIS_PASSWORD=null        # or the generated password
CACHE_STORE=redis          # CACHE_DRIVER on Laravel 10 and older
QUEUE_CONNECTION=redis
SESSION_DRIVER=redis
```

In `config/database.php`, give the `cache` connection its own port:

```php
'cache' => [
    // ...
    'port' => env('REDIS_CACHE_PORT', env('REDIS_PORT', '6379')),
],
```

Then run `php artisan config:cache` and restart the queue workers (`php artisan queue:restart`).

**Single mode:** set `REDIS_PORT=6379` only.

> **Existing servers may use the opposite mapping:** queue on 6379 (`redis-server`) and cache on 6380 (`redis-server@cache`). The script detects this and never moves ports. On such a server, use **Review and tune** on each instance rather than *Set up instances*, and keep its existing `.env`. The 6379-cache / 6380-queue default applies to new servers only.

---

## Health checks and the summary

Health checks run from the menu and always before the summary. For each instance they check:

- the unit is active (an error if it is enabled or managed but not running) and answers `PING`;
- every setting in the managed block matches the running value (a difference usually means a restart is pending);
- the last RDB save succeeded, and AOF writes succeed when AOF is on;
- it isn't listening on the network without a password and protected mode (that is an error; a firewall reminder is a warning);
- memory use is below 90% of `maxmemory` (a warning above that);
- `vm.overcommit_memory` is 1 and transparent hugepages aren't `always` (warnings).

The summary is built from the live state of the server, not from your answers:

```
Redis Setup Summary
────────────────────────────────
Version:       7.4.1
Mode:          Dual Instance

CACHE
  Port:        6379
  Memory:      2 GB
  Eviction:    noeviction
  Persistence: None
  Status:      Running

QUEUE
  Port:        6380
  Memory:      1 GB
  Eviction:    noeviction
  Persistence: RDB (30 10 60 1)
  Status:      Running

Health Checks: PASSED
Changes:       6 applied, 14 unchanged
Result:        SUCCESS
────────────────────────────────
```

The RAM table above uses nominal sizes. A "8 GB" server reports a little less RAM (about 7.8 GB), so the recommendation is rounded to the nearest 128 MB below it, for example 1.9 GB instead of 2 GB.

`Result` is `SUCCESS` only if no step failed and the health checks passed. It is `DRY RUN` in dry-run mode and `FAILED` otherwise, and the exit code is non-zero.

---

## Module status (`--status-json`)

`./setup-redis.sh --status-json` prints one JSON line in the softsynk-server status format (schema 1) and exits 0 whatever it finds. It takes no lock, writes no log or temporary file, never installs anything and never prompts. For each instance it reads the config file and the unit state, then sends only `PING`, `INFO` and `CONFIG GET` to `127.0.0.1`. The password goes to `redis-cli` through `REDISCLI_AUTH` and is never printed; the output says only whether one is set.

Root sees the most: the configs (mode 640, `root:redis`) hold each instance's port, role and password. Without root, an unreadable config makes the health `unknown` with the message `cannot read ... (run as root)`, never a crash.

| Field | Meaning |
|---|---|
| `install_state` | `installed` (an instance has this script's managed block), `adopted` (Redis runs but the script has not managed it), `not_installed`, or `unknown` (a config could not be read) |
| `health` | `healthy`; `degraded` (over 90% of `maxmemory`); `unhealthy` (an enabled or managed unit is not running, no answer to `PING`, a failed RDB save or AOF write, or listening on the network with no password and protected mode off); `unknown` (systemd, `redis-cli` or a config gave no answer); `unsupported` (not Ubuntu or Debian) |
| `details` | `redis_version`, then per instance `NAME.role`, `.port`, `.state`, `.enabled`, and for a running one `.password_set`, `.used_memory`, `.maxmemory`, `.keys` |
| `warnings`, `actions` | What was found, and what to run about it. Notes (not changing the health) cover non-local binds, `vm.overcommit_memory` not 1 and transparent hugepages `always` |

It does not compare the running values with the managed block; the maintenance menu's health checks do that.

---

## Logs, troubleshooting and recovery

**Where to look**

```bash
sudo tail -n 50 /opt/softsynk-server-management/log/redis/setup-redis.log
```

```bash
sudo journalctl -u redis-server@queue -n 50 --no-pager
```

```bash
sudo tail -n 50 /var/log/redis/redis-server-queue.log
```

| Symptom | Likely cause and fix |
|---|---|
| `Another setup-redis.sh run is in progress` | Another run holds the lock. Wait for it, or check with `ps aux \| grep setup-redis` |
| `Redis rejected the new config` | The validation server's error is shown. Nothing was written. Usually a typo in a custom value or a directive your Redis version doesn't know |
| `unbalanced managed-block markers` | Someone edited the markers by hand. Fix or delete the block in the config, then rerun |
| New instance `did not start` | The unit was disabled and its config moved to `.failed-STAMP`. Check the journal lines shown; often a port or permission problem |
| Health: `X is 'a' but the config says 'b'` | That setting needs a restart that was postponed. Restart the unit when convenient |
| Health: `last RDB save failed` | Usually a full disk, wrong permissions on the data dir, or `vm.overcommit_memory=0` |
| Laravel: `OOM command not allowed` | A `noeviction` instance reached `maxmemory`. Raise it, clear stale keys, or use `allkeys-lru` on the cache |
| Laravel: `NOAUTH` | A password was set but `REDIS_PASSWORD` was not updated. Fix `.env`, then run `config:cache` and `queue:restart` |

**Rolling back by hand.** Each change leaves a timestamped backup next to the file:

```bash
ls -l /etc/redis/*.bak-*
```

```bash
sudo cp -a /etc/redis/redis-queue.conf.bak-20261104-101500 /etc/redis/redis-queue.conf
```

```bash
sudo systemctl restart redis-server@queue
```

To roll back a systemd drop-in, delete `/etc/systemd/system/<unit>.d/setup-redis.conf` and run `sudo systemctl daemon-reload`. To roll back the kernel setting, delete `/etc/sysctl.d/90-redis.conf` and run `sudo sysctl -w vm.overcommit_memory=0`.

Backups are never deleted automatically. Remove old ones yourself when you no longer need them.

---

## Reusing the script on other servers

The script has no server-specific values. Everything comes from discovery: RAM, CPU, Redis version and existing instances. To use it on another server:

1. Install softsynk-server on the server (or copy the repository layout: `deployment/redis-setup/` and `lib/`).
2. Run it with `--dry-run` and read the plans.
3. Run it for real.

To change the defaults for a team, edit the constants at the top of `setup-redis.sh` (`RDB_QUEUE_SAVE`, paths) and the share percentages in `recommend_mb`, then run `--self-test`.

---

## Limitations and testing status

**Limitations (by design):**

- Ubuntu and Debian with systemd only. No RHEL, Alpine, containers or macOS.
- Standalone Redis only. No Sentinel, Cluster, replication or TLS.
- The script does not manage ACL users (only `requirepass`), firewall rules, transparent hugepages or Redis version upgrades.
- Unattended runs are limited: `--non-interactive --accept-defaults` takes the defaults, passwords come from `SOFTSYNK_ANSWER_pw`, and data-loss steps need their `--confirm-...` option. Other answers cannot be pre-supplied by name.
- It never moves an existing instance to another port and never migrates data between instances.
- Redis 6.0 is the oldest supported version. On Redis 6, changing `bind` needs a restart.
- The package's logrotate rule may not cover log files with other names that you created by hand (for example `redis-cache.log`).

**What was tested:** `bash -n` (syntax), ShellCheck 0.11 with no warnings, and `--self-test` (size parsing, rounding of recommendations, config-block replace and reload, local-bind detection), all on macOS with Bash 3.2 and `/bin/bash`. `tests/deployment/test_status.py` also runs `--status-json` against stubbed `systemctl`, `redis-cli` and friends (healthy, stopped, failed save, memory, open bind, unreadable config, adopted, not installed, no password in the output) and checks that softsynk-server accepts the output.

**What still needs a real Ubuntu or Debian server.** It was deliberately not run against a live Redis or systemd. Before relying on it, check these on a disposable VM:

1. Fresh install from packages.redis.io, and the fallback to the distribution package.
2. Dual setup on a fresh server: the default unit is adopted as cache, and `redis-server@queue` is created on 6380.
3. A rerun with all answers kept reports 0 applied.
4. Persistence switches None → RDB → AOF → RDB on an instance holding data. The key count must stay the same.
5. Setting, changing and removing a password. Check that it never appears in `<root>/log/redis/setup-redis.log`, `ps` or the journal.
6. Restart rollback: give a value the config check accepts but that makes Redis fail to start, for example a `dir` without permissions.
7. `--dry-run` leaves `/etc`, `/var/lib/redis` and the running settings unchanged. Compare `CONFIG GET *` output and file checksums before and after.
8. Adopting an existing reversed layout (queue on 6379, cache on 6380) without changing ports.
