# softsynk-server

Server-management scripts for Ubuntu with systemd: PostgreSQL backups, Redis setup and automatic security updates, behind one command with a [gum](https://github.com/charmbracelet/gum) terminal UI.

| Module | What it does | Docs |
|---|---|---|
| `backup` | `synk-backup`: encrypted [restic](https://restic.net) backups of every PostgreSQL database (plus roles), a local repository and an optional S3-compatible copy (Cloudflare R2, Backblaze B2, Hetzner Object Storage or any S3 endpoint), tiered retention, integrity checks, restore tests and restores. Runs from a systemd timer every 6 hours. | [deployment/backup](deployment/backup/README.md) |
| `redis` | Installs and tunes Redis as a single instance or as separate cache and queue instances. | [deployment/redis-setup](deployment/redis-setup/README.md) |
| `auto-updates` | Configures unattended-upgrades, third-party repos, automatic reboots and failure mail. | [deployment/auto-updates](deployment/auto-updates/README.md) |

`softsynk-server` installs, configures, updates, rolls back and reports the status of every module. Everything it manages lives under `/opt/softsynk-server-management`, with versioned releases and a `current` symlink for instant rollback.

## Requirements

- Ubuntu with systemd (the backup module needs Ubuntu 26.04 or newer), run as root
- Python 3.11 or newer (standard library only), Bash 4.4 or newer
- `gum` and `restic` from the Ubuntu archive (`bootstrap.sh --install` installs gum)

## Install

Build a release tarball on your machine:

```bash
git archive --format=tar.gz --prefix="softsynk-server-$(cat VERSION)/" -o "softsynk-server-$(cat VERSION).tar.gz" HEAD
shasum -a 256 "softsynk-server-$(cat VERSION).tar.gz"
```

Copy it to the server, then, as root:

```bash
tar -xzf softsynk-server-0.1.0.tar.gz
bash softsynk-server-0.1.0/bootstrap.sh --from softsynk-server-0.1.0.tar.gz --sha256 <sha256> --install
softsynk-server --status
```

Run `softsynk-server` with no arguments for the interactive menu, or `--help` for every command.

## Documentation

- [Architecture](docs/architecture.md): layout, commands, exit codes, update and rollback, trust model
- [Writing a module](docs/module-development.md)
- [Design decision: backup stack and engine](docs/decisions/0001-backup-and-server-management.md)

## Development

Tests use only the Python standard library; the backup smoke test also needs local PostgreSQL client and server binaries and restic.

```bash
for d in tests/engine tests/backup tests/deployment; do python3 -m unittest discover -s "$d" || break; done
bash tests/ui/test_ui.sh
```

## Security

Report vulnerabilities privately through GitHub's "Report a vulnerability" (security advisories) on this repository, not in a public issue.

## License

[MIT](LICENSE)
