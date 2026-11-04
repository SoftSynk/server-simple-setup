# Writing a Synk Server Manager module

A module is a folder with a manifest and the scripts the manifest points to. The engine never imports module code; it only validates the manifest and runs the declared commands. Read [architecture.md](architecture.md) first for the overall picture.

Interface version: **1**. The engine refuses any other `interface` value.

## 1. Register the module

Add it to `modules.toml` at the repository root. The order here is the display order and the tie-break when dependencies do not decide.

```toml
[[modules]]
id = "backup"
manifest = "deployment/backup/module.toml"
```

`id` must match `^[a-z][a-z0-9-]{1,31}$`, be unique, and equal the `id` inside the manifest. The manifest path is relative to the repository root and must stay inside it.

## 2. Write the manifest

```toml
schema = 1
id = "backup"
name = "Database backups"
description = "PostgreSQL dumps to local disk and S3 with restic, every 6 hours"
version = "1.0.0"
interface = 1
requires = []          # hard dependencies: module ids
after = []             # ordering hint only: module ids
effects = ["installs restic", "adds systemd timer softsynk-backup.timer"]

[platform]
os = ["ubuntu"]
min_version = "24.04"

[operations]           # argv arrays; the first item is a program path inside the module folder
install   = ["setup-backup.sh"]
configure = ["setup-backup.sh", "--configure"]
status    = ["synk-backup", "--status-json"]

[commands]             # command names the module ships; validated only (see the table)
synk-backup = "synk-backup"
```

### Fields

| Field | Required | Rule |
|---|---|---|
| `schema` | yes | Integer `1`. |
| `id` | yes | Same as the registry id. |
| `name`, `description` | yes | Non-empty strings shown in menus. |
| `version` | yes | The module's own version; letters, digits and `._+-`. |
| `interface` | yes | Integer `1`. |
| `requires` | no | Module ids that must be installed first. Unknown ids are an error. |
| `after` | no | Module ids to order after when both are selected. Does not block. |
| `effects` | no | Plain sentences shown to the operator before install. Be honest: packages, units, files outside the root. |
| `[platform]` | no | `os` (list, matched against `ID` in `/etc/os-release`) and `min_version` (for example `"24.04"`). On a mismatch the module is listed as unsupported and skipped; it is not an error. |
| `[operations]` | yes | See below. |
| `[commands]` | no | `name = "path"` entries. Same path rules as operations. Validated, but this engine version does not act on them: `bootstrap.sh` links `synk-backup` by name when `deployment/backup/synk-backup` exists. |

Unknown top-level keys, unknown operations and unknown `[platform]` keys are errors, so a typo cannot silently disable something. The plan's earlier `privileges` and `platform.arch` fields were dropped and are rejected.

### Operations

| Operation | Required | Purpose |
|---|---|---|
| `install` | yes | Install the module. Safe to rerun. |
| `status` | yes | Print the status JSON (section 4). Must be read-only and fast. |
| `configure` | no | Re-run configuration. |
| `validate`, `update` | no | Reserved. They are accepted and checked but this engine version does not run them. |

Each value is an **array of strings**, never a shell string. `install = "run.sh install"` is rejected. The program (first item) is a path relative to the manifest's folder and must:

- be a non-empty relative path with no `..` component;
- resolve (symlinks followed) to a file inside the release directory;
- exist and be executable.

Arguments after the program are passed through unchanged. Nothing is expanded by a shell.

## 3. How operations run

- The working directory is the module's folder.
- `install` and `configure` run with the terminal attached, so the module can prompt. They run as the same user as the engine (normally root).
- `status` runs with stdin from `/dev/null` and its output captured, under a timeout (20 seconds). The engine kills the whole process group on timeout.
- The environment is the engine's own environment plus:

| Variable | Value |
|---|---|
| `SOFTSYNK_ROOT` | Management root. |
| `SOFTSYNK_MODULE` | This module's id. |
| `SOFTSYNK_ETC_DIR`, `SOFTSYNK_SECRETS_DIR`, `SOFTSYNK_STATE_DIR`, `SOFTSYNK_LOG_DIR`, `SOFTSYNK_TMP_DIR` | `<root>/{etc,secrets,state,log,tmp}/<module>`. The engine does not create them; the module does (secrets directories 0700, files 0600). |
| `SOFTSYNK_INTERACTIVE` | `1` if the module may prompt, else `0`. |
| `SOFTSYNK_NONINTERACTIVE` | The opposite, `1` or `0`. |
| `SOFTSYNK_ACCEPT_DEFAULTS` | `1` if the operator passed `--accept-defaults`. |
| `SOFTSYNK_DRY_RUN` | `1` if the operator passed `--dry-run`. The module decides what a dry run means; it must change nothing. |
| `SOFTSYNK_RUN_ID` | One id per engine invocation, for correlating logs. |

When `SOFTSYNK_INTERACTIVE=0` the module must not prompt. If it lacks a required answer it exits non-zero (conventionally 64) instead of waiting or guessing.

### Exit codes of install and configure

| Exit code | Engine treats it as |
|---|---|
| 0 | Success |
| 130 (or killed by SIGINT) | Cancelled; dependents are blocked, independent modules continue |
| anything else | Failed; dependents are blocked, independent modules continue |

Ctrl-C reaches the module, not the engine, while an operation runs. Handle it and exit 130.

The exit code of `status` is ignored: the JSON is what counts, so a module may exit non-zero after printing valid JSON.

## 4. Status JSON

`status` prints exactly one JSON object on stdout:

```json
{
  "schema": 1,
  "module": "backup",
  "version": "1.0.0",
  "observed_at": "2026-11-04T12:00:00Z",
  "install_state": "installed",
  "health": "healthy",
  "summary": "last backup 2h ago, restore test passed 3 days ago",
  "details": {},
  "warnings": [],
  "actions": []
}
```

| Field | Rule |
|---|---|
| `schema` | Required, integer `1`. |
| `module` | Required, must equal the module id. |
| `install_state` | Required: `installed`, `adopted` (the service exists but the module's own managed files are absent), `not_installed`, or `unknown`. |
| `health` | Required: `healthy`, `degraded`, `unhealthy`, `unknown`, `stale`, or `unsupported`. |
| `summary` | Optional string, one line. |
| `version`, `observed_at` | Optional strings. |
| `details` | Optional object (free-form, machine-readable facts). |
| `warnings` | Optional array of strings. A `degraded` module should explain why here. |
| `actions` | Optional array: suggested next steps for the operator. |

Anything else (invalid JSON, a missing or wrong `module`, another schema number, an unknown `install_state` or `health`, wrong types, no output, a timeout) is recorded by the engine as `health: unknown` with a warning. Keep `status` read-only and quick: no network calls without a short timeout, no long-running checks (read the result of the last run instead), and never an operation that can change the system.

Health maps to the exit code of `--status`: healthy 0, degraded 1, unhealthy 2, unknown or stale 3. A module with `install_state` `not_installed`, or with `health` `unsupported`, is excluded from the overall result.

Report `not_installed` plainly when nothing is installed. Do not report `unhealthy` for a module the operator has not installed.

## 5. Dependencies

- `requires` is a hard dependency. `install` runs required modules first. If a required module is not installed and the operator did not select it, the dependent is shown as BLOCKED with that reason; nothing is added silently. If a required module fails or is cancelled during the run, its dependents are blocked and not run.
- `after` only influences order.
- A cycle in `requires`/`after` makes every module on the cycle invalid, with a message such as `dependency cycle: a -> b -> a`.
- An invalid manifest never takes the others down. The module is reported, and modules that require it are blocked.

## 6. Checklist for a new module

1. Manifest validates: `./softsynk-server validate`.
2. `status` prints valid JSON in every state, including `not_installed`, and is read-only.
3. `install` is safe to rerun and honors `SOFTSYNK_INTERACTIVE=0`, `SOFTSYNK_DRY_RUN=1` and Ctrl-C (exit 130).
4. Everything the module changes outside the root is listed in `effects` and in its README. Rollback of the framework will not undo it.
5. Secrets are never printed, logged, or put in `status` output.
6. Add a row for it in `modules.toml`.

For test examples, see `tests/fixtures/modules/` (a shared `run.sh` and one manifest per case) and `tests/engine/`. Run the suite with `python3 -m unittest discover -s tests/engine`.
