#!/usr/bin/env bash
#
# setup-auto-updates.sh - interactive, idempotent automatic updates for Ubuntu (systemd).
#
# Sets up unattended-upgrades so the server installs security updates (and optionally the
# distribution's stable updates and chosen third-party repos) every day without anyone logging
# in. All settings live in ONE managed file, so the distro's own files stay untouched. Reruns
# show the current state, keep current answers on Enter, and change only what differs. Every
# change is shown as a diff, confirmed, syntax-checked, backed up, written atomically and verified
# with a real unattended-upgrade dry run.
#
# Usage:  sudo ./setup-auto-updates.sh [--dry-run] [--root DIR] [--non-interactive] [--accept-defaults]
#         ./setup-auto-updates.sh --status-json | --help | --self-test
#
set -Eeuo pipefail
# apt, apt-cache and unattended-upgrade output is parsed below; translated messages would not match.
export LC_ALL=C

SELF=$(readlink -f -- "${BASH_SOURCE[0]}")
HERE=${SELF%/*}
# shellcheck source=../../lib/ui.sh
source "$HERE/../../lib/ui.sh"

readonly SCRIPT_VERSION="1.0.0"
# Loaded after 50unattended-upgrades (the distro defaults) and 20auto-upgrades, so these values win.
readonly CONF=/etc/apt/apt.conf.d/52unattended-upgrades-local
readonly NR_CONF=/etc/needrestart/conf.d/50-setup-auto-updates.conf
# LOG_FILE and LOCK_FILE live under SOFTSYNK_ROOT; main sets them once --root is known.
# Backups live outside apt.conf.d: apt would load a "*.bak-STAMP" copy there as live config.
readonly BACKUP_DIR=/var/backups/setup-auto-updates
readonly UU_LOG=/var/log/unattended-upgrades/unattended-upgrades.log
readonly TIMERS="apt-daily.timer apt-daily-upgrade.timer"
readonly DEFAULT_REBOOT_TIME="04:00"
readonly EMAIL_RE='^[^][:space:]@"\;{}[]+@[^][:space:]@"\;{}[]+\.[^][:space:]@"\;{}[]+$'

STAMP=$(date +%Y%m%d-%H%M%S)
readonly STAMP
DRY_RUN=${SOFTSYNK_DRY_RUN:-0}
WORK=""
APPLIED=0
UNCHANGED=0
FAILED=0
HEALTH="NOT RUN"
BACKUPS=()
CONF_BACKUP=""     # backup of $CONF made by this run ("" if the file was new)
CONF_WRITTEN=0
MANAGED=0          # 1 when $CONF exists and carries this script's header
DROPS=0            # 1 when a saved choice is about to be dropped (the write then defaults to No)
OS_ID=""
OS_NAME=""
# Choices (rendered into the managed file; also read back from it on reruns)
SCOPE=""        # security | updates
THIRD_PARTY=""  # space-separated repo hosts, e.g. "packages.sury.org nginx.org"
REBOOT=""       # off | HH:MM
MAIL=""         # empty = no mail

# ---------------------------------------------------------------- cleanup and signals

cleanup() {
  if [[ -n $WORK && -d $WORK ]]; then rm -rf -- "$WORK"; fi
  # An interrupt between install and mv in put_file leaves its temp file behind.
  rm -f -- "$CONF.tmp-$STAMP~" "$NR_CONF.tmp-$STAMP~" 2>/dev/null || true
}
on_signal() {
  printf '\n' >&2
  warn "Interrupted. Config files are replaced atomically, so none is half-written."
  list_backups
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM HUP
trap '[[ $BASH_SUBSHELL -gt 0 ]] || error "Unexpected failure at line $LINENO: $BASH_COMMAND"' ERR

# ---------------------------------------------------------------- pure helpers (bash 3.2 safe, see --self-test)

valid_time() { [[ $1 =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; }
valid_email() { [[ $1 =~ $EMAIL_RE ]]; }

# choice_get FILE KEY: value of a "//@ KEY=VALUE" line written by render_conf
choice_get() {
  [[ -r $1 ]] || return 0
  sed -n "s|^//@ $2=||p" "$1" | tail -n 1
}

# third_party_sites: read `apt-cache policy` on stdin, print each non-distro repo host once.
# Each repo prints a "release o=ORIGIN,..." line followed by an "origin HOST" line. Only plain
# host names pass, since they are written into the apt config.
third_party_sites() {
  awk '
    /^ *-?[0-9]+ / { o = "-"; next }
    /^ +release / { o = "-"; if (match($0, /[ ,]o=[^,]*/)) o = substr($0, RSTART + 3, RLENGTH - 3); next }
    /^ +origin / { if (o !~ /^(Ubuntu|Debian)/ && $2 ~ /^[A-Za-z0-9.-]+$/ && !seen[$2]++) print $2 }'
}

# recommended_site HOST -> 0 when the repo is safe to auto-update by default: versioned packages
# (php8.5-* only moves within 8.5; see the php alternative warning in ask_choices) or an
# internet-facing server that needs security fixes fast.
recommended_site() {
  case $1 in
    packages.sury.org | nginx.org) return 0 ;;
  esac
  return 1
}

# needrestart_mode FILE...: effective $nrconf{restart} (last assignment wins), "i" if unset
needrestart_mode() {
  local m
  m=$({ cat "$@" 2>/dev/null || true; } | sed -n "s/^[[:space:]]*\$nrconf{restart}[[:space:]]*=[[:space:]]*'\\([a-z]\\)'.*/\\1/p" | tail -n 1)
  echo "${m:-i}"
}

# render_conf: the complete managed file for the current choices
render_conf() {
  local s
  cat <<EOF
// Managed by setup-auto-updates.sh $SCRIPT_VERSION. Rerun the script to change these settings;
// manual edits here are replaced. Put permanent local overrides in a file sorted after this one.
//@ scope=$SCOPE
//@ third_party=$THIRD_PARTY
//@ reboot=$REBOOT
//@ mail=$MAIL

// Refresh package lists, download and install updates daily (apt-daily*.timer); clean cache weekly.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";

// Lists add to the distro defaults in 50unattended-upgrades.
Unattended-Upgrade::Allowed-Origins {
        "\${distro_id}:\${distro_codename}-security";
EOF
  # shellcheck disable=SC2016 # ${distro_id} is apt syntax, not a shell variable
  if [[ $SCOPE == updates ]]; then printf '        "${distro_id}:${distro_codename}-updates";\n'; fi
  printf '};\n'
  if [[ -n $THIRD_PARTY ]]; then
    printf '\n// Third-party repos chosen during setup.\nUnattended-Upgrade::Origins-Pattern {\n'
    for s in $THIRD_PARTY; do printf '        "site=%s";\n' "$s"; done
    printf '};\n'
  fi
  cat <<'EOF'

// Keep locally changed config files (nginx.conf, php.ini, ...) instead of skipping the package.
// This also applies to manual apt runs: they keep your config without asking.
Dpkg::Options {
        "--force-confdef";
        "--force-confold";
};

Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
EOF
  if [[ $REBOOT == off ]]; then
    printf '\nUnattended-Upgrade::Automatic-Reboot "false";\n'
  else
    # WithUsers "true": a forgotten SSH or tmux session must not block kernel updates forever.
    printf '\n// Reboot only when an update requires it (/var/run/reboot-required), at this server-local time.\n'
    printf 'Unattended-Upgrade::Automatic-Reboot "true";\n'
    printf 'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";\n'
    printf 'Unattended-Upgrade::Automatic-Reboot-Time "%s";\n' "$REBOOT"
  fi
  if [[ -n $MAIL ]]; then
    printf '\nUnattended-Upgrade::Mail "%s";\nUnattended-Upgrade::MailReport "only-on-error";\n' "$MAIL"
  fi
}

# ---------------------------------------------------------------- safe file changes

run() {
  if ((DRY_RUN)); then
    info "[dry-run] would run: $*"
    return 0
  fi
  "$@"
}

put_file() {
  if ((DRY_RUN)); then
    info "[dry-run] would write $2"
    return 0
  fi
  # The temp name ends in "~", which apt ignores, so a concurrent apt run never reads it.
  install -m "$3" -o "${4%%:*}" -g "${4##*:}" "$1" "$2.tmp-$STAMP~"
  mv -f "$2.tmp-$STAMP~" "$2"
}

# backup FILE: copy to $BACKUP_DIR/NAME.bak-STAMP (each file is backed up at most once per run);
# sets LAST_BACKUP ("" when FILE is absent)
backup() {
  local b="$BACKUP_DIR/${1##*/}.bak-$STAMP"
  LAST_BACKUP=""
  [[ -e $1 ]] || return 0
  LAST_BACKUP=$b
  if ((DRY_RUN)); then
    info "[dry-run] would back up $1"
    return 0
  fi
  install -d -m 0700 "$BACKUP_DIR"
  cp -a "$1" "$b"
  BACKUPS+=("$1 -> $b")
  info "Backup: $b"
}

list_backups() {
  local b
  for b in ${BACKUPS[@]+"${BACKUPS[@]}"}; do say "Backup: $b"; done
}

# ---------------------------------------------------------------- discovery

uu_installed() { [[ $(dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null) == "install ok installed" ]]; }

# problem MSG: a failed precondition. Fatal, except in a dry run, which only reads and carries on.
problem() {
  if ((DRY_RUN)); then warn "$1 (ignored in a dry run)"; else die "$1"; fi
}

detect_platform() {
  OS_NAME=unknown
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    OS_ID=$(. /etc/os-release && echo "${ID:-}")
    # shellcheck disable=SC1091
    OS_NAME=$(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")
  else
    problem "Cannot read /etc/os-release."
  fi
  # Debian's security/updates suites are named stable-security etc., so the origin lines below
  # would match nothing there.
  [[ $OS_ID == ubuntu ]] || problem "Unsupported OS: $OS_NAME. This script supports Ubuntu only."
  command -v systemctl >/dev/null || problem "systemd is required."
}

# A broken file elsewhere in apt.conf.d makes apt-config fail; report empty values, not a crash.
apt_dump() { apt-config dump 2>/dev/null || true; }
apt_value() { apt_dump | sed -n "s|^$1 \"\\(.*\\)\";\$|\\1|p" | tail -n 1; }
apt_list() { apt_dump | sed -n "s|^$1:: \"\\(.*\\)\";\$|\\1|p"; }
# Sorted so reruns list (and write) the repos in the same order.
detect_sites() { { apt-cache policy 2>/dev/null || true; } | third_party_sites | sort; }

needrestart_now() {
  local f=() x
  [[ -e /etc/needrestart/needrestart.conf ]] && f+=(/etc/needrestart/needrestart.conf)
  for x in /etc/needrestart/conf.d/*.conf; do [[ -e $x ]] && f+=("$x"); done
  ((${#f[@]})) || {
    echo i
    return 0
  }
  needrestart_mode "${f[@]}"
}

timer_line() {
  local t=$1
  printf '%s: %s/%s, next %s' "$t" "$(systemctl is-enabled "$t" 2>/dev/null || true)" \
    "$(systemctl is-active "$t" 2>/dev/null || true)" \
    "$(systemctl show -p NextElapseUSecRealtime --value "$t" 2>/dev/null || echo unknown)"
}

reboot_status() {
  if [[ -e /var/run/reboot-required ]]; then
    echo "yes ($(tr '\n' ' ' 2>/dev/null </var/run/reboot-required.pkgs | sed 's/ $//'))"
  else
    echo no
  fi
}

show_state() {
  local x
  section "Current state"
  say "OS:                $OS_NAME, timezone $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
  if uu_installed; then
    say "unattended-upgrades: $(dpkg-query -W -f='${Version}' unattended-upgrades)"
  else
    say "unattended-upgrades: NOT installed"
  fi
  for x in $TIMERS; do say "$(timer_line "$x")"; done
  say "Auto-upgrade:      APT::Periodic::Unattended-Upgrade=$(apt_value APT::Periodic::Unattended-Upgrade)"
  while IFS= read -r x; do say "Allowed origin:    $x"; done < <(apt_list Unattended-Upgrade::Allowed-Origins)
  while IFS= read -r x; do say "Origin pattern:    $x"; done < <(apt_list Unattended-Upgrade::Origins-Pattern)
  say "Automatic reboot:  $(apt_value Unattended-Upgrade::Automatic-Reboot) $(apt_value Unattended-Upgrade::Automatic-Reboot-Time)"
  say "needrestart mode:  $(needrestart_now) (a = restart services automatically)"
  say "Reboot required:   $(reboot_status)"
  say "Third-party repos: $(detect_sites | tr '\n' ' ')"
  if ((MANAGED)); then
    say "Managed file:      $CONF"
  elif [[ -e $CONF ]]; then
    warn "$CONF exists but was not written by this script; it will be replaced (after a diff and a backup)."
  else
    say "Managed file:      none yet"
  fi
}

# ---------------------------------------------------------------- steps

install_uu() {
  if uu_installed; then
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  section "Install"
  confirm "unattended-upgrades is not installed. Install it now? (needrestart may restart services using old libraries)" Y || die "unattended-upgrades is required."
  # umask 022: maintainer scripts must create world-readable apt config, not our private 077.
  run env DEBIAN_FRONTEND=noninteractive sh -c 'umask 022; exec apt-get install -y unattended-upgrades'
  APPLIED=$((APPLIED + 1))
  ((DRY_RUN)) || success "unattended-upgrades installed."
}

ask_choices() {
  local pick def s sites saved_tp distro
  distro=$(echo "${OS_ID:0:1}" | tr '[:lower:]' '[:upper:]')${OS_ID:1}
  section "Settings (Enter keeps the value in brackets)"

  def=2
  [[ $(choice_get "$CONF" scope) == security ]] && def=1
  choose pick "Which updates should install automatically?" "$def" \
    "Security updates only" \
    "Security + $distro stable updates (reviewed bug fixes, SRU) (recommended)"
  if ((pick == 1)); then SCOPE=security; else SCOPE=updates; fi

  THIRD_PARTY=""
  sites=$(detect_sites)
  saved_tp=" $(choice_get "$CONF" third_party) "
  if [[ -n $sites ]]; then
    say "" "Third-party repos are NOT updated unless you add them here. An update restarts that"
    say "service briefly (needrestart). PHP (sury) and nginx are internet-facing, so their security"
    say "fixes matter; repos that can jump major versions (redis.io) are safer to update by hand."
    for s in $sites; do
      if ((MANAGED)); then
        def=N
        [[ $saved_tp == *" $s "* ]] && def=Y
      elif recommended_site "$s"; then def=Y; else def=N; fi
      if confirm "Auto-update packages from every repo on $s?" "$def"; then THIRD_PARTY+="${THIRD_PARTY:+ }$s"; fi
    done
  fi
  for s in $saved_tp; do
    if [[ $'\n'$sites$'\n' != *$'\n'$s$'\n'* ]]; then
      warn "Previously chosen repo $s is not in 'apt-cache policy' now; it will be dropped."
      DROPS=1
    fi
  done
  # Unversioned sury metapackages (php, php-cli, php-fpm, ...) follow sury's default PHP version, and
  # the php alternative in auto mode then switches /usr/bin/php to the new version.
  if [[ " $THIRD_PARTY " == *" packages.sury.org "* ]] &&
    update-alternatives --query php 2>/dev/null | grep -qx 'Status: auto'; then
    warn "/usr/bin/php follows the newest installed PHP. Pin it so a new PHP version never takes over:"
    say "  sudo update-alternatives --set php \$(readlink -f /usr/bin/php)"
  fi

  s=$(choice_get "$CONF" reboot)
  def=2
  [[ $s == off ]] && def=1
  [[ -e /var/run/reboot-required ]] && warn "A reboot is already pending: with automatic reboot on, it happens at the reboot time after the next update run."
  choose pick "Kernel and core library updates only take effect after a reboot. Reboot automatically?" "$def" \
    "No, never reboot automatically (the login banner and this script show when one is needed)" \
    "Yes, only when an update requires it, at a set time (about 1-2 min downtime) (recommended)"
  if ((pick == 1)); then
    REBOOT=off
  else
    [[ -n $s && $s != off ]] || s=$DEFAULT_REBOOT_TIME
    while true; do
      ask REBOOT "Reboot time, 24h HH:MM in server time ($(timedatectl show -p Timezone --value 2>/dev/null || echo local))" "$s"
      valid_time "$REBOOT" && break
      warn "Use HH:MM, for example 04:00."
    done
  fi

  MAIL=""
  if [[ -x /usr/bin/mail || -x /usr/sbin/sendmail ]]; then
    s=$(choice_get "$CONF" mail)
    while true; do
      ask MAIL "Email for failure reports (Enter keeps, '-' for none; needs working outgoing mail)" "${s:--}"
      [[ $MAIL == - ]] && MAIL=""
      if [[ -z $MAIL ]] || valid_email "$MAIL"; then break; fi
      warn "That does not look like an email address."
    done
  else
    info "No mail command found; email reports skipped."
    s=$(choice_get "$CONF" mail)
    if [[ -n $s ]]; then
      warn "The saved report address $s will be dropped (no mail command)."
      DROPS=1
    fi
  fi
}

write_conf() {
  local new=$WORK/new.conf err=$WORK/apt.err def
  section "Managed config: $CONF"
  render_conf >"$new"
  if [[ -e $CONF ]] && cmp -s "$new" "$CONF"; then
    success "Already up to date."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  if [[ -e $CONF ]]; then
    diff -u "$CONF" "$new" >&2 || true
  else
    sed 's/^/    + /' "$new" >&2
  fi
  if ! apt-config dump >/dev/null 2>"$err"; then
    error "The existing apt configuration is already broken; fix it first (nothing written):"
    sed 's/^/    /' "$err" >&2
    FAILED=$((FAILED + 1))
    return 0
  fi
  if ! apt-config -c "$new" dump >/dev/null 2>"$err"; then
    error "apt rejected the generated file (nothing written):"
    sed 's/^/    /' "$err" >&2
    FAILED=$((FAILED + 1))
    return 0
  fi
  info "Syntax check passed (apt-config)."
  def=Y
  # An existing file this script did not write is someone's hand-made config: replace only on an explicit yes.
  # Dropping saved choices (often a transient apt-cache failure) also needs an explicit yes.
  if [[ -e $CONF ]] && ((MANAGED == 0 || DROPS)); then def=N; fi
  if ! confirm "Write these settings?" "$def"; then
    warn "Skipped; the current config stays as it is."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  backup "$CONF"
  CONF_BACKUP=$LAST_BACKUP
  put_file "$new" "$CONF" 0644 root:root
  CONF_WRITTEN=1
  APPLIED=$((APPLIED + 1))
  ((DRY_RUN)) || success "Written."
}

# restore_conf: undo this run's write_conf (previous file back, or remove a newly created one)
restore_conf() {
  if [[ -n $CONF_BACKUP ]]; then
    put_file "$CONF_BACKUP" "$CONF" 0644 root:root
    warn "Restored $CONF from $CONF_BACKUP."
  else
    rm -f -- "$CONF"
    warn "Removed $CONF; the distro defaults apply again."
  fi
  CONF_WRITTEN=0
  APPLIED=$((APPLIED - 1))
  FAILED=$((FAILED + 1))
}

ensure_timers() {
  local t bad=""
  section "Daily timers"
  for t in $TIMERS; do
    if [[ $(systemctl is-enabled "$t" 2>/dev/null) != enabled || $(systemctl is-active "$t" 2>/dev/null) != active ]]; then
      bad+=" $t"
    fi
  done
  if [[ -z $bad ]]; then
    success "Both timers are enabled and active."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  warn "Not enabled or not running:$bad"
  # Persistent=true: a timer that missed its time fires right away, so a real upgrade can start now.
  if confirm "Enable and start them? (a missed run may start a real upgrade right away)" Y; then
    # shellcheck disable=SC2086
    if run systemctl enable --now $bad; then
      APPLIED=$((APPLIED + 1))
    else
      error "Could not enable the timers (masked?). Check: systemctl status$bad"
      FAILED=$((FAILED + 1))
    fi
  else
    UNCHANGED=$((UNCHANGED + 1))
  fi
}

ensure_needrestart() {
  local tmp=$WORK/nr.conf mode
  section "Service restarts after library updates (needrestart)"
  if ! command -v needrestart >/dev/null; then
    info "needrestart is not installed; services keep old libraries until restarted or rebooted."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  mode=$(needrestart_now)
  if [[ $mode == a ]]; then
    success "Mode 'a': services are restarted automatically."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  warn "Mode '$mode': unattended runs only list services that still use old libraries."
  if confirm "Set automatic restarts (mode 'a') in $NR_CONF?" Y; then
    # shellcheck disable=SC2016 # $nrconf is Perl syntax for needrestart
    printf '# Managed by setup-auto-updates.sh: restart services automatically after library updates.\n$nrconf{restart} = '"'a'"';\n' >"$tmp"
    backup "$NR_CONF"
    put_file "$tmp" "$NR_CONF" 0644 root:root
    APPLIED=$((APPLIED + 1))
  else
    UNCHANGED=$((UNCHANGED + 1))
  fi
}

# ---------------------------------------------------------------- verification

health_check() {
  local fails=0 skipped=0 rc out=$WORK/uu-dry.log want got s t u sites units=""
  section "Health checks"
  if ((DRY_RUN)); then
    HEALTH="SKIPPED (dry run)"
    info "Skipped in dry-run mode (the new settings were not written)."
    return 0
  fi

  if [[ $(apt_value APT::Periodic::Unattended-Upgrade) == 1 && $(apt_value APT::Periodic::Enable) != 0 ]]; then
    success "Daily unattended upgrade is on."
  else
    error "Daily upgrades are off in the effective apt config (APT::Periodic::Unattended-Upgrade not 1, or APT::Periodic::Enable 0)."
    fails=$((fails + 1))
  fi

  want=$(choice_get "$CONF" reboot)
  if [[ -n $want ]]; then
    got=$(apt_value Unattended-Upgrade::Automatic-Reboot)
    case ${got,,} in 1 | yes | true | on) got=true ;; esac
    if [[ ($want == off && $got != true) || ($want != off && $got == true && $(apt_value Unattended-Upgrade::Automatic-Reboot-Time) == "$want") ]]; then
      success "Reboot setting is effective (${want})."
    else
      error "Reboot setting is overridden by a file after $CONF (effective: $got)."
      fails=$((fails + 1))
    fi
  fi

  for t in $TIMERS; do
    # An active timer whose service is masked starts nothing.
    if [[ $(systemctl is-active "$t" 2>/dev/null) == active && $(systemctl is-enabled "${t%.timer}.service" 2>/dev/null) != masked ]]; then
      success "$t active."
    else
      error "$t is not active, or ${t%.timer}.service is masked."
      fails=$((fails + 1))
    fi
  done

  # oneshot units are "activating" while they run, not "active".
  if systemctl show -p ActiveState --value apt-daily.service apt-daily-upgrade.service 2>/dev/null | grep -qx -e active -e activating; then
    warn "A scheduled apt run is in progress; skipping the dry run. Rerun the script later to test."
    skipped=1
  else
    info "Running 'unattended-upgrade --dry-run --debug' (downloads but installs nothing new; can take a minute)..."
    rc=0
    # Even with --dry-run, unattended-upgrade finishes an interrupted dpkg run (dpkg --configure -a).
    # umask 022: the dry run downloads packages and refreshes apt caches that must stay world-readable.
    # --foreground: Ctrl-C reaches unattended-upgrade instead of waiting for the timeout; -k: it treats
    # SIGTERM as "finish gracefully", so kill it if it is still running 60 s later.
    (umask 022 && exec timeout --foreground -k 60 900 unattended-upgrade --dry-run --debug) >"$out" 2>&1 || rc=$?
    if ((rc == 0)); then
      success "Dry run passed."
      s=$(sed -n 's/^Packages that will be upgraded: *//p' "$out" | tail -n 1)
      say "Would upgrade now: ${s:-nothing pending}"
      # "Allowed origins" only echoes the config, so also require the repo to still exist in apt.
      sites=$(detect_sites)
      for t in $(choice_get "$CONF" third_party); do
        if grep -qF "site=$t" "$out" && [[ $'\n'$sites$'\n' == *$'\n'$t$'\n'* ]]; then success "Third-party repo active: $t"; else
          error "Third-party repo $t is not both allowed in the dry run and present in apt sources."
          fails=$((fails + 1))
        fi
      done
    # Match only the lock messages: a plain "lock" also matches debug lines such as "blocklist".
    elif grep -q -e 'Lock could not be acquired' -e 'Lock file is already taken' -e 'Cache lock can not be acquired' \
      -e 'Could not get lock' "$out"; then
      warn "apt is busy (another apt/dpkg run holds the lock); dry run skipped. Rerun the script later to test."
      skipped=1
    else
      if ((rc == 124)); then error "Dry run timed out after 15 minutes. Last lines:"; else error "Dry run failed (exit $rc). Last lines:"; fi
      tail -n 15 "$out" | sed 's/^/    /' >&2
      fails=$((fails + 1))
      # Default No: network or mirror trouble also fails the dry run and is no reason to undo.
      if ((CONF_WRITTEN)) && confirm "Undo this run's config change ($CONF)?" N; then
        restore_conf
      fi
    fi
  fi

  if [[ $(choice_get "$CONF" reboot) != off && -e $CONF ]]; then
    # After an automatic reboot every service must come back on its own.
    # Template instances (redis-server@NAME) appear in list-units, not in list-unit-files.
    for u in nginx postgresql supervisor redis-server \
      $(systemctl list-unit-files 'php*-fpm.service' --no-legend 2>/dev/null | awk '{print $1}') \
      $(systemctl list-units --all --plain --no-legend 'redis-server@*.service' 2>/dev/null | awk '{print $1}'); do
      systemctl cat "$u" >/dev/null 2>&1 || continue
      [[ $(systemctl is-enabled "$u" 2>/dev/null) == enabled ]] || units+=" $u"
    done
    if [[ -z $units ]]; then success "Core services start at boot."; else
      warn "Not enabled at boot (would stay down after an automatic reboot):$units"
    fi
  fi

  if ((fails > 0)); then HEALTH=FAILED; elif ((skipped)); then HEALTH="PASSED (dry run skipped)"; else HEALTH=PASSED; fi
}

print_summary() {
  local changes result line="────────────────────────────────" scope tp rb ml last src=$CONF
  # A dry run shows the planned settings instead of the unchanged file.
  if ((DRY_RUN && CONF_WRITTEN)); then src=$WORK/new.conf; fi
  scope=$(choice_get "$src" scope)
  case $scope in
    security) scope="Security only" ;;
    updates) scope="Security + stable updates" ;;
    *) scope="Distro defaults (not managed)" ;;
  esac
  tp=$(choice_get "$src" third_party)
  rb=$(choice_get "$src" reboot)
  case $rb in
    off) rb="Off" ;;
    "") rb="$(apt_value Unattended-Upgrade::Automatic-Reboot) (not managed)" ;;
    *) rb="At $rb when required" ;;
  esac
  ml=$(choice_get "$src" mail)
  # From the timer, not $UU_LOG: this script's own dry run also logs a start line there.
  last=$(systemctl show -p LastTriggerUSec --value apt-daily-upgrade.timer 2>/dev/null) || true
  [[ $last == n/a ]] && last=""

  printf '\n%s\n%s\n' "Auto-Updates Summary" "$line"
  printf '%-17s%s\n' "OS:" "$OS_NAME" \
    "Updates:" "$scope" \
    "Third-party:" "${tp:-none}" \
    "Next run:" "$(systemctl show -p NextElapseUSecRealtime --value apt-daily-upgrade.timer 2>/dev/null || echo unknown)" \
    "Last run:" "${last:-unknown}" \
    "Reboot:" "$rb" \
    "Restarts:" "needrestart mode $(needrestart_now)" \
    "Email:" "${ml:-none}" \
    "Reboot needed:" "$(reboot_status)"
  if ((DRY_RUN)); then
    changes="$APPLIED planned (dry run), $UNCHANGED unchanged"
    result="DRY RUN (nothing changed)"
    if ((FAILED > 0)); then result="DRY RUN FAILED (nothing changed)"; fi
  else
    changes="$APPLIED applied, $UNCHANGED unchanged"
    result=SUCCESS
    if [[ $HEALTH == FAILED ]] || ((FAILED > 0)); then result=FAILED; fi
  fi
  if ((FAILED > 0)); then changes+=", $FAILED failed"; fi
  printf '\n%-17s%s\n' "Health Checks:" "$HEALTH"
  printf '%-17s%s\n' "Changes:" "$changes" "Result:" "$result"
  printf '%s\n' "$line"
  list_backups
  if ((DRY_RUN == 0)); then say "Session log: $LOG_FILE" "Update log:  $UU_LOG"; fi
  [[ $result != *FAILED* ]]
}

# ---------------------------------------------------------------- status (--status-json)

# json_str TEXT: TEXT as a JSON string (control characters dropped, quotes and backslashes escaped).
json_str() {
  local s
  s=$(printf '%s' "$1" | tr -d '\000-\010\013\014\016-\037\177')
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

ST_DET="" ST_WARN="" ST_ACT="" ST_FIRST="" ST_HEALTH=healthy
st_s() { ST_DET+="${ST_DET:+,}$(json_str "$1"):$(json_str "$2")"; }
st_b() { ST_DET+="${ST_DET:+,}$(json_str "$1"):$([[ $2 == 1 ]] && echo true || echo false)"; }
st_act() { ST_ACT+="${ST_ACT:+,}$(json_str "$1")"; }
# st_bad LEVEL MSG: record a finding; the worst level (unknown < degraded < unhealthy) wins.
st_bad() {
  ST_WARN+="${ST_WARN:+,}$(json_str "$2")"
  [[ -n $ST_FIRST ]] || ST_FIRST=$2
  case $ST_HEALTH:$1 in
    healthy:* | unknown:degraded | unknown:unhealthy | degraded:unhealthy) ST_HEALTH=$1 ;;
  esac
}
# st_emit INSTALL_STATE HEALTH SUMMARY: the one JSON document, on stdout.
st_emit() {
  printf '{"schema":1,"module":"auto-updates","version":%s,"observed_at":"%s","install_state":"%s","health":"%s","summary":%s,"details":{%s},"warnings":[%s],"actions":[%s]}\n' \
    "$(json_str "$SCRIPT_VERSION")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$(json_str "$3")" "$ST_DET" "$ST_WARN" "$ST_ACT"
}

# status_json: read-only snapshot for softsynk-server. It takes no lock, writes no log or temp file,
# needs neither root nor gum, and never runs unattended-upgrade (its dry run downloads packages and
# holds the apt lock). Anything it cannot read makes the health "unknown", never a crash.
status_json() {
  set +e +E
  trap - ERR
  local os="" state=not_installed managed=0 uu=0 ok t x got want scope="" summary rb=off
  # shellcheck disable=SC1091
  [[ -r /etc/os-release ]] && os=$(. /etc/os-release 2>/dev/null && echo "${ID:-}")
  if [[ $os != ubuntu ]]; then
    st_act "Use this module on Ubuntu with systemd."
    st_emit not_installed unsupported "Ubuntu only (found ${os:-unknown})"
    return 0
  fi
  grep -q '^// Managed by setup-auto-updates.sh' "$CONF" 2>/dev/null && managed=1
  uu_installed && uu=1
  if ((managed)); then state=installed; elif ((uu)); then state=adopted; fi
  if [[ $state == not_installed ]]; then
    st_act "Run setup-auto-updates.sh."
    st_emit not_installed unknown "not installed"
    return 0
  fi
  st_b managed "$managed"
  if ((uu)); then
    st_s unattended_upgrades "$(dpkg-query -W -f='${Version}' unattended-upgrades 2>/dev/null)"
  else
    st_bad unhealthy "unattended-upgrades is not installed"
  fi
  if ((managed)); then
    scope=$(choice_get "$CONF" scope)
    want=$(choice_get "$CONF" reboot)
    st_s scope "$scope"
    st_s third_party "$(choice_get "$CONF" third_party)"
    st_s reboot "$want"
  else
    st_act "Run setup-auto-updates.sh to manage these settings."
    [[ -e $CONF ]] && st_bad degraded "$CONF exists but was not written by this script"
  fi

  if apt-config dump >/dev/null 2>&1; then
    if [[ $(apt_value APT::Periodic::Unattended-Upgrade) == 1 && $(apt_value APT::Periodic::Enable) != 0 ]]; then
      st_b auto_upgrade 1
    else
      st_b auto_upgrade 0
      st_bad unhealthy "daily upgrades are off in the effective apt config"
    fi
    got=$(apt_value Unattended-Upgrade::Automatic-Reboot | tr '[:upper:]' '[:lower:]')
    case $got in 1 | yes | true | on) got=true ;; esac
    if [[ -n ${want-} ]]; then
      ok=0
      if [[ $want == off ]]; then
        [[ $got != true ]] && ok=1
      else
        [[ $got == true && $(apt_value Unattended-Upgrade::Automatic-Reboot-Time) == "$want" ]] && ok=1
      fi
      ((ok)) || st_bad unhealthy "the reboot setting is overridden by a file sorted after $CONF"
    fi
    if [[ $got == true ]]; then rb="at $(apt_value Unattended-Upgrade::Automatic-Reboot-Time)"; fi
  else
    st_bad unknown "apt-config could not read the apt configuration"
  fi

  for t in $TIMERS; do
    x=$(systemctl is-active "$t" 2>/dev/null)
    if [[ -z $x ]]; then
      st_bad unknown "systemd did not answer for $t"
    elif [[ $x != active || $(systemctl is-enabled "${t%.timer}.service" 2>/dev/null) == masked ]]; then
      st_bad unhealthy "$t is not active, or ${t%.timer}.service is masked"
    fi
  done
  st_s next_run "$(systemctl show -p NextElapseUSecRealtime --value apt-daily-upgrade.timer 2>/dev/null)"
  x=$(systemctl show -p LastTriggerUSec --value apt-daily-upgrade.timer 2>/dev/null)
  [[ $x == n/a ]] && x=""
  st_s last_run "$x"
  st_s needrestart_mode "$(needrestart_now)"

  if [[ -e /var/run/reboot-required ]]; then
    st_b reboot_required 1
    if [[ ${got-} != true ]]; then
      st_bad degraded "a reboot is pending and automatic reboot is off"
      st_act "Reboot the server when convenient."
    fi
  else
    st_b reboot_required 0
  fi

  case $scope in
    security) scope="security updates" ;;
    updates) scope="security + stable updates" ;;
    *) scope="distro defaults" ;;
  esac
  if [[ $ST_HEALTH == healthy ]]; then summary="daily updates on ($scope), reboot $rb"; else summary=$ST_FIRST; fi
  st_emit "$state" "$ST_HEALTH" "$summary"
}

usage() {
  cat <<EOF
setup-auto-updates.sh $SCRIPT_VERSION - automatic security updates for Ubuntu (systemd)

Usage:
  sudo ./setup-auto-updates.sh             interactive setup; reruns keep current answers on Enter
  sudo ./setup-auto-updates.sh --dry-run   ask everything, show the diff; change nothing
  ./setup-auto-updates.sh --status-json    print the module status as JSON (read-only, no root needed)
  ./setup-auto-updates.sh --self-test      check the script's internal helpers (no root needed)
  ./setup-auto-updates.sh --help           this text

Options:
  --root DIR          management root (default \$SOFTSYNK_ROOT or /opt/softsynk-server-management)
  --non-interactive   never prompt; a question without an answer exits 64
  --accept-defaults   with --non-interactive (or no terminal), take each question's default

Defaults: security + stable updates daily, PHP (sury) and nginx.org repos when present,
reboot at $DEFAULT_REBOOT_TIME only when required, keep local config files. See README.md.
Exit codes: 0 ok, 1 failed, 64 missing or invalid input, 130 cancelled.
EOF
}

# ---------------------------------------------------------------- self-test

self_test() {
  local fails=0 t out
  t=$(mktemp -d)
  WORK=$t
  check() {
    if [[ $1 != "$2" ]]; then
      printf 'FAIL %s: got [%s], want [%s]\n' "$3" "$1" "$2" >&2
      fails=$((fails + 1))
    fi
  }
  yn() { if "$@"; then echo yes; else echo no; fi; }
  check "$(yn valid_time 04:00)" yes time1
  check "$(yn valid_time 23:59)" yes time2
  check "$(yn valid_time 24:00)" no time3
  check "$(yn valid_time 4:00)" no time4
  check "$(yn valid_email ops@example.com)" yes mail1
  check "$(yn valid_email 'a"b@x.com')" no mail2
  check "$(yn valid_email 'a@x.com;')" no mail3
  check "$(yn valid_email 'a@x')" no mail4
  check "$(yn recommended_site packages.sury.org)" yes rec1
  check "$(yn recommended_site packages.redis.io)" no rec2
  check "$(json_str 'a"b\c')" '"a\"b\\c"' json1
  check "$(json_str "$(printf 'x\ty\001z')")" '"x\tyz"' json2

  out=$(third_party_sites <<'EOF'
Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://security.ubuntu.com/ubuntu resolute-security/main amd64 Packages
     release v=26.04,o=Ubuntu,a=resolute-security,n=resolute,l=Ubuntu,c=main,b=amd64
     origin security.ubuntu.com
 500 http://flat.example/repo ./ Packages
     origin flat.example
 500 https://packages.redis.io/deb resolute/main all Packages
     release o=packages.redis.io,a=resolute,n=resolute,c=main,b=all
     origin packages.redis.io
 500 https://packages.redis.io/deb resolute/main amd64 Packages
     release o=packages.redis.io,a=resolute,n=resolute,c=main,b=amd64
     origin packages.redis.io
 500 https://packages.sury.org/php resolute/main amd64 Packages
     release o=deb.sury.org,a=resolute,n=resolute,c=main,b=amd64
     origin packages.sury.org
 500 https://deb.nodesource.com/node_22.x nodistro/main amd64 Packages
     release o=. nodistro,a=nodistro,n=nodistro,l=. nodistro,c=main,b=amd64
     origin deb.nodesource.com
 500 https://example.com/norelease ./ Packages
     release c=
     origin example.com
 500 https://bad.example/x ./ Packages
     release o=Bad
     origin bad;host{x}
EOF
  )
  check "$(echo "$out" | tr '\n' ' ')" "flat.example packages.redis.io packages.sury.org deb.nodesource.com example.com " sites

  printf "\$nrconf{restart} = 'i';\n" >"$t/a.conf"
  printf "#\$nrconf{restart} = 'l';\n  \$nrconf{restart} = 'a';\n" >"$t/b.conf"
  check "$(needrestart_mode "$t/a.conf" "$t/b.conf")" a nr1
  check "$(needrestart_mode "$t/a.conf")" i nr2
  : >"$t/c.conf"
  check "$(needrestart_mode "$t/c.conf")" i nr3

  SCOPE=updates THIRD_PARTY="packages.sury.org nginx.org" REBOOT=04:00 MAIL=ops@example.com
  render_conf >"$t/r.conf"
  check "$(choice_get "$t/r.conf" scope)" updates c_scope
  check "$(choice_get "$t/r.conf" third_party)" "packages.sury.org nginx.org" c_tp
  check "$(choice_get "$t/r.conf" reboot)" 04:00 c_reboot
  check "$(choice_get "$t/r.conf" mail)" ops@example.com c_mail
  check "$(choice_get "$t/missing" scope)" "" c_missing
  check "$(grep -c '"site=' "$t/r.conf")" 2 r_sites
  # shellcheck disable=SC2016 # literal apt variables
  check "$(grep -c '${distro_codename}-updates' "$t/r.conf")" 1 r_updates
  check "$(grep -c 'Automatic-Reboot-Time "04:00"' "$t/r.conf")" 1 r_rtime
  check "$(grep -c 'MailReport' "$t/r.conf")" 1 r_mail
  check "$(grep -c ' {$' "$t/r.conf")" "$(grep -c '^};$' "$t/r.conf")" r_braces

  SCOPE=security THIRD_PARTY="" REBOOT=off MAIL=""
  render_conf >"$t/s.conf"
  check "$(grep -c -e '}-updates' -e 'Origins-Pattern' -e 'Mail' -e 'Reboot-Time' "$t/s.conf")" 0 s_absent
  check "$(grep -c 'Automatic-Reboot "false"' "$t/s.conf")" 1 s_reboot
  # shellcheck disable=SC2016 # literal apt variables
  check "$(grep -c '${distro_codename}-security' "$t/s.conf")" 1 s_security
  check "$(choice_get "$t/s.conf" third_party)" "" s_tp
  render_conf >"$t/s2.conf"
  check "$(yn cmp -s "$t/s.conf" "$t/s2.conf")" yes idempotent

  if ((fails)); then
    echo "self-test: $fails failure(s)" >&2
    return 1
  fi
  echo "self-test: all checks passed"
}

# ---------------------------------------------------------------- main

need_value() { (($# >= 2)) && [[ $2 != --* ]] || ui_need "$1 needs a value."; }

main() {
  local mode=run
  while (($#)); do
    if ui_flag "$1"; then
      shift
      continue
    fi
    case $1 in
      -h | --help)
        usage
        exit 0
        ;;
      --self-test) mode=self-test ;;
      --status-json) mode=status ;;
      --dry-run) DRY_RUN=1 ;;
      --root)
        need_value "$@"
        SOFTSYNK_ROOT=$2
        shift
        ;;
      *)
        error "Unknown option: $1 (see --help)"
        exit 64
        ;;
    esac
    shift
  done
  [[ $DRY_RUN == 1 ]] || DRY_RUN=0
  [[ $SOFTSYNK_ROOT == /* ]] || ui_need "--root must be an absolute path."
  case $mode in
    self-test)
      self_test
      exit
      ;;
    status)
      status_json
      exit 0
      ;;
  esac

  ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4))) || problem "Bash 4.4 or newer is required."
  [[ $EUID -eq 0 ]] || problem "Run as root: sudo $0"
  LOG_FILE=$(ss_path log auto-updates)/setup-auto-updates.log
  LOCK_FILE=$(ss_path lock)/auto-updates.lock
  readonly LOG_FILE LOCK_FILE
  umask 077
  WORK=$(mktemp -d /tmp/setup-auto-updates.XXXXXX)
  # A dry run changes nothing, so it creates no log or lock under the root and takes no lock.
  if ((DRY_RUN == 0)); then
    install -d -m 0755 -- "${LOG_FILE%/*}" "${LOCK_FILE%/*}"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another setup-auto-updates.sh run is in progress."
  fi

  detect_platform
  # Prompts need gum (a non-interactive run never prompts, so it never needs it).
  if ((DRY_RUN == 0)) && ui_interactive; then ui_install_gum || die "gum is needed for the prompts."; fi
  if grep -q '^// Managed by setup-auto-updates.sh' "$CONF" 2>/dev/null; then MANAGED=1; fi
  info "setup-auto-updates.sh $SCRIPT_VERSION$( ((DRY_RUN)) && echo ' (dry run: nothing will be changed)' || true)"
  show_state
  install_uu
  ask_choices
  write_conf
  ensure_timers
  ensure_needrestart
  health_check
  print_summary || exit 1
}

main "$@"
