# Ops toolkit: deferred work

Left out of 0.2.0 on purpose (see `docs/decisions/0002-ops-toolkit.md`). Each item has the trigger that makes it worth doing; without the trigger, leave it.

## Scope cuts made while building 0.2.0

| Item | Trigger |
|---|---|
| Dashboard drill-down (Enter on a service, disk or alert) and the maintenance menu (`m`); the dashboard has keys `q`, `r`, `t` only | Operators run the same follow-up commands from the dashboard often |
| PHP-FPM pool apply in `synk-tune` (it audits only; Forge may rewrite `www.conf`, a pool reload can cut requests, and sizing from memory use is not verified offline) | Audit findings on pools are acted on by hand repeatedly, and a staging server confirms the sizing |
| Webhook, Slack and Discord notifications | The owner wants a channel other than mail |
| Per-item default in `ui.review` (today every finding defaults to apply) | A flow needs a recommended-but-off or report-only item |
| Forge and Envoyer zero-downtime layouts (`current -> releases/N`, shared `storage`) in `synk-clean` log cleanup and rotation | A managed server uses that layout and its logs fill the disk |
| A lock around the `notify` failure-mail record (two units failing at the same moment can mail one twice) | Duplicate failure mails are seen |

## Deferred features

| Item | Trigger |
|---|---|
| HTML mail; per-severity recipients | Mail volume or audience grows |
| Monitor checks: load average, per-mount thresholds, OOM kills, TLS certificate expiry, HTTP endpoints, time sync, PHP-FPM saturation | An incident of that kind was missed, or the owner asks |
| Scheduled automatic cleanup timer for `synk-clean` | The operator runs the same `synk-clean run` categories repeatedly |
| Docker and snap pruning; old kernel removal | Docker or snap is used on managed servers, or `/boot` runs near full |
| Hardening: non-interactive apply, revert that survives a reboot (oneshot unit in /etc/systemd/system), SSH port change, AllowUsers/AllowGroups, fail2ban install and config, nftables management, UDP audit | Fleet provisioning, or the owner asks |
| Optimizer apply for Nginx, Apache, MySQL, PostgreSQL, Meilisearch and php.ini; OPcache hit rate through FPM; Forge drift detection | The owner asks after the report-only output has proved useful |
| System-info TTL cache | Detection is measured above 0.5 s |
| Dashboard: per-process view, multi-server view | The owner asks |
| Module uninstall (disable timers, remove managed files) | The first module is retired |
| Remove command links for commands a release no longer ships | The first command is removed |
| Move `synk-backup` helpers onto `lib/softsynk`; share the Bash helpers of the redis and auto-updates setups | The next substantive change to those files |
