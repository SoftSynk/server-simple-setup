#!/usr/bin/env bash
# setup-backup.sh - install and configure the database backup module (synk-backup + systemd timer).
#
# A thin wizard: it installs gum and restic, asks the questions, hands everything to
# `synk-backup configure` (which validates before it writes anything) and only then installs the
# systemd units and enables the timer. All backup logic lives in synk-backup. Safe to rerun.
set -Eeuo pipefail

SELF=$(readlink -f -- "${BASH_SOURCE[0]}")
HERE=${SELF%/*}
SYNK=$HERE/synk-backup
SCRIPT_VERSION=1.0.0
# shellcheck source=../../lib/ui.sh
source "$HERE/../../lib/ui.sh"

usage() {
  cat <<'EOF'
Usage: setup-backup.sh [--configure] [--dry-run] [--root DIR] [--non-interactive] [--accept-defaults]
                       [--ANSWER VALUE]...

  (no option)      install packages, ask the questions, configure, install the systemd timer
  --configure      only re-run the questions and the configuration (the module must be installed)
  --dry-run        ask and validate everything, change nothing
  --root DIR       management root (default $SOFTSYNK_ROOT or /opt/softsynk-server-management)
  --non-interactive, --accept-defaults, --version, --help

Answers (each one skips its prompt; "-" means "none" for the exclusions):
  --local-repo PATH            --remote-preset none|r2|b2|hetzner|custom
  --remote-id ID               account id (r2) or region / location (b2, hetzner)
  --remote-endpoint URL        custom preset only        --remote-region NAME
  --remote-bucket NAME         --remote-prefix PATH
  --mail-to ADDRESS (deprecated, ignored: failure mails use synk-notify)
  --retention-daily N          --retention-weekly N
  --retention-twice-monthly N  --exclude-databases a,b

Credentials are never accepted as options (a command line is visible to every user via ps). Set them in
the environment instead, or answer the prompts:
  SOFTSYNK_ANSWER_REPO_PASSWORD         repository password (at least 12 characters)
  SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID      S3 access key id
  SOFTSYNK_ANSWER_S3_SECRET_ACCESS_KEY  S3 secret access key
A new repository password is only generated for a person at a terminal: scripted runs must supply it.

Exit codes: 0 ok, 1 failed, 64 missing or invalid input, 130 cancelled.
EOF
}

# ---------------------------------------------------------------- options

DRY_RUN=${SOFTSYNK_DRY_RUN:-0}
MODE=install

need_value() { (($# >= 2)) && [[ $2 != --* ]] || ui_need "$1 needs a value."; }

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
    --version)
      echo "setup-backup.sh $SCRIPT_VERSION"
      exit 0
      ;;
    --dry-run) DRY_RUN=1 ;;
    --configure) MODE=configure ;;
    --root)
      need_value "$@"
      SOFTSYNK_ROOT=$2
      shift
      ;;
    --local-repo | --remote-preset | --remote-id | --remote-endpoint | --remote-region | --remote-bucket | \
      --remote-prefix | --mail-to | --retention-daily | --retention-weekly | \
      --retention-twice-monthly | --exclude-databases)
      need_value "$@"
      name=$(printf '%s' "${1#--}" | tr '[:lower:]-' '[:upper:]_')
      export "SOFTSYNK_ANSWER_$name=$2"
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
export SOFTSYNK_ROOT
# These paths end up in systemd unit files and sed expressions: keep them plain.
if [[ ! $SOFTSYNK_ROOT =~ ^/[A-Za-z0-9_./-]*$ || ! $HERE =~ ^/[A-Za-z0-9_./+-]*$ ]]; then
  ui_need "The root and this module's folder must be absolute paths without spaces or special characters."
fi
# shellcheck disable=SC2034  # read by lib/ui.sh: every info/warn/error line is appended to it
LOG_FILE=$(ss_path log backup)/setup-backup.log
trap 'ui_cancel' INT

UNIT_DIR=${SOFTSYNK_SYSTEMD_DIR:-/etc/systemd/system}
BIN_DIR=${SOFTSYNK_BIN_DIR:-/usr/local/bin}
PROBLEMS=0

# ---------------------------------------------------------------- preflight

problem() {
  PROBLEMS=$((PROBLEMS + 1))
  if [[ $DRY_RUN == 1 ]]; then warn "$1 (ignored in a dry run)"; else error "$1"; fi
}

# shellcheck disable=SC1091
os_field() { ( . /etc/os-release 2>/dev/null && printf '%s' "${!1-}" ) || true; }

preflight() {
  section "Checking this machine"
  local id ver t
  [[ $EUID -eq 0 ]] || problem "Run as root (sudo)."
  id=$(os_field ID)
  ver=$(os_field VERSION_ID)
  if [[ $id != ubuntu ]]; then
    problem "Ubuntu 26.04 or newer is required (found ${id:-unknown} ${ver})."
  elif [[ $(printf '%s\n26.04\n' "$ver" | sort -V | head -n1) != 26.04 ]]; then
    problem "Ubuntu 26.04 or newer is required (found $ver)."
  fi
  for t in pg_dump pg_dumpall pg_restore psql python3; do
    command -v "$t" >/dev/null 2>&1 || problem "$t is missing (postgresql-client / python3)."
  done
  id postgres >/dev/null 2>&1 || problem "The 'postgres' OS user does not exist: is PostgreSQL installed on this machine?"
  if ((PROBLEMS > 0)) && [[ $DRY_RUN != 1 ]]; then exit 1; fi
  ((PROBLEMS > 0)) || success "Preflight passed."
}

install_packages() {
  section "Packages"
  if command -v gum >/dev/null 2>&1 && command -v restic >/dev/null 2>&1; then
    success "gum and restic are already installed."
    return 0
  fi
  if [[ $DRY_RUN == 1 ]]; then
    info "[dry run] would install whichever of gum and restic is missing"
    return 0
  fi
  if ! ui_install_gum; then
    ! ui_interactive || die "gum is needed for the prompts."
    warn "gum is not available; continuing without prompts."
  fi
  if ! command -v restic >/dev/null 2>&1; then
    info "Installing restic with apt-get"
    DEBIAN_FRONTEND=noninteractive apt-get install -y restic ||
      { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y restic; }
  fi
  command -v restic >/dev/null 2>&1 || die "restic is still missing after apt-get."
}

# ---------------------------------------------------------------- questions

# D_<key>: defaults from the stored configuration (it holds no secrets).
load_defaults() {
  local out="" k v
  if [[ -x $SYNK ]]; then
    out=$("$SYNK" --root "$SOFTSYNK_ROOT" config 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
c, r = d["config"], d["config"].get("remote") or {}
rows = {"local_repo": c["local_repo"], "preset": r.get("preset", "none"), "repo": r.get("repo", ""),
        "endpoint": r.get("endpoint", ""), "region": r.get("region", ""), "bucket": r.get("bucket", ""),
        "prefix": r.get("prefix") or d["defaults"]["prefix"],
        "daily": c["retention"]["daily"], "weekly": c["retention"]["weekly"],
        "twice_monthly": c["retention"]["twice_monthly"], "exclude": ",".join(c["exclude_databases"]),
        "has_password": str(d["has_password"]).lower(), "has_s3_keys": str(d["has_s3_keys"]).lower()}
for k, v in rows.items():
    print("%s=%s" % (k, v))
') || out=""
  fi
  D_local_repo="$SOFTSYNK_ROOT/data/backup/restic" D_preset=none D_repo="" D_endpoint="" D_region="" D_bucket=""
  D_prefix="softsynk-$(hostname -s 2>/dev/null || echo server)" D_daily=3 D_weekly=1 D_twice_monthly=2
  D_exclude="" D_has_password=false D_has_s3_keys=false
  while IFS='=' read -r k v; do
    case $k in
      local_repo | preset | repo | endpoint | region | bucket | prefix | daily | weekly | twice_monthly | exclude | \
        has_password | has_s3_keys) printf -v "D_$k" '%s' "$v" ;;
    esac
  done <<<"$out"
}

# ask_required VAR "Question" [default]: like ask, but an empty answer is not accepted.
ask_required() {
  local v=$1
  while :; do
    ask "$@"
    [[ -z ${!v} ]] || return 0
    ui_interactive || ui_need "A value is required: pass $(ui_opt_name "$v") VALUE."
    warn "An answer is required."
  done
}

ask_remote() {
  PRESET=none REMOTE_ID="" REMOTE_ENDPOINT="" REMOTE_REGION="" REMOTE_BUCKET="" REMOTE_PREFIX=""
  S3_ACCESS_KEY_ID="" S3_SECRET_ACCESS_KEY="" KEEP_REMOTE=0
  if [[ -n $D_repo ]]; then
    info "The remote is a repository location set by hand in the configuration; leaving it as it is."
    KEEP_REMOTE=1
    return 0
  fi
  local def=1 names=(none r2 b2 hetzner custom) d_id="" d_endpoint=""
  case $D_preset in r2) def=2 ;; b2) def=3 ;; hetzner) def=4 ;; custom) def=5 ;; esac
  section "Off-server copy (S3-compatible)"
  choose REMOTE_PRESET "Where should the off-server copy go?" "$def" \
    "none - local backups only (not recommended)" "r2 - Cloudflare R2" "b2 - Backblaze B2" \
    "hetzner - Hetzner Object Storage" "custom - any other S3-compatible service"
  PRESET=${names[$((REMOTE_PRESET - 1))]}
  if [[ $PRESET == none ]]; then
    warn "No off-server copy: a lost or compromised server also loses every backup."
    return 0
  fi
  if [[ $PRESET == "$D_preset" ]]; then
    d_endpoint=$D_endpoint
    case $PRESET in
      r2) d_id=$(sed -E 's#^https://([^.]+)\.r2\.cloudflarestorage\.com$#\1#' <<<"$D_endpoint") ;;
      b2 | hetzner) d_id=$D_region ;;
    esac
  fi
  case $PRESET in
    r2) ask_required REMOTE_ID "Cloudflare account id" "$d_id" ;;
    b2) ask_required REMOTE_ID "Backblaze region (for example us-west-004)" "$d_id" ;;
    hetzner) ask_required REMOTE_ID "Hetzner location (for example fsn1)" "$d_id" ;;
    custom)
      ask_required REMOTE_ENDPOINT "Endpoint URL (https://...)" "$d_endpoint"
      ask_required REMOTE_REGION "Region" "${D_region:-us-east-1}"
      ;;
  esac
  ask_required REMOTE_BUCKET "Bucket name (turn on versioning or Object Lock for it, see the README)" "$D_bucket"
  ask_required REMOTE_PREFIX "Path prefix inside the bucket" "$D_prefix"
  if [[ -z ${SOFTSYNK_ANSWER_S3_ACCESS_KEY_ID+x} && $D_has_s3_keys == true && $PRESET == "$D_preset" ]] &&
    confirm "Keep the stored S3 access keys?" Y; then
    return 0
  fi
  ask_required S3_ACCESS_KEY_ID "S3 access key id"
  ui_password S3_SECRET_ACCESS_KEY "S3 secret access key"
}

show_password_once() {
  {
    printf '\n%s\n' "================ REPOSITORY PASSWORD (shown once) ================"
    printf '  %s\n\n' "$REPO_PASSWORD"
    printf '%s\n' "Every backup is encrypted with it. Without it NOTHING can be restored, from this" \
      "server or from the bucket. Store it in your password manager now, outside this server." \
      "=================================================================="
  } >&2
}

ask_password() {
  REPO_PASSWORD=""
  [[ $D_has_password != true ]] || return 0
  section "Repository password"
  local mode last4 typed="" n again=""
  if [[ -n ${SOFTSYNK_ANSWER_REPO_PASSWORD+x} ]] || ! ui_interactive; then
    ui_password REPO_PASSWORD "Repository password"
  else
    choose mode "There is no repository password yet." 1 \
      "generate - create a strong random password for me" "enter - I will type my own"
    if [[ $mode == 1 ]]; then
      REPO_PASSWORD=$("$SYNK" genpass)
      if [[ $DRY_RUN == 1 ]]; then
        info "[dry run] a random password would be generated and shown once."
      else
        show_password_once
        last4=${REPO_PASSWORD: -4}
        for n in 1 2 3; do
          ui_password typed "Type the LAST 4 characters of the password to confirm you stored it"
          [[ $typed != "$last4" ]] || break
          warn "That does not match (attempt $n of 3)."
          typed=""
        done
        [[ -n $typed ]] || die "The password was not confirmed; nothing was configured."
        info "Tip: clear your terminal scrollback now."
      fi
    else
      while :; do
        ui_password REPO_PASSWORD "Repository password (at least 12 characters)"
        ui_password again "Repeat the password"
        [[ $REPO_PASSWORD == "$again" && ${#REPO_PASSWORD} -ge 12 ]] && break
        warn "The two entries differ, or the password is shorter than 12 characters."
      done
    fi
  fi
  [[ ${#REPO_PASSWORD} -ge 12 ]] || ui_need "The repository password must be at least 12 characters."
}

ask_all() {
  load_defaults
  section "Where backups are kept on this server"
  ask_required LOCAL_REPO "Local repository path" "$D_local_repo"
  ask_remote
  section "Notifications, retention and exclusions"
  MAIL_TO=${SOFTSYNK_ANSWER_MAIL_TO:-}  # deprecated and ignored (failure mails use synk-notify); --mail-to is still accepted
  unset SOFTSYNK_ANSWER_MAIL_TO
  ask RETENTION_DAILY "Daily snapshots to keep" "$D_daily"
  ask RETENTION_WEEKLY "Weekly snapshots to keep" "$D_weekly"
  ask RETENTION_TWICE_MONTHLY "Twice-monthly snapshots to keep" "$D_twice_monthly"
  ask EXCLUDE_DATABASES "Databases to skip (comma separated, - = none)" "${D_exclude:--}"
  [[ $EXCLUDE_DATABASES != - ]] || EXCLUDE_DATABASES=""
  ask_password
  # an unused pre-supplied secret must not stay in the environment of the commands below
  unset SOFTSYNK_ANSWER_REPO_PASSWORD SOFTSYNK_ANSWER_S3_SECRET_ACCESS_KEY
}

# ---------------------------------------------------------------- configure

# JSON on stdout; the secrets go through the environment of this one python3 process, never argv.
secrets_json() {
  SS_PW=$REPO_PASSWORD SS_KID=$S3_ACCESS_KEY_ID SS_SEC=$S3_SECRET_ACCESS_KEY python3 -c '
import json, os
pairs = (("restic_password", "SS_PW"), ("s3_access_key_id", "SS_KID"), ("s3_secret_access_key", "SS_SEC"))
print(json.dumps({k: os.environ[e] for k, e in pairs if os.environ.get(e)}))'
}

run_configure() {
  local args=(configure --local-repo "$LOCAL_REPO" --mail-to "$MAIL_TO" --retention-daily "$RETENTION_DAILY"
    --retention-weekly "$RETENTION_WEEKLY" --retention-twice-monthly "$RETENTION_TWICE_MONTHLY"
    --exclude-databases "$EXCLUDE_DATABASES" --secrets-stdin)
  if [[ $KEEP_REMOTE == 0 ]]; then
    args+=(--remote-preset "$PRESET")
    if [[ $PRESET != none ]]; then
      [[ -z $REMOTE_ID ]] || args+=(--remote-id "$REMOTE_ID")
      [[ -z $REMOTE_ENDPOINT ]] || args+=(--remote-endpoint "$REMOTE_ENDPOINT")
      [[ -z $REMOTE_REGION ]] || args+=(--remote-region "$REMOTE_REGION")
      args+=(--remote-bucket "$REMOTE_BUCKET" --remote-prefix "$REMOTE_PREFIX")
    fi
  fi
  [[ $DRY_RUN != 1 ]] || args+=(--dry-run)
  section "Validating and configuring"
  secrets_json | "$SYNK" --root "$SOFTSYNK_ROOT" "${args[@]}" ||
    die "Configuration failed; nothing was installed or enabled."
}

# ---------------------------------------------------------------- systemd

synk_exec() { # the stable path through `current` when this script runs from a release
  local cur=$SOFTSYNK_ROOT/current/deployment/backup/synk-backup
  if [[ -x $cur && $(readlink -f -- "$cur") == "$SYNK" ]]; then printf '%s' "$cur"; else printf '%s' "$SYNK"; fi
}

# write_unit NAME EXEC: render systemd/NAME.in; 0 = the installed file changed, 1 = it was already current.
write_unit() {
  local dest=$UNIT_DIR/$1 body
  body=$(sed -e "s|@ROOT@|$SOFTSYNK_ROOT|g" -e "s|@SYNK_BACKUP@|$2|g" "$HERE/systemd/$1.in")
  if [[ -f $dest && $(<"$dest") == "$body" ]]; then return 1; fi
  printf '%s\n' "$body" >"$dest.new"
  chmod 0644 "$dest.new"
  mv -f -- "$dest.new" "$dest"
  info "Wrote $dest"
}

link_command() {
  local link=$BIN_DIR/synk-backup
  if [[ ! -e $link && ! -L $link ]]; then
    ln -s -- "$(synk_exec)" "$link"
    info "Linked $link"
  elif [[ $(readlink -f -- "$link" 2>/dev/null || true) != "$SYNK" ]]; then
    warn "$link already exists and is not this module's; left alone."
  fi
}

install_units() {
  section "Schedule"
  if [[ $DRY_RUN == 1 ]]; then
    info "[dry run] would write softsynk-backup.service and softsynk-backup.timer to $UNIT_DIR, enable the timer and link $BIN_DIR/synk-backup"
    return 0
  fi
  local exe f changed=0
  exe=$(synk_exec)
  install -d -m 0755 -- "$UNIT_DIR"
  for f in softsynk-backup.service softsynk-backup.timer; do
    if write_unit "$f" "$exe"; then changed=1; fi
  done
  # 1.0.0 shipped its own failure-mail unit; failure mails now go through the notify module's softsynk-alert@
  if [[ -f $UNIT_DIR/softsynk-notify@.service ]]; then
    rm -f -- "$UNIT_DIR/softsynk-notify@.service"
    changed=1
  fi
  [[ -f $UNIT_DIR/softsynk-alert@.service ]] ||
    warn "softsynk-alert@.service is not installed, so a failed backup sends no mail. Run: synk-notify configure"
  if command -v systemctl >/dev/null 2>&1; then
    ((changed == 0)) || systemctl daemon-reload
    systemctl enable --now softsynk-backup.timer
    success "Timer enabled: backups run at 00:15, 06:15, 12:15 and 18:15 server time."
  else
    warn "systemd is not available here: the units were written but nothing is scheduled."
  fi
  link_command
}

# ---------------------------------------------------------------- main

main() {
  info "Database backup setup: $MODE$([[ $DRY_RUN == 1 ]] && echo ' (dry run)')"
  preflight
  if [[ $MODE == install ]]; then
    install_packages
  elif [[ $DRY_RUN != 1 && ! -f $UNIT_DIR/softsynk-backup.timer ]]; then
    ui_need "The backup module is not installed yet: run setup-backup.sh without --configure."
  fi
  ask_all
  run_configure
  [[ $MODE != install ]] || install_units
  if [[ $DRY_RUN == 1 ]]; then
    success "Dry run finished: the settings are valid; nothing was changed."
    return 0
  fi
  section "Result"
  "$SYNK" --root "$SOFTSYNK_ROOT" --status || true
  info "The repository password is stored in $(ss_path secrets backup)/restic.pass. Keep a copy somewhere else too."
  if ui_interactive && confirm "Run the first backup now?" Y; then
    "$SYNK" --root "$SOFTSYNK_ROOT" run || warn "The first backup did not finish cleanly: see 'synk-backup --status'."
  else
    info "The first backup runs at the next timer tick; run 'synk-backup run' to start it now."
  fi
  success "Backup setup finished. Try: synk-backup --status"
}

main
