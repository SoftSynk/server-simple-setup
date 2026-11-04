# Synk Server Manager architecture

Synk Server Manager (`softsynk-server` command) installs, configures, updates and reports on a small set of server modules (notify, backup, Redis, automatic updates, housekeeping, optimizer, monitor, hardening). The engine is one Python 3 file, `softsynk-server`, that uses only the standard library. It contains no per-module code. A module is a folder with a data-only manifest (`module.toml`) and its own scripts; the engine reads the manifest and runs the module's declared commands.

Companion document: [module-development.md](module-development.md) (how to write a module).

## Parts

| Part | File | Job |
|---|---|---|
| Engine | `softsynk-server` | Validate manifests, order modules by dependency, run operations, aggregate status, update and roll back the engine's own release. |
| Registry | `modules.toml` | Ordered list of module ids and manifest paths. Nothing is discovered by scanning folders. |
| Manifests | `deployment/<module>/module.toml` | Data only: identity, dependencies, platform, and the command (argv array) for each operation. |
| Bootstrap | `bootstrap.sh` | First install and every update: stage, verify, validate, activate a release; create the root tree and command links. |
| Version | `VERSION` | The only source of the framework version. |
| Shared library | `lib/softsynk/` | Code shared by the Python modules (below). The engine does not import it. |

## Server layout

Default root: `/opt/softsynk-server-management`. Override with `--root DIR` or `SOFTSYNK_ROOT`.

```
releases/<version>/            immutable code, made read-only after staging
current  -> releases/<version> relative symlink, switched atomically
previous -> releases/<version> kept for rollback
etc/<module>/                  configuration                    (0755 dirs)
secrets/<module>/              credentials                      (secrets/ is 0700)
state/<module>/                module state and install markers
state/framework/history.jsonl  one JSON line per install/configure/update/rollback
log/<module>/                  logs
data/<module>/                 module data (backup repository, monitor history, change backups)
tmp/<module>/                  scratch space                    (tmp/ is 0700)
lock/framework.lock            global lock for mutating commands
lock/<module>.lock             one run at a time per module
```

Command links, created by `bootstrap.sh` only when missing or already pointing into `current`: `/usr/local/bin/softsynk-server` and every command a valid manifest declares under `[commands]` (read with `softsynk-server commands`), for example `synk-backup`, `synk-notify`, `synk-monitor`. Set `SOFTSYNK_BIN_DIR` to put them elsewhere. A link that points somewhere else is left alone with a warning.

Some things stay where the operating system expects them and are outside the root: systemd units, Redis config and data under `/etc/redis` and `/var/lib/redis`, apt configuration, PostgreSQL data, and the two command links.

## Commands

```
softsynk-server                              interactive menu (terminal only; gum if present, else numbered prompts)
softsynk-server install [module...]          install in dependency order; menu when no names are given
softsynk-server configure <module>           run the module's configure operation
softsynk-server update --from DIR|TARBALL [--sha256 HEX]
softsynk-server --rollback                   point current back at the previous release
softsynk-server --status [--json]            module health (read-only)
softsynk-server --list                       modules, versions, install state, blockers (read-only)
softsynk-server validate                     check registry and manifests (read-only; also run by bootstrap)
softsynk-server commands                     commands the modules ship, name<TAB>path (read-only; bootstrap links them)
softsynk-server --version | --help
```

Global flags: `--root DIR`, `--non-interactive`, `--accept-defaults`, `--dry-run` (install/configure only; it is passed to the module as `SOFTSYNK_DRY_RUN=1`, and the module decides what that means).

Read-only commands (`--status`, `--list`, `validate`, `commands`, `--version`, `--help`) never install anything, never take the lock, and never write a file or create the root. The tests hash the root and the release before and after to prove it.

### Interactive and non-interactive

Interactive means a terminal on stdin and stdout, without `--non-interactive` or `SOFTSYNK_NONINTERACTIVE=1`. Non-interactive runs never prompt:

- `install` and `configure` need module names, otherwise exit 64.
- Every confirmation (the install plan, an update, a rollback) needs `--accept-defaults` (or `SOFTSYNK_ACCEPT_DEFAULTS=1`), otherwise exit 64. Nothing is changed.
- A bare `softsynk-server` without a terminal prints help to stderr and exits 64.

Prompts use `gum choose`, `gum confirm` and `gum input` with only their basic flags when `gum` is on `PATH`, and a plain numbered prompt otherwise. Ctrl-C or Esc in any prompt cancels (exit 130).

## Shared library and module state

The Python modules (notify, monitor, housekeeping, optimizer, hardening) share `lib/softsynk/`, standard library only: `core` (paths, validated JSON config, atomic writes, locks, logs, status documents), `sysinfo` (readers for `/proc` and systemd), `ui` (prompts with the same non-interactive rules as `lib/ui.sh`), `change` (back up, write, validate, reload, verify and restore for system config files) and `notify` (the one mail sender; backup uses it too). A script adds `lib/` to `sys.path` from its own real path and never writes bytecode, because releases are read-only.

Each module keeps its files in its own folder under the root, one writer per file:

| Path | Holds |
|---|---|
| `etc/<module>/config.json` | Settings, no secrets. Unknown keys and wrong types are refused (exit 64). |
| `secrets/<module>/` | Credentials, files 0600 (for example `notify/smtp.env`). |
| `state/<module>/` | Current state: last run, `monitor/latest.json`, `monitor/alerts.json`. |
| `data/<module>/` | Records that grow: `monitor/history/YYYY-MM-DD.jsonl` (7 days, capped), `<module>/backups/<stamp>/` copies of system files a tool changed. |
| `log/<module>/YYYY-MM.jsonl` | Logs, secrets redacted, pruned after 90 days. |

If the monitor cannot write `state/monitor` (disk full, read-only), it writes `latest.json` and `alerts.json` to `/run/softsynk-monitor` instead and readers use the newer copy, so a full disk does not stop alerts.

Tools that change the system (`synk-harden`, `synk-tune`, the setup of `synk-clean`) follow the same order, written out in each script: Inspect (read-only) → Validate (the current config passes its own check, else refuse) → Recommend → Explain (benefit and risk) → Customize → Preview (diff) → Confirm → Apply (`change.apply`: back up, write, validate, reload) → Verify → Report (summary, log, backup stamp, undo command). Any failed check restores every file and reloads the old config. A run with nothing to change says so and touches nothing.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Healthy / succeeded |
| 1 | Degraded |
| 2 | Unhealthy, or a command failed (also: lock held, update refused) |
| 3 | Incomplete: health unknown or timed out, or `install` finished with modules blocked |
| 64 | Invalid request or missing required input |
| 130 | Cancelled |

For `--status` the exit code is the worst module, ranked healthy < degraded < unknown/stale (3) < unhealthy (2). A known failure outranks "cannot tell". Modules that are not installed, and modules whose platform is unsupported, do not count.

## Status

`--status` runs each module's `status` operation one after another, each under a timeout (20 seconds; `SOFTSYNK_STATUS_TIMEOUT` overrides it and exists for tests). The child runs in its own process group and the whole group is killed on timeout. A module that times out, cannot start, prints invalid JSON, reports for another module, or uses an unknown schema, health value or install state becomes `health: unknown` with a warning explaining why. One broken module never hides the others.

`install_state` (`installed`, `adopted`, `not_installed`, `unknown`) is separate from `health`. "Adopted" means the service exists but the module's own managed files are absent, for example Redis installed by hand.

`--status --json` prints one document:

```json
{
  "schema": 1,
  "framework": {"version": "0.2.0", "interface": 1},
  "observed_at": "2026-11-04T12:00:00Z",
  "overall": {"health": "degraded", "exit_code": 1},
  "modules": [ {"module": "...", "name": "...", "version": "...", "observed_at": "...",
                "install_state": "...", "health": "...", "summary": "...",
                "details": {}, "warnings": [], "actions": []} ]
}
```

The process exit code equals `overall.exit_code`. The schema number changes only on breaking changes.

## Install

1. Take the global lock (below).
2. For named modules, read the status of any required module that was not named.
3. Show the plan in dependency order; a module that cannot run is shown as BLOCKED with the reason (invalid manifest, unsupported platform, a requirement that is not installed and was not selected, a requirement that is itself blocked).
4. One confirmation for the whole plan.
5. Run each module's `install` command with the terminal attached.
6. Print a summary and append to `history.jsonl`.

A failed or cancelled module blocks its dependents; independent modules continue. Required modules are never added silently. Exit: 2 if any module failed, else 130 if any was cancelled, else 3 if any was blocked, else 0. Skipping a module never touches it.

## Concurrency

Mutating commands (`install`, `configure`, `update`, `--rollback`, and `bootstrap.sh`) take a non-blocking `flock(2)` on `lock/framework.lock`. A second one is refused at once with exit 2 and a message; it never waits. The lock is released when the process exits, including after a crash. `update` runs `bootstrap.sh` as a child and tells it the lock is already held (`SOFTSYNK_LOCK_HELD=1`). Read-only commands do not lock.

State files are written atomically (temporary file, fsync, rename). The `current` and `previous` links are switched by creating a new link and renaming it over the old one.

## Update and rollback

`update` reuses the exact path of the first install, so there is one staging implementation:

1. The source is a directory, or a tarball with its expected sha256 (`--sha256`, required for tarballs, refused for directories).
2. `bootstrap.sh` verifies the tarball's sha256 before extracting a single byte, then stages into `releases/.staging-<pid>`.
3. The staged release's own engine runs `validate` against it. Any invalid manifest stops the update before anything is switched.
4. The directory is renamed to `releases/<version>` and made read-only. If that version already exists, identical contents are accepted (rerun); different contents are refused (bump `VERSION`).
5. `previous` is set to the old release and `current` to the new one, each atomically.

Any failure before step 5 leaves `current` and `previous` untouched and removes the staging directory.

`--rollback` points `current` back at `previous` and `previous` at the release it left, so running it again returns to the newer release. It asks for confirmation first.

**Honest limits of rollback.** Rollback reverts framework code only. It does not undo anything a module changed on the system: configuration under `etc/`, installed packages, systemd units, Redis or apt settings, database contents. A new release may also have run a module operation that a rollback cannot take back. Old releases are not deleted automatically; remove them by hand when you are sure they are not needed. The module-specific `update` operation described in the original plan (offered after the framework switch) is reserved in the manifest format but not invoked by this version.

## Trust model for distribution

The tools verify integrity, not authenticity.

- A directory source is trusted as much as the account that put it there. Use it for `scp -r` of a checked-out repository, then `sudo ./bootstrap.sh`.
- A tarball needs a sha256, and a mismatch is refused with nothing changed. A checksum downloaded from the same place as the tarball only proves the file was not corrupted in transit. It does not prove who published it, because whoever can replace the tarball can replace the checksum.
- To get authenticity, obtain the expected sha256 out of band: from a release page you already trust over a separate channel, or from your own notes made when you built the release. Signing releases (for example with `minisign`) would close the gap; it is not implemented.
- The engine never runs anything from a manifest through a shell. Manifest commands are argv arrays whose program must resolve to an executable inside the release directory; absolute paths, `..` and symlinks leaving the release are rejected. Manifests are data. The module scripts themselves run as root with full privileges, so reading a release before installing it is the real check.

### Remote install (template only)

No repository URL or published release exists yet, so there is no one-line installer. When a release is published, the flow is:

```sh
# 1. fetch the tarball (placeholders: replace with the real location of your release)
curl -fsSLo softsynk-server-<VERSION>.tar.gz "<RELEASE_URL>/softsynk-server-<VERSION>.tar.gz"
# 2. get the expected sha256 from a source you trust, NOT from the same server, and check it
EXPECTED_SHA256="<64-hex-digits-from-a-trusted-channel>"
echo "$EXPECTED_SHA256  softsynk-server-<VERSION>.tar.gz" | sha256sum -c -
# 3. only then unpack bootstrap.sh, read it, and run it; it verifies the tarball again before extracting it
tar -xzf softsynk-server-<VERSION>.tar.gz --strip-components=1 softsynk-server-<VERSION>/bootstrap.sh
sudo ./bootstrap.sh --from softsynk-server-<VERSION>.tar.gz --sha256 "$EXPECTED_SHA256"   # add --install to continue into install
```

`curl ... | sudo bash` is deliberately not offered: it runs unverified code as root.

## Platform

Linux with Python 3.11 or newer (`tomllib`). Modules declare `[platform] os = ["ubuntu"]` and `min_version`; on any other system they are listed as unsupported and skipped, and `bootstrap.sh` warns when the host is not Ubuntu. `bootstrap.sh --install` installs `gum` through apt only on Ubuntu and only as root; elsewhere it warns and menus fall back to plain prompts.

The notify, monitor, housekeeping, hardening and optimizer modules declare `min_version = "24.04"` because Python 3.11 (shipped by Ubuntu 24.04) is their only requirement; the backup module declares 26.04 for its own tooling.

Test hooks: `SOFTSYNK_OS_RELEASE` (path to an os-release file) and `SOFTSYNK_STATUS_TIMEOUT` (seconds).

## Tests

```sh
python3 -m unittest discover -s tests/engine
```

The other directories (`tests/lib`, `notify`, `housekeeping`, `optimizer`, `monitor`, `hardening`, `backup`, `deployment`) run the same way; see [CONTRIBUTING.md](../CONTRIBUTING.md). The engine suite builds throwaway releases from `tests/fixtures/` in temporary directories and never touches a real root. It covers validation of every bad-manifest fixture, dependency order, blocking and cycles, status JSON and exit codes, the timeout, read-only guarantees, non-interactive exit 64, the lock, bootstrap, update, rollback, the wrong-checksum refusal, terminal prompts (through a pty), and `shellcheck`.
