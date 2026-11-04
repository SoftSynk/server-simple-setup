# setup-auto-updates.sh

An interactive, idempotent script that turns on automatic updates on Ubuntu servers that use systemd.

Once it has run, the server installs security updates every day without anyone logging in. You can also include the distribution's stable updates and chosen third-party repositories, such as PHP and nginx. The script can schedule a reboot at a quiet hour, but only when an update needs one. Services are restarted after library updates.

Every setting lives in one file that the script manages. The distribution's own files are never edited. Reruns show the current state, keep your previous answers when you press Enter, and change only what differs. Before writing anything, the script shows a diff, asks for approval, checks the syntax with apt and makes a backup. After writing, it checks the result with a real `unattended-upgrade` dry run.

---

## Contents

1. [Architecture](#architecture)
2. [Supported environments and prerequisites](#supported-environments-and-prerequisites)
3. [Usage](#usage)
4. [First run and reruns](#first-run-and-reruns)
5. [Decisions and defaults](#decisions-and-defaults)
6. [Paths](#paths)
7. [Health checks and the summary](#health-checks-and-the-summary)
8. [Module status (`--status-json`)](#module-status---status-json)
9. [Logs, troubleshooting and recovery](#logs-troubleshooting-and-recovery)
10. [Reusing the script on other servers](#reusing-the-script-on-other-servers)
11. [Limitations and testing status](#limitations-and-testing-status)

---

## Architecture

```
apt-daily.timer          (twice a day)  apt update + download upgradeable packages
apt-daily-upgrade.timer  (daily, ~06:00 + up to 60 min random delay)
   └─ unattended-upgrade
        ├─ installs updates from the allowed origins only
        ├─ keeps locally edited config files (confold)
        ├─ removes old kernels and newly unused dependencies
        ├─ needrestart restarts services that still use old libraries
        └─ if /var/run/reboot-required exists → reboot at the chosen time
```

apt reads `/etc/apt/apt.conf.d/` in name order, and a later value overrides an earlier one. List settings such as `Allowed-Origins` are different: each file adds to the list. The script writes its settings to `52unattended-upgrades-local`, which loads after `10periodic`, `20auto-upgrades` and `50unattended-upgrades`. So it can add origins and override single values without touching those files. A package upgrade can replace them, but it never touches this one.

The managed file starts with the choices the script reads back on reruns:

```
// Managed by setup-auto-updates.sh 1.0.0. Rerun the script to change these settings;
//@ scope=updates
//@ third_party=packages.sury.org nginx.org
//@ reboot=04:00
//@ mail=
```

---

## Supported environments and prerequisites

- **Ubuntu** with systemd. Built for, and checked against, Ubuntu 26.04 with unattended-upgrades 2.12. Debian is refused: its security and updates suites have different names, so the origin lines would match nothing.
- **Bash 4.4 or newer** to run the script. `--self-test` also runs on Bash 3.2, such as the macOS default.
- **Root**, through sudo, for setup. `--status-json`, `--self-test` and `--help` need no root.
- **An interactive terminal and gum** for the prompts. The script installs gum on the first interactive run (shared UI in `lib/ui.sh`). A `--non-interactive` run never prompts, so it needs neither a terminal nor gum.
- **Network access** to the package mirrors. The dry run at the end contacts them.
- If `unattended-upgrades` is not installed, the script offers to install it.
- `needrestart` is optional. Without it, services keep running old library code until they are restarted.

---

## Usage

```bash
sudo ./setup-auto-updates.sh             # interactive setup; reruns keep current answers on Enter
sudo ./setup-auto-updates.sh --dry-run   # ask everything, show the diff, change nothing
./setup-auto-updates.sh --status-json    # module status as JSON; read-only, no root needed
./setup-auto-updates.sh --self-test      # check internal helpers (no root, nothing installed)
./setup-auto-updates.sh --help
```

| Option | What it does |
|---|---|
| `--dry-run` | Asks everything and shows the diff, but writes nothing. It takes no lock and writes no log. Preflight problems (not root, not Ubuntu, no systemd, old Bash) become warnings, so it also runs on a laptop. |
| `--status-json` | Prints one JSON document (see [Module status](#module-status---status-json)) and exits 0. Never installs, writes, locks or runs `unattended-upgrade`. |
| `--self-test` | Checks the internal helpers. Also works on Bash 3.2 (the macOS default). |
| `--non-interactive` | Never prompts. A question with no pre-supplied answer exits 64, unless `--accept-defaults` is also given. |
| `--accept-defaults` | Takes each question's default (the previous answer on a rerun) when it has no pre-supplied answer. Only applies when the run is non-interactive: `--non-interactive`, or no terminal. |
| `--root DIR` | Management root for the log and lock (default `$SOFTSYNK_ROOT`, else `/opt/softsynk-server-management`). Must be an absolute path. |

An unknown option exits 64. The same switches can come from the environment (`SOFTSYNK_NONINTERACTIVE=1`, `SOFTSYNK_ACCEPT_DEFAULTS=1`, `SOFTSYNK_DRY_RUN=1`, `SOFTSYNK_ROOT`), which is how Synk Server Manager passes them.

**Pre-supplied answers.** Two free-text questions can be answered ahead of time in the environment: `SOFTSYNK_ANSWER_REBOOT=03:30` (the reboot time; asked only when automatic reboot is chosen) and `SOFTSYNK_ANSWER_MAIL=ops@example.com` (`-` for none). The choice and yes/no questions have no stable names, so use `--accept-defaults` for them. Confirmations such as "Write these settings?" follow their default, which is No when the script would replace a file it did not write or drop a saved choice.

On a server, let Synk Server Manager run it:

```bash
sudo softsynk-server install auto-updates
```

To run the script directly from a checkout or release, keep the repository layout: it sources `../../lib/ui.sh` (the shared prompts), so the `auto-updates` folder alone is not enough.

```bash
sudo ./deployment/auto-updates/setup-auto-updates.sh
```

---

## First run and reruns

The script follows the same steps on every run:

1. **Current state.** Shows the OS and timezone, the unattended-upgrades version, both timers with their next run, the allowed origins and patterns now in effect, the reboot setting, the needrestart mode, whether a reboot is pending, any third-party repos it found and the managed file.
2. **Install.** Runs only if unattended-upgrades is missing, and asks first.
3. **Settings.** Four questions. On reruns, the value in brackets is your previous answer.
4. **Managed config.** Builds the file and compares it with the one on disk.
   - If nothing differs, it reports "Already up to date" and counts the step as unchanged.
   - Otherwise it shows the diff, checks it with `apt-config`, asks, backs up the current file and writes the new one atomically.
5. **Daily timers.** Enables and starts `apt-daily.timer` and `apt-daily-upgrade.timer` if either is off. It asks first.
6. **needrestart.** If the mode is not `a` (automatic), it offers to set it in its own file under `conf.d`.
7. **Health checks** and the **summary**.

Reruns are safe. Pressing Enter at every question leaves the system unchanged, and the summary reports `0 applied`. There are two exceptions, and both are warned about:
- A saved repo no longer appears in `apt-cache policy`.
- The saved email address can't be used because no mail command is installed.

In both cases the saved value would be dropped, so "Write these settings?" defaults to No.

If `52unattended-upgrades-local` already exists but this script didn't write it, the script warns, shows the diff, and also defaults to No before replacing it.

**When to run it:** avoid 06:00–07:00 server time. That is when the daily upgrade runs. The script's dry run holds the unattended-upgrades lock, so if they overlap, that day's real run exits and its updates wait until the next day.

---

## Decisions and defaults

| Question | Default | Why |
|---|---|---|
| Which updates | Security + stable updates | Stable updates, `-updates`, are bug fixes that went through the same review as security updates. Security-only leaves known bugs unfixed. |
| Third-party repos | PHP (`packages.sury.org`) and `nginx.org`: yes. Anything else: no | PHP and nginx face the internet, so their security fixes matter most. Sury packages are versioned (`php8.5-*`), so updates stay within the same minor version. One exception: unversioned metapackages (`php`, `php-cli`, `php-fpm`) follow sury's newest PHP. If `/usr/bin/php` is in automatic mode, the script warns and prints the `update-alternatives --set` command that pins it. `packages.redis.io` can move to a new major version, and Node is only used for builds, so update those by hand. Each answer covers every repo on that host: sury's nginx repo, or all Launchpad PPAs (`ppa.launchpadcontent.net`). |
| Automatic reboot | Yes, at 04:00 server time, only when required | Kernel and libc fixes only take effect after a reboot. Without one, the server keeps running the vulnerable kernel. A reboot means about 1–2 minutes of downtime, and all services are set to start at boot. |
| Failure email | None | Needs working outgoing mail. Forge's sendmail often isn't set up to relay. Reports go only when something fails (`only-on-error`). |

Other settings, always on:

- `Dpkg::Options --force-confdef --force-confold`. When a package ships a new default config and you have edited yours (`nginx.conf`, `php.ini`), apt keeps yours. Without this, unattended-upgrades skips that package permanently. **This also applies to manual `apt upgrade` runs:** they keep your config without asking.
- `Remove-Unused-Kernel-Packages` and `Remove-New-Unused-Dependencies`. These stop old kernels from filling the disk. The broader `Remove-Unused-Dependencies` is deliberately not enabled, because it could remove packages that were marked as automatically installed.
- `Automatic-Reboot-WithUsers "true"`. A forgotten SSH or tmux session must not block kernel updates forever.
- Package lists are refreshed and updates installed daily, and the download cache is cleaned weekly.

Not changed: the time of day of the upgrade run. The distro timer runs it at 06:00 plus a random delay of up to 60 minutes. The reboot happens at the next reboot time after that, so in the worst case about 22 hours later.

---

## Paths

| Path | What |
|---|---|
| `/etc/apt/apt.conf.d/52unattended-upgrades-local` | The managed config. Rerun the script instead of editing it. |
| `/etc/needrestart/conf.d/50-setup-auto-updates.conf` | Written only if needrestart was not already in mode `a`. |
| `/var/backups/setup-auto-updates/NAME.bak-STAMP` | Backups. They are kept here, not next to the file, because apt would load a `.bak-…` copy inside `apt.conf.d` as live config. |
| `<root>/log/auto-updates/setup-auto-updates.log` | This script's session log. `<root>` is `--root`, `$SOFTSYNK_ROOT` or `/opt/softsynk-server-management`. Not written in a dry run. |
| `/var/log/unattended-upgrades/` | What the daily runs installed (`unattended-upgrades.log`, `unattended-upgrades-dpkg.log`). Reading it needs sudo. |
| `<root>/lock/auto-updates.lock` | Stops two runs from overlapping. Not taken in a dry run. |

---

## Health checks and the summary

After the changes, the script checks the following. In `--dry-run` mode it skips these checks, because nothing was written. (`--status-json` is a separate, lighter read-only snapshot, described below.)

- `APT::Periodic::Unattended-Upgrade` is `1` in the effective apt config.
- The effective reboot setting matches your choice. This catches another `apt.conf.d` file that sorts later and overrides it.
- Both timers are active.
- Daily upgrades are not turned off by `APT::Periodic::Enable "0"`, and neither timer's service is masked.
- `unattended-upgrade --dry-run --debug` succeeds. It runs with `LC_ALL=C` so its output can be parsed. It installs nothing new, but if an earlier dpkg run was interrupted, unattended-upgrades finishes it first (`dpkg --configure -a`), even in dry-run mode. The script then lists what would be upgraded now. It also confirms that each chosen third-party repo is in the allowed list and still exists in apt's sources.
  - If apt is already busy (a scheduled run or a Forge deploy), the dry run is skipped. Only apt's or unattended-upgrades' own lock messages count as busy, so a package whose name contains "lock" doesn't hide a real failure. The run first waits for apt's lock as long as `DPkg::Lock::Timeout` allows, if it is set. The script itself times out after 15 minutes, and kills the dry run 60 seconds later if it is still running. Ctrl-C works at any point. and the result shows `PASSED (dry run skipped)`. Rerun the script later to test.
  - If the dry run fails right after this run wrote the config, the script offers to undo that change. The default is No, because network or mirror trouble also makes the dry run fail.
- If automatic reboot is on: nginx, PostgreSQL, PHP-FPM, Redis (including any `redis-server@NAME` instance) and supervisor are all enabled at boot. Any that are not enabled are reported as a warning.

Example summary:

```
Auto-Updates Summary
────────────────────────────────
OS:              Ubuntu 26.04 LTS
Updates:         Security + stable updates
Third-party:     packages.sury.org nginx.org
Next run:        Thu 2026-11-05 06:21:07 UTC
Last run:        Wed 2026-11-04 06:49:03 UTC
Reboot:          At 04:00 when required
Restarts:        needrestart mode a
Email:           none
Reboot needed:   no

Health Checks:   PASSED
Changes:         1 applied, 3 unchanged
Result:          SUCCESS
────────────────────────────────
```

`Result: FAILED` means a health check failed or a step failed. The script then exits 1. Other exit codes: 64 for an unknown option or a question that has no answer in a non-interactive run, 130 when you cancel. In `--dry-run` mode, a failed step shows `DRY RUN FAILED` and also exits non-zero. "Last run" comes from the `apt-daily-upgrade.timer` timer, so the script's own dry run doesn't count as a run.

---

## Module status (`--status-json`)

`./setup-auto-updates.sh --status-json` prints one JSON line in the Synk Server Manager status format (schema 1) and exits 0 whatever it finds. It reads only: the managed file, `dpkg-query`, `apt-config dump` (the effective config), `systemctl is-active/is-enabled/show` for the two timers, the needrestart config and `/var/run/reboot-required`. It takes no lock, writes nothing and never runs `unattended-upgrade` or apt, so it is safe at any time, including during the 06:00 upgrade window and without root.

| Field | Meaning |
|---|---|
| `install_state` | `installed` (the managed file exists), `adopted` (unattended-upgrades is installed but this script has not written its file), `not_installed`, or `unknown` |
| `health` | `healthy`; `degraded` (a reboot is pending and automatic reboot is off, or an unmanaged file is in the way); `unhealthy` (daily upgrades off in the effective config, the saved reboot setting overridden by a later file, a timer inactive or masked, unattended-upgrades missing); `unknown` (systemd or `apt-config` gave no answer); `unsupported` (not Ubuntu) |
| `details` | `managed`, `unattended_upgrades` (version), `auto_upgrade`, `next_run`, `last_run`, `needrestart_mode`, `reboot_required`, and, when the managed file exists, `scope`, `third_party` and `reboot` |
| `warnings`, `actions` | What was found, and what to run about it |

---

## Logs, troubleshooting and recovery

**See what was installed:**

```bash
sudo tail -n 50 /var/log/unattended-upgrades/unattended-upgrades.log
```

```bash
zgrep -h " upgrade " /var/log/dpkg.log* | tail -n 30
```

**Test without installing anything:**

```bash
sudo unattended-upgrade --dry-run --debug
```

**Is a reboot pending, and why?**

```bash
cat /var/run/reboot-required.pkgs
```

**Cancel tonight's scheduled reboot:**

```bash
sudo shutdown -c
```

**A package is never upgraded.** Look for "kept back" or a conffile prompt in the debug output. Check that its repo is allowed: the dry run's "Allowed origins are:" line has to match the repo's `apt-cache policy` entry.

**Undo:**

1. Restore the backup from `/var/backups/setup-auto-updates/`, or delete the managed file to fall back to the distro's security-only defaults:

   ```bash
   sudo rm /etc/apt/apt.conf.d/52unattended-upgrades-local
   ```

2. Then check the effective config:

   ```bash
   apt-config dump | grep -E 'Unattended-Upgrade|Periodic'
   ```

**Hold back one package**, for example during a migration:

```bash
sudo apt-mark hold PACKAGE
```

Release it later with `sudo apt-mark unhold PACKAGE`. unattended-upgrades respects holds.

---

## Reusing the script on other servers

The script has no host-specific values. It finds third-party repos on each server from `apt-cache policy` and asks about each one. The only defaults it bakes in are yes for `packages.sury.org` and `nginx.org` (see `recommended_site`), and 04:00 for the reboot time. To prepare a new server, copy the folder and run the script. To apply the same answers non-interactively, copy the managed file instead and rerun the script once to verify.

---

## Limitations and testing status

- Independent code review: three rounds, each by a fresh reviewer, on 2026-11-04.
  - Round 1: no blockers, 2 major and 11 minor findings.
  - Round 2: checked the round 1 fixes against the server's real unattended-upgrades source and `apt-cache policy` output. No blockers or major findings, 9 minor findings.
  - Round 3: reviewed every change against the full script and tested the changed functions on the server, as a non-root user. No material findings. The one minor item is fixed: a hard kill 60 seconds after the dry run's 15-minute timeout.
  - All findings are fixed. Earlier reviews' findings are also fixed, except one that no longer applies: the lock file used to be created in `--dry-run` mode. A dry run now takes no lock.
- Verified on the server's source:
  - unattended-upgrades never reboots or sends mail in a dry run.
  - The dry run doesn't stop that day's scheduled run from happening.
  - The three lock messages the script looks for exist in the installed version.
- Checked locally: `bash -n`, ShellCheck (no findings), `tests/deployment/test_status.py` (`--status-json` against stubbed `systemctl`, `dpkg-query` and `apt-config`: not installed, adopted, healthy, daily upgrades off, overridden reboot setting, pending reboot, inactive timer, silent systemd, unreadable apt config; plus a check that Synk Server Manager accepts the output) and `--self-test`, which also passes on Bash 5.3. The self-test covers time and email validation, repo detection from real `apt-cache policy` output (including a repo without a release line, and host names that would be unsafe in the config), needrestart mode parsing, the read-back of choices and the generated file for both the full and the minimal choice sets.
- Checked on the target server (read-only, as a non-root user): apt accepts the generated file (`apt-config -c`), and the merged values come out as expected. The installed unattended-upgrades supports `site=` patterns and logs them in the allowed-origins line.
- The interactive run as root is the first end-to-end test. Use `--dry-run` first if you want to see the diff before anything is written.
- The script doesn't change the upgrade time, set package blacklists, or configure outgoing mail. Change these by hand if you need them.
- Service restarts are not new: needrestart is already in mode `a`, so library updates already restart php-fpm, PostgreSQL, Redis and supervisor (stopping running queue jobs). Including stable updates makes this more frequent, during the 06:00–07:00 window.
