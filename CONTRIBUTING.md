# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Ground rules

- **Keep it simple.** Build what the issue needs, nothing speculative. The engine, the backup runner and the Python modules (shared code in `lib/softsynk`) use only the Python standard library, and the rest is Bash. Please don't add dependencies without discussing them in an issue first.
- **No secrets in arguments.** Credentials come from prompts or `SOFTSYNK_ANSWER_<NAME>` environment variables, never command-line options.
- **Never test against a live server.** Tests use temporary directories and stubs.

## Development setup

You need Python 3.11+, Bash and [ShellCheck](https://www.shellcheck.net). The backup smoke test also needs local PostgreSQL server and client binaries and [restic](https://restic.net), and the UI tests use [gum](https://github.com/charmbracelet/gum) when it is installed. macOS and Linux both work.

```bash
for d in tests/lib tests/notify tests/housekeeping tests/optimizer tests/monitor tests/hardening tests/engine tests/backup tests/deployment; do python3 -m unittest discover -s "$d" || break; done
bash tests/ui/test_ui.sh
bash deployment/redis-setup/setup-redis.sh --self-test
bash deployment/auto-updates/setup-auto-updates.sh --self-test
```

The whole suite takes about 2 minutes.

## Testing scope

- Test our own code. External tools (restic, gum, pg_dump, apt, systemd, Redis) get one short smoke test that proves the wiring, plus checks of the exact flags and exit codes we rely on.
- Decision logic (retention, due tiers, manifest validation) is unit-tested with stubs and an injected clock.
- Shell scripts must pass `shellcheck` with no warnings.

## Adding a module

Follow [docs/module-development.md](docs/module-development.md): register it in `modules.toml`, write a data-only `module.toml`, and implement `--status-json`. Copy the shape of an existing module under `deployment/`.

## Pull requests

1. Open an issue first for anything larger than a bug fix.
2. Add or update tests and docs in the same PR.
3. Describe what changed and how you tested it, including any check you could only do on a real server.

By contributing, you agree that your contribution is dedicated to the public domain under [CC0-1.0](LICENSE), the same as the rest of the project.
