# SSH and firewall hardening (module `hardening`)

Finds weak SSH and firewall settings, explains the benefit and the risk of each fix, and applies the ones you
pick. A wrong SSH or firewall change can lock you out of a server, so every change has three safeguards: a
check that your own login really uses a key, the SSH port allowed before the firewall is turned on, and a
timer that undoes everything unless you confirm from a new SSH session.

Requires: Ubuntu 24.04 or newer, python3, OpenSSH server. `ufw` is optional: without it, the firewall findings
are skipped. Nothing runs on a timer except the one-off revert timer that `apply` arms.

## Commands

Run as root, on the server, from an SSH session that is still connected (not the provider console, and not a
tmux or screen session whose SSH login has closed; see the lockout guard below).

```sh
/usr/local/bin/synk-harden audit               # read-only: what could be hardened, with benefit and risk
/usr/local/bin/synk-harden apply               # pick findings, preview, type the hostname, apply, arm the revert timer
/usr/local/bin/synk-harden apply --dry-run     # pick findings and show the preview only; nothing is changed (needs a person too)
/usr/local/bin/synk-harden confirm             # keep the change: run it from a NEW SSH session
/usr/local/bin/synk-harden revert              # undo a change that waits for confirmation, now
/usr/local/bin/synk-harden --status-json       # what softsynk-server reads; always exits 0
```

`apply` needs a person at the keyboard: it asks you to type the server's hostname and the change must be tested
from a new SSH login. Without a terminal, or with `--non-interactive`, it is refused with exit 64, and there is no
option that skips the question (`--accept-defaults` does not). Use `audit` to look without changing anything.

Through the engine (as root): `softsynk-server install hardening` runs the read-only `audit`; it never applies a change.

Exit codes: 0 ok, 2 something failed or a finding was refused, 64 usage or config error, 130 cancelled.

## What it checks

Settings are read with `sshd -T` (the effective config, including `Include` files) and `ufw status verbose`.

| Id | Fix | Written to |
| --- | --- | --- |
| `ssh_password` | `PasswordAuthentication no` and `KbdInteractiveAuthentication no` | SSH drop-in |
| `ssh_rootlogin` | `PermitRootLogin prohibit-password` when it is `yes` (root keeps its key; `no` can be typed in but is refused while you are root) | SSH drop-in |
| `ssh_emptypw` | `PermitEmptyPasswords no` | SSH drop-in |
| `ssh_maxauth` | `MaxAuthTries 4` when it is above 4 (an edited value cannot go below 3) | SSH drop-in |
| `ssh_x11` | `X11Forwarding no` | SSH drop-in |
| `ssh_grace` | `LoginGraceTime 30` when it is above 30 or unlimited | SSH drop-in |
| `ssh_alive` | `ClientAliveInterval 300` with `ClientAliveCountMax 2` when keep-alive is off or above 300 | SSH drop-in |
| `ufw_enable` | turn ufw on: deny incoming, allow outgoing, allow every SSH port and the listed ports | ufw |

All SSH fixes go into one file, `/etc/ssh/sshd_config.d/10-softsynk.conf`. It sorts before the `50-cloud-init.conf`
that cloud images ship, and sshd uses the first value it sees. The SSH port is never changed. Only values from a
fixed list are ever written, so a typed value cannot add another directive. If that file holds a line this tool
did not write (an `AllowUsers` or `Match` line, say), `apply` refuses and names the line: rewriting the file would
delete it, so move it to another file such as `/etc/ssh/sshd_config.d/20-local.conf` first. If `/etc/ssh/sshd_config`
has no `Include /etc/ssh/sshd_config.d/*.conf` line, sshd never reads the drop-in; the check after the reload then
fails, everything is put back, and the message says to add that line.

`ufw_enable` is offered only when ufw is installed and inactive. It keeps ports 80 and 443 open when something
listens on them; any other public listener is shown as "not kept" and can be added by choosing edit and typing
a list such as `443/tcp, 8080`. An already active firewall is never changed; `audit` only warns when no rule
allows an SSH port, when a database or cache port (3306, 5432, 6379, 6380, 7700, 11211, 9200, 27017) listens on
a public address, and when the SSH config contains `Match` blocks (`sshd -T` does not evaluate them). Ports that
Docker publishes bypass ufw; check them with `docker ps`.

## The lockout guard

- `ssh_password` and `ssh_rootlogin` are refused (exit 2, with the reason) unless the SSH login log (`journalctl`,
  last 7 days) shows that this session, found by client address and port, logged in with `publickey`.
  The session must also be live: `ss` has to show an established connection from that client address and port to
  the SSH port. A shell kept in tmux or screen carries the address of a login that closed long ago and is refused.
  The refusal also covers: no SSH session at all (console), `PubkeyAuthentication no`, an `AuthenticationMethods`
  setting, and a login user without any key in its authorized keys file (an `AuthorizedKeysCommand` counts as keys).
  Logins by SSH certificate are not recognised, so they are refused too.
- `PermitRootLogin no` and `forced-commands-only` are refused when you are logged in as root.
- ufw: every SSH port is allowed before `ufw --force enable`. The ports are the `Port` lines of `sshd -T`, the
  ports sshd listens on (systemd socket activation can differ from `Port`) and the port of your own connection.
- `ufw_enable` is also refused, naming the rule, when `ufw show added` lists a deny or reject rule that covers an
  SSH port or names no port at all (ufw hides such rules while it is off, then applies them on enable); the message
  gives the `ufw delete` command. A rule for another port is fine. It is refused too when SSH is reachable over IPv6
  (your session or a public sshd listener) and `/etc/default/ufw` does not say `IPV6=yes`, because ufw would then
  leave IPv6 unfiltered.
- A refused finding does not stop the others you picked; the exit code is still 2.
- `MaxAuthTries` cannot be set below 3: a client with a few keys would be cut off before the right one is tried.

## Apply, test, confirm

1. `apply` shows the findings. For each, choose apply, skip or edit.
2. It prints the diff of the SSH drop-in and the exact `ufw` commands, in order.
3. You type the server's hostname. This is the only way past the question, and it needs a terminal.
4. It backs up the drop-in (and the ufw state files when ufw is chosen), then arms a transient timer
   `softsynk-harden-revert-<time>.timer` (10 minutes by default). The timer is armed before anything changes;
   if it cannot be armed, nothing is changed.
5. It writes the drop-in, checks it with `sshd -t`, reloads `ssh.service` (open sessions stay up), and checks that
   `sshd -T` now shows every value. Then it applies the ufw commands.
6. Any failure in step 5 puts everything back at once. If even that fails, the timer stays armed.
7. **Open a NEW SSH session** (another terminal window), log in as you normally do, and run
   `/usr/local/bin/synk-harden confirm` there. `confirm` refuses the session that applied the change, a shell
   without a live SSH connection (the console, or an old tmux) and a session missing from the login log. It stops
   the timer and keeps the change. From the console, run `revert` instead, or log in over SSH and confirm there.
8. If you do nothing, or the new login does not work, the timer runs `synk-harden revert` and restores the
   previous files, turns ufw off if it was off before, reloads ssh and sends a notification mail (best effort).
   If a `confirm` or `revert` holds the lock at that moment, the timer retries for up to 2 minutes.

`revert` (without a timer) undoes a change that still waits for confirmation. A confirmed change stays: to undo it later,
delete `/etc/ssh/sshd_config.d/10-softsynk.conf`, run `/usr/bin/systemctl reload ssh.service` and review `/usr/sbin/ufw status`.

Running `apply` again is safe: it shows only what is still open, and keeps the earlier drop-in values. It refuses
while a change waits for confirmation.

## If you are locked out

Do not wait for the timer if you can help it: use the provider's web console, log in there, and run

```sh
/usr/local/bin/synk-harden revert
```

If you cannot, the timer restores the old settings by itself 10 minutes after `apply`. A reboot loses the timer
(it is transient); `--status-json` then reports `unhealthy` until you run `confirm` or `revert`.

## Config and state

`<root>/etc/hardening/config.json` (JSON, optional): `{"revert_minutes": 10}`, from 3 to 60. Unknown keys and
wrong types are errors (exit 64). Backups go to `<root>/data/hardening/backups/<time>/`, the change record to
`<root>/state/hardening/pending.json` (`pending`, `confirmed` or `reverted`), logs to `<root>/log/hardening/`.

## Status

`--status-json` is `not_installed` until a change is applied, `degraded` while a change waits for confirmation (the
summary names the time), `unhealthy` when it waits but its timer is gone, and `healthy` once confirmed. A reverted
change is mentioned as a warning. Findings and warnings are listed in the details.

## What it does not do

No SSH port change, no `AllowUsers`, no fail2ban, no `ufw limit`, no nftables or iptables management, no
two-factor setup. It does not touch an already active firewall. It does not check `TrustedUserCAKeys`.

## Files

| File | Purpose |
| --- | --- |
| `synk-harden` | The tool (Python 3 standard library plus `lib/softsynk`) |
| `module.toml` | Manifest for the Synk Server Manager engine (`install` and `configure` run the read-only `audit`, `status`, command `synk-harden`) |
