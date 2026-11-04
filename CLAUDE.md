# softsynk: agent and contributor notes

## Server work
- Never run commands against a live server from an agent session. Hand the operator exact commands with full paths (no `~`, no `sudo`; they run as root).

## Testing scope
- Test only our own code. External packages and tools (restic, gum, pg_dump, apt, systemd, Redis) are out of test scope.
- For externals: one short integration smoke test that proves our wiring works, plus quick probes of the exact flags and exit codes we rely on. No deep suites, no long simulations, no full reruns per tool version.
- Decision logic (retention, due tiers, manifest validation) is unit-tested with stubs and an injected clock.
- Budget: the whole `tests/` tree runs in about 2 minutes.

## Scope
- KISS: build only what was asked. No speculative features or extra verification layers.
