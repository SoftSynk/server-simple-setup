#!/usr/bin/env bash
#
# setup-redis.sh - interactive, idempotent Redis provisioning for Ubuntu/Debian with systemd.
#
# First run: installs Redis (after confirmation) and sets up one shared instance or a
# dual CACHE + QUEUE layout. Later runs: show the current state and offer only the
# actions that apply. Every change is shown as a plan, confirmed, backed up, validated,
# applied at runtime where Redis allows it, and verified.
#
# Usage:  sudo ./setup-redis.sh [--dry-run] [--root DIR] [--non-interactive] [--accept-defaults]
#         ./setup-redis.sh --status-json | --help | --self-test
#
set -Eeuo pipefail

SELF=$(readlink -f -- "${BASH_SOURCE[0]}")
HERE=${SELF%/*}
# shellcheck source=../../lib/ui.sh
source "$HERE/../../lib/ui.sh"

readonly SCRIPT_VERSION="1.0.0"
readonly ETC_DIR=/etc/redis
readonly STOCK_CONF=/etc/redis/redis.conf
readonly DATA_ROOT=/var/lib/redis
readonly LOG_DIR=/var/log/redis
# LOG_FILE and LOCK_FILE live under SOFTSYNK_ROOT; main sets them once --root is known.
readonly SYSCTL_FILE=/etc/sysctl.d/90-redis.conf
readonly KEYRING=/usr/share/keyrings/redis-archive-keyring.gpg
readonly APT_LIST=/etc/apt/sources.list.d/redis.list
readonly MARK_BEGIN="# >>> setup-redis.sh managed block"
readonly MARK_END="# <<< setup-redis.sh managed block"
readonly RDB_QUEUE_SAVE="30 10 60 1"                 # snapshot after 30 s with >=10 writes, or 60 s with >=1
readonly RDB_DEFAULT_SAVE="3600 1 300 100 60 10000"  # Redis default; kept next to AOF as a backup snapshot
readonly MIB=1048576

STAMP=$(date +%Y%m%d-%H%M%S)
readonly STAMP
DRY_RUN=${SOFTSYNK_DRY_RUN:-0}
WORK=""              # private temp dir (root only)
VALIDATE_DIR=""      # temp dir for the throwaway validation server (owned by redis)
VALIDATOR_PID=""
APPLIED=0
UNCHANGED=0
FAILED=0
HEALTH="NOT RUN"
BACKUPS=()
LAST_BACKUP=""       # path written by the latest backup() call
CLAIMED_PORTS=" "
OS_NAME=""
RAM_MB=0
CPUS=0
REDIS_VER=""
PEND_K=()            # desired settings for the instance being configured (keys)
PEND_V=()            # ... and their values

# ---------------------------------------------------------------- cleanup and signals

cleanup() {
  if [[ -n $VALIDATOR_PID ]]; then kill "$VALIDATOR_PID" 2>/dev/null || true; fi
  if [[ -n $VALIDATE_DIR && -d $VALIDATE_DIR ]]; then rm -rf -- "$VALIDATE_DIR"; fi
  if [[ -n $WORK && -d $WORK ]]; then rm -rf -- "$WORK"; fi
}
on_signal() {
  printf '\n' >&2
  warn "Interrupted. Config files are replaced atomically, so none is half-written."
  list_backups
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM
# Report only failures of the main shell; failures inside $(...) are handled by their callers.
trap '[[ $BASH_SUBSHELL -gt 0 ]] || error "Unexpected failure at line $LINENO: $BASH_COMMAND"' ERR

# problem MSG: a preflight failure. A dry run changes nothing, so it only warns and goes on.
problem() {
  if ((DRY_RUN)); then warn "$1 (ignored in a dry run)"; else die "$1"; fi
}

# ---------------------------------------------------------------- pure helpers (bash 3.2 safe, see --self-test)

# to_bytes VALUE: Redis memory units -> bytes. Same rules as redis.conf: k/m/g decimal, kb/mb/gb binary.
to_bytes() {
  local v n
  v=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  [[ $v =~ ^([0-9]{1,12})(b|k|kb|m|mb|g|gb)?$ ]] || return 1
  n=$((10#${BASH_REMATCH[1]}))
  case ${BASH_REMATCH[2]} in
    '' | b) echo "$n" ;;
    k) echo $((n * 1000)) ;;
    kb) echo $((n * 1024)) ;;
    m) echo $((n * 1000000)) ;;
    mb) echo $((n * MIB)) ;;
    g) echo $((n * 1000000000)) ;;
    gb) echo $((n * 1073741824)) ;;
  esac
}

# human BYTES -> "512 MB", "1.5 GB", "unlimited"
human() {
  awk -v b="$1" 'BEGIN {
    if (b == 0) { print "unlimited"; exit }
    if (b >= 1073741824) { g = b / 1073741824; fmt = (g == int(g)) ? "%d GB\n" : "%.1f GB\n"; printf fmt, g }
    else printf "%d MB\n", b / 1048576 }'
}

# bytes_to_conf BYTES -> "512mb" when it is a whole number of MiB, else plain bytes
bytes_to_conf() {
  if (($1 > 0 && $1 % MIB == 0)); then echo "$(($1 / MIB))mb"; else echo "$1"; fi
}

# recommend_mb ROLE RAM_MB: cache 25% of RAM, queue 12.5%, shared 25%, rounded to 128 MB.
# Dual worst case: 37.5% used, ~50% with fork copy-on-write while the queue saves,
# which leaves about half of RAM for the OS, PHP-FPM and the database.
recommend_mb() {
  local mb
  if [[ $1 == queue ]]; then mb=$(($2 / 8)); else mb=$(($2 / 4)); fi
  mb=$(((mb + 64) / 128 * 128))
  if ((mb < 128)); then mb=128; fi
  echo "$mb"
}

# conf_get FILE KEY: effective value of KEY in a redis.conf (last line wins, quotes stripped).
# For "save", rules after the last `save ""` are joined; an absent save means the Redis default.
conf_get() {
  [[ -r $1 ]] || return 0
  awk -v k="$2" -v dflt="$RDB_DEFAULT_SAVE" '
    { line = $0; sub(/^[ \t]+/, "", line) }
    line == "" || line ~ /^#/ { next }
    {
      split(line, a, /[ \t]+/)
      if (tolower(a[1]) != k) next
      v = line; sub(/^[^ \t]+[ \t]*/, "", v); found = 1
      if (k == "save") { if (v == "\"\"" || v == "" || v == "\047\047") sv = ""; else { gsub(/"/, "", v); sv = (sv == "" ? v : sv " " v) } }
      else last = v
    }
    END {
      if (k == "save") v = found ? sv : dflt; else v = last
      gsub(/^"|"$/, "", v); print v
    }' "$1"
}

# strip_block FILE: FILE without the managed block
strip_block() {
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" 'index($0, b) == 1 { s = 1; next } index($0, e) == 1 { s = 0; next } !s' "$1"
}

# block_ok FILE: markers are balanced (0 or 1 block), so stripping cannot eat real settings
block_ok() {
  local nb ne
  nb=$(grep -cF "$MARK_BEGIN" "$1" || true)
  ne=$(grep -cF "$MARK_END" "$1" || true)
  [[ $nb == "$ne" && $nb -le 1 ]]
}

pend_reset() {
  PEND_K=()
  PEND_V=()
}
pend_set() {
  local i
  for i in ${PEND_K[@]+"${!PEND_K[@]}"}; do
    if [[ ${PEND_K[i]} == "$1" ]]; then
      PEND_V[i]=$2
      return 0
    fi
  done
  PEND_K+=("$1")
  PEND_V+=("$2")
}
pend_get() {
  local i
  for i in ${PEND_K[@]+"${!PEND_K[@]}"}; do
    if [[ ${PEND_K[i]} == "$1" ]]; then
      printf '%s' "${PEND_V[i]}"
      return 0
    fi
  done
  return 1
}

# load_block FILE: read the managed block of FILE into PEND_K/PEND_V
load_block() {
  local line k v cur
  pend_reset
  [[ -r $1 ]] || return 0
  while IFS= read -r line; do
    if [[ -z $line || $line == \#* ]]; then continue; fi
    k=${line%%[[:space:]]*}
    v=${line#"$k"}
    v=${v#"${v%%[![:space:]]*}"}
    v=${v#\"}
    v=${v%\"}
    if [[ $k == save ]]; then
      if [[ -z $v ]]; then
        pend_set save ""
      else
        cur=$(pend_get save || true)
        pend_set save "${cur:+$cur }$v"
      fi
    elif [[ $k == maxmemory ]]; then
      # Stored as "256mb" in the file, compared as bytes everywhere else (CONFIG GET returns bytes).
      pend_set maxmemory "$(to_bytes "$v" || echo "$v")"
    else
      pend_set "$k" "$v"
    fi
  done < <(awk -v b="$MARK_BEGIN" -v e="$MARK_END" 'index($0, b) == 1 { s = 1; next } index($0, e) == 1 { s = 0 } s' "$1")
}

# write_block ROLE: print the managed block for PEND_K/PEND_V.
# It goes at the end of the file because later directives override earlier ones;
# `save ""` first clears any save rules defined above it.
write_block() {
  local i j k v pairs
  printf '%s (rerun setup-redis.sh instead of editing by hand)\n' "$MARK_BEGIN"
  printf '# role=%s\n' "$1"
  for i in ${PEND_K[@]+"${!PEND_K[@]}"}; do
    k=${PEND_K[i]}
    v=${PEND_V[i]}
    case $k in
      save)
        echo 'save ""'
        pairs=()
        read -r -a pairs <<<"$v" || true
        for ((j = 0; j + 1 < ${#pairs[@]}; j += 2)); do echo "save ${pairs[j]} ${pairs[j + 1]}"; done
        ;;
      maxmemory) echo "maxmemory $(bytes_to_conf "$v")" ;;
      *) if [[ -z $v ]]; then echo "$k \"\""; else echo "$k $v"; fi ;;
    esac
  done
  printf '%s\n' "$MARK_END"
}

is_local_bind() {
  local t
  [[ -n $1 ]] || return 1
  for t in $1; do
    t=${t#-}
    case $t in 127.* | ::1 | localhost) ;; *) return 1 ;; esac
  done
  return 0
}

# ver_ge X.Y: installed Redis is at least X.Y
ver_ge() {
  local a b x y
  IFS=. read -r a b _ <<<"$REDIS_VER."
  IFS=. read -r x y <<<"$1"
  ((10#${a:-0} > x || (10#${a:-0} == x && 10#${b:-0} >= y)))
}
# Localhost binding. The "-" (optional address, no failure without IPv6) needs Redis 7.
local_bind() { if ver_ge 7.0; then echo "127.0.0.1 -::1"; else echo "127.0.0.1 ::1"; fi; }

# ---------------------------------------------------------------- instances
# Instance "default" is the packaged redis-server.service with /etc/redis/redis.conf.
# Any other instance NAME is redis-server@NAME.service with /etc/redis/redis-NAME.conf.

inst_conf() { case $1 in default) echo "$STOCK_CONF" ;; *) echo "$ETC_DIR/redis-$1.conf" ;; esac; }
inst_unit() { case $1 in default) echo redis-server.service ;; *) echo "redis-server@$1.service" ;; esac; }
inst_port() {
  local p
  p=$(conf_get "$(inst_conf "$1")" port)
  echo "${p:-6379}"
}
inst_role() { sed -n 's/^# role=\([a-z]*\)$/\1/p' "$(inst_conf "$1")" 2>/dev/null | tail -n 1; }

list_instances() {
  local f n
  if [[ -f $STOCK_CONF ]]; then echo default; fi
  for f in "$ETC_DIR"/redis-*.conf; do
    [[ -f $f ]] || continue
    n=${f#"$ETC_DIR"/redis-}
    n=${n%.conf}
    if [[ $n =~ ^[a-z0-9_-]+$ ]]; then echo "$n"; fi
  done
}

unit_state() { systemctl is-active "$1" 2>/dev/null || true; }
unit_enabled() { systemctl is-enabled "$1" 2>/dev/null || true; }

# rcli NAME ARGS...: redis-cli against an instance. The password comes from its config file
# and travels in the environment (REDISCLI_AUTH), never on the command line or in logs.
rcli() {
  local conf port pw
  conf=$(inst_conf "$1")
  shift
  port=$(conf_get "$conf" port)
  pw=${RCLI_PW-$(conf_get "$conf" requirepass)} # RCLI_PW: old password while a change is applied
  if [[ -n $pw ]]; then
    REDISCLI_AUTH=$pw timeout 5 redis-cli -h 127.0.0.1 -p "${port:-6379}" "$@"
  else
    timeout 5 redis-cli -h 127.0.0.1 -p "${port:-6379}" "$@"
  fi
}
is_up() { [[ $(rcli "$1" PING 2>/dev/null) == PONG ]]; }
# Read helpers print nothing (and succeed) when the instance is down, so callers stay safe under set -e.
live_get() { rcli "$1" CONFIG GET "$2" 2>/dev/null | sed -n 2p || true; }
info_field() { rcli "$1" INFO "$2" 2>/dev/null | tr -d '\r' | sed -n "s/^$3://p" || true; }

# live_set NAME KEY VALUE: CONFIG SET through stdin so values (passwords) never show in `ps`.
live_set() {
  local out
  out=$(printf 'CONFIG SET %s "%s"\n' "$2" "$3" | rcli "$1" 2>/dev/null) || true
  [[ $out == OK ]]
}

key_count() {
  rcli "$1" INFO keyspace 2>/dev/null | tr -d '\r' |
    sed -n 's/^db[0-9]*:keys=\([0-9]*\).*/\1/p' | awk '{ s += $1 } END { print s + 0 }' || true
}

# current NAME KEY: running value if the instance answers, else the value in its config file
current() {
  local v
  if is_up "$1"; then v=$(live_get "$1" "$2"); else v=$(conf_get "$(inst_conf "$1")" "$2"); fi
  if [[ $2 == maxmemory && -n $v && ! $v =~ ^[0-9]+$ ]]; then v=$(to_bytes "$v" || echo "$v"); fi
  if [[ $2 == maxmemory && -z $v ]]; then v=0; fi
  printf '%s' "$v"
}

persistence_desc() {
  local aof save
  aof=$(current "$1" appendonly)
  save=$(current "$1" save)
  if [[ $aof == yes && -n $save ]]; then
    echo "AOF + RDB"
  elif [[ $aof == yes ]]; then
    echo "AOF"
  elif [[ -n $save ]]; then
    echo "RDB ($save)"
  else
    echo "None"
  fi
}

guess_role() {
  local save aof policy
  save=$(current "$1" save)
  aof=$(current "$1" appendonly)
  policy=$(current "$1" maxmemory-policy)
  if [[ -z $save && $aof != yes ]] || [[ -n $policy && $policy != noeviction ]]; then echo cache; else echo queue; fi
}

# port_owner PORT -> "redis:NAME", "proc:NAME", or nothing when free
port_owner() {
  local n proc
  for n in $(list_instances); do
    if [[ $(inst_port "$n") == "$1" ]]; then
      echo "redis:$n"
      return 0
    fi
  done
  proc=$(ss -ltnpH "sport = :$1" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | sed -n 1p || true)
  if [[ -n $proc ]]; then
    echo "proc:$proc"
  elif [[ -n $(ss -ltnH "sport = :$1" 2>/dev/null || true) ]]; then
    echo "proc:unknown"
  fi
}

# ---------------------------------------------------------------- safe file changes

# run CMD...: run a changing command, or only print it with --dry-run. Never pass secrets.
run() {
  if ((DRY_RUN)); then
    info "[dry-run] would run: $*"
    return 0
  fi
  "$@"
}

# put_file SRC DEST MODE USER:GROUP: atomic replace (write a temp file next to DEST, then mv)
put_file() {
  if ((DRY_RUN)); then
    info "[dry-run] would write $2"
    return 0
  fi
  install -m "$3" -o "${4%%:*}" -g "${4##*:}" "$1" "$2.tmp-$STAMP"
  mv -f "$2.tmp-$STAMP" "$2"
}

backup() {
  local b="$1.bak-$STAMP" i=1
  LAST_BACKUP=""
  [[ -e $1 ]] || return 0
  while [[ -e $b ]]; do # second change to the same file in one run
    i=$((i + 1))
    b="$1.bak-$STAMP-$i"
  done
  LAST_BACKUP=$b
  if ((DRY_RUN)); then
    info "[dry-run] would back up $1"
    return 0
  fi
  cp -a "$1" "$b"
  BACKUPS+=("$1 -> $b")
  info "Backup: $b"
}

list_backups() {
  local b
  for b in ${BACKUPS[@]+"${BACKUPS[@]}"}; do say "Backup: $b"; done
}

# validate_conf FILE: parse FILE with a throwaway redis-server (as user redis, unix socket only,
# temp data dir, no persistence) to catch bad directives before a real instance sees them.
validate_conf() {
  local f=$1 ok=1 _ pw
  VALIDATE_DIR=$(mktemp -d /tmp/redis-validate.XXXXXX)
  # dir, logfile and pidfile are checked or created while parsing; point them at the temp dir.
  sed -E 's/^[[:space:]]*(dir|logfile|pidfile|unixsocket)[[:space:]].*/# (validation) &/' "$f" >"$VALIDATE_DIR/test.conf"
  chown -R redis:redis "$VALIDATE_DIR"
  chmod 700 "$VALIDATE_DIR"
  chmod 600 "$VALIDATE_DIR/test.conf"
  pw=$(conf_get "$f" requirepass)
  runuser -u redis -- redis-server "$VALIDATE_DIR/test.conf" --port 0 \
    --unixsocket "$VALIDATE_DIR/s.sock" --unixsocketperm 700 --dir "$VALIDATE_DIR" \
    --logfile "" --pidfile "$VALIDATE_DIR/p.pid" --daemonize no --supervised no \
    --appendonly no --save "" >"$VALIDATE_DIR/out" 2>&1 &
  VALIDATOR_PID=$!
  for _ in $(seq 40); do
    kill -0 "$VALIDATOR_PID" 2>/dev/null || break
    # An empty REDISCLI_AUTH still sends AUTH "", which fails, so set it only with a password.
    if [[ $(if [[ -n $pw ]]; then export REDISCLI_AUTH=$pw; fi; timeout 2 redis-cli -s "$VALIDATE_DIR/s.sock" PING 2>/dev/null || true) == PONG ]]; then
      ok=0
      break
    fi
    sleep 0.25
  done
  kill "$VALIDATOR_PID" 2>/dev/null || true
  wait "$VALIDATOR_PID" 2>/dev/null || true
  VALIDATOR_PID=""
  if ((ok == 0)); then
    success "Config validated with a throwaway redis-server."
  else
    error "Config validation failed. redis-server said:"
    grep -iv 'requirepass' "$VALIDATE_DIR/out" | tail -n 15 | sed 's/^/      /' >&2 || true
  fi
  rm -rf -- "$VALIDATE_DIR"
  VALIDATE_DIR=""
  return "$ok"
}

wait_up() {
  local _
  for _ in $(seq 40); do
    if is_up "$1"; then return 0; fi
    sleep 0.5
  done
  return 1
}

# Wait until a runtime switch to AOF has finished its first rewrite. Restarting before that
# would make Redis load an empty AOF instead of the existing data.
wait_aof() {
  local _ inf
  info "Waiting for the first AOF rewrite to finish..."
  for _ in $(seq 600); do
    inf=$(rcli "$1" INFO persistence 2>/dev/null | tr -d '\r' || true)
    if grep -q '^aof_enabled:1' <<<"$inf" && grep -q '^aof_rewrite_in_progress:0' <<<"$inf" &&
      grep -q '^aof_rewrite_scheduled:0' <<<"$inf"; then
      if grep -q '^aof_last_bgrewrite_status:ok' <<<"$inf"; then
        success "AOF is active."
        return 0
      fi
      error "The AOF rewrite failed (see the Redis log)."
      return 1
    fi
    sleep 0.5
  done
  warn "AOF rewrite still running after 5 minutes."
  return 1
}

# After switching AOF off, take a fresh RDB snapshot so a crash cannot fall back to an old one.
bgsave_wait() {
  local _ before tried=0
  info "Writing a fresh RDB snapshot..."
  before=$(info_field "$1" persistence rdb_last_save_time)
  for _ in $(seq 240); do
    if [[ $(info_field "$1" persistence rdb_bgsave_in_progress) == 0 ]]; then
      if [[ $(info_field "$1" persistence rdb_last_save_time) != "$before" ]]; then
        if [[ $(info_field "$1" persistence rdb_last_bgsave_status) == ok ]]; then
          success "RDB snapshot written."
        else
          warn "Last RDB snapshot failed. Check the Redis log."
        fi
        return 0
      fi
      if ((tried)) && [[ $(info_field "$1" persistence rdb_last_bgsave_status) == err ]]; then
        warn "BGSAVE failed. Check the Redis log and run BGSAVE by hand before restarting."
        return 0
      fi
      rcli "$1" BGSAVE >/dev/null 2>&1 || true
      tried=1
    fi
    sleep 1
  done
  warn "No fresh RDB snapshot after 4 minutes. Run BGSAVE by hand before restarting."
}

restart_or_rollback() { # NAME BACKUP
  local name=$1 b=$2 unit conf
  unit=$(inst_unit "$name")
  conf=$(inst_conf "$name")
  if systemctl restart "$unit" && wait_up "$name"; then
    success "$unit restarted."
    return 0
  fi
  error "$unit is not healthy after the restart. Last log lines:"
  journalctl -u "$unit" -n 15 --no-pager >&2 || true
  FAILED=$((FAILED + 1))
  if [[ -f $b ]]; then
    warn "Rolling back to $b."
    cp -a "$b" "$conf"
    systemctl restart "$unit" || true
    if wait_up "$name"; then warn "Rolled back: $unit runs with its previous config."; else error "Rollback restart failed too. Check: journalctl -u $unit"; fi
  fi
}

# ---------------------------------------------------------------- systemd

ensure_template_unit() {
  local d bin
  for d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
    if [[ -f $d/redis-server@.service ]]; then return 0; fi
  done
  bin=$(command -v redis-server)
  info "No redis-server@.service template found. Installing one at /etc/systemd/system (same hardening as the packaged unit)."
  cat >"$WORK/redis-server@.service" <<EOF
[Unit]
Description=Advanced key-value store (instance %i)
After=network.target
Documentation=https://redis.io/docs/

[Service]
Type=notify
ExecStart=$bin /etc/redis/redis-%i.conf --supervised systemd --daemonize no
PIDFile=/run/redis-%i/redis-server.pid
TimeoutStopSec=0
Restart=always
RestartSec=2
User=redis
Group=redis
RuntimeDirectory=redis-%i
RuntimeDirectoryMode=2755
UMask=007
LimitNOFILE=65535
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=-/var/lib/redis -/var/log/redis -/run/redis-%i
NoNewPrivileges=true
CapabilityBoundingSet=
MemoryDenyWriteExecute=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictRealtime=true
RestrictNamespaces=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
EOF
  put_file "$WORK/redis-server@.service" /etc/systemd/system/redis-server@.service 644 root:root
  run systemctl daemon-reload
}

step_systemd() {
  local unit restart nofile user dir
  unit=$(inst_unit "$1")
  dir="/etc/systemd/system/$unit.d"
  restart=$(systemctl show -p Restart --value "$unit" 2>/dev/null || true)
  nofile=$(systemctl show -p LimitNOFILE --value "$unit" 2>/dev/null || true)
  user=$(systemctl show -p User --value "$unit" 2>/dev/null || true)
  printf '\n' >&2
  info "systemd reliability for $unit: Restart=${restart:-?} LimitNOFILE=${nofile:-?} User=${user:-root}"
  if [[ -z $user || $user == root ]]; then
    warn "$unit runs as root. The packaged units run as 'redis'; check for local overrides (systemctl cat $unit)."
  fi
  if [[ $restart =~ ^(always|on-failure|on-abnormal)$ && $nofile =~ ^[0-9]+$ ]] && ((nofile >= 10032)); then
    success "Already restarts on failure and allows enough open files for maxclients 10000."
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  say "Proposed drop-in $dir/setup-redis.conf: Restart=always, RestartSec=2, LimitNOFILE=65535." \
    "Restart= applies after a daemon-reload; LimitNOFILE applies on the next restart. Low risk."
  confirm "Add this systemd drop-in?" Y || return 0
  printf '[Service]\nRestart=always\nRestartSec=2\nLimitNOFILE=65535\n' >"$WORK/dropin.conf"
  run install -d -m 755 "$dir"
  backup "$dir/setup-redis.conf"
  put_file "$WORK/dropin.conf" "$dir/setup-redis.conf" 644 root:root
  run systemctl daemon-reload
  APPLIED=$((APPLIED + 1))
  warn "LimitNOFILE takes effect the next time $unit restarts."
}

# ---------------------------------------------------------------- configuration steps

validate_mem() { # validate_mem NAME BYTES IS_NEW
  local b=$2 ram_b=$((RAM_MB * MIB)) used
  if ((b == 0)); then
    warn "0 means unlimited: Redis can grow until the server runs out of memory and the kernel OOM killer stops Redis or another process."
    confirm "Use unlimited?" N
    return
  fi
  if ((b < 32 * MIB)); then
    error "Too small. Use at least 32mb."
    return 1
  fi
  if ((b > ram_b * 9 / 10)); then
    error "More than 90% of RAM ($(human $((ram_b * 9 / 10))))."
    return 1
  fi
  if (($3 == 0)) && is_up "$1"; then
    used=$(info_field "$1" memory used_memory)
    if ((b < ${used:-0} * 12 / 10)); then
      error "Below current usage plus 20% ($(human "${used:-0}") used now). Writes would fail or keys would be evicted at once."
      return 1
    fi
  fi
  if ((b > ram_b / 2)); then
    warn "More than half of RAM for one instance. The OS, PHP and the database must still fit."
    confirm "Continue with $(human "$b")?" N || return 1
  fi
  return 0
}

step_memory() { # NAME ROLE IS_NEW
  local name=$1 role=$2 new=$3 rec cur pick input bytes opts
  rec=$(($(recommend_mb "$role" "$RAM_MB") * MIB))
  printf '\n' >&2
  info "Memory limit (maxmemory)"
  say "RAM: $RAM_MB MB. Rule used here: cache 25% of RAM, queue 12.5%, single shared instance 25%." \
    "That keeps at least half of RAM for the OS, PHP-FPM and the database, plus room for fork" \
    "copy-on-write during background saves and for fragmentation (RSS is often 1.2-1.5x used memory)." \
    "Recommended for $role: $(human "$rec"). Applied at runtime, no restart."
  opts=("Accept recommended ($(human "$rec"))" "Custom value")
  if ((new == 0)); then
    cur=$(current "$name" maxmemory)
    opts+=("Keep current ($(human "$cur"))")
  fi
  local def=1
  if ((new == 0)); then def=3; fi # Enter keeps what an existing instance has
  while true; do
    choose pick "maxmemory for $role" "$def" "${opts[@]}"
    case $pick in
      1) bytes=$rec ;;
      2)
        ask input "maxmemory (for example 512mb, 2gb; 0 = unlimited)"
        bytes=$(to_bytes "$input") || {
          warn "Not a valid size: $input"
          continue
        }
        ;;
      3) return 0 ;;
    esac
    if validate_mem "$name" "$bytes" "$new"; then break; fi
  done
  pend_set maxmemory "$bytes"
}

step_eviction() { # NAME ROLE IS_NEW
  local name=$1 role=$2 new=$3 pick policy mem opts
  local policies=(noeviction allkeys-lru allkeys-lfu volatile-lru volatile-ttl)
  printf '\n' >&2
  info "Eviction policy (maxmemory-policy)"
  say "noeviction: never delete keys. At the memory limit every write fails with an OOM error." \
    "allkeys-lru / allkeys-lfu: delete least recently / least frequently used keys. Fine for a pure cache, never for queues." \
    "volatile-lru / volatile-ttl: delete only keys that have an expiry." \
    "Applied at runtime, no restart."
  if [[ $role == cache ]]; then
    say "For a cache-only instance allkeys-lru is the usual choice. The default here is noeviction: safe, but cache writes fail when full."
  fi
  opts=("${policies[@]}")
  local def=1
  if ((new == 0)); then
    opts+=("Keep current ($(current "$name" maxmemory-policy))")
    def=${#opts[@]}
  fi
  choose pick "maxmemory-policy for $role" "$def" "${opts[@]}"
  if ((pick > ${#policies[@]})); then return 0; fi
  policy=${policies[pick - 1]}
  if [[ $role != cache && $policy != noeviction ]]; then
    warn "Eviction on a $role instance can silently delete queued jobs, sessions or locks."
    confirm_danger eviction "$name" || policy=noeviction
  fi
  if [[ $policy == noeviction ]]; then
    mem=$(pend_get maxmemory || current "$name" maxmemory)
    if ((${mem:-0} > 0)); then
      warn "With noeviction, once $(human "$mem") is used every write fails (in Laravel, dispatch() and cache writes throw). Watch used_memory."
    fi
  fi
  pend_set maxmemory-policy "$policy"
}

step_persistence() { # NAME ROLE IS_NEW
  local name=$1 role=$2 new=$3 pick def=2 opts
  printf '\n' >&2
  info "Persistence"
  say "1 None: nothing on disk; data is gone after a restart. Right for a disposable cache." \
    "2 RDB snapshots (save $RDB_QUEUE_SAVE): a snapshot 30 s after 10+ writes, or 60 s after 1+ write." \
    "  A crash can lose up to ~60 s of writes; a clean restart loses nothing. Low disk I/O." \
    "3 AOF (appendfsync everysec) plus hourly RDB: every write is logged and synced once per second." \
    "  A crash loses up to ~1 s. More disk writes." \
    "Applied at runtime, no restart. Switching to AOF first rewrites the log in the background."
  if [[ $role == cache ]]; then def=1; fi
  opts=("None" "RDB snapshots ($RDB_QUEUE_SAVE)" "AOF + RDB")
  if ((new == 0)); then
    opts+=("Keep current ($(persistence_desc "$name"))")
    def=4
  fi
  while true; do
    choose pick "Persistence for $role" "$def" "${opts[@]}"
    case $pick in
      1)
        if [[ $role != cache ]]; then
          warn "No persistence on a $role instance: every queued job is lost when Redis restarts."
          confirm_danger no-persistence "$name" || continue
        fi
        pend_set save ""
        pend_set appendonly no
        ;;
      2)
        pend_set save "$RDB_QUEUE_SAVE"
        pend_set appendonly no
        ;;
      3)
        pend_set save "$RDB_DEFAULT_SAVE"
        pend_set appendonly yes
        pend_set appendfsync everysec
        ;;
      4) ;;
    esac
    return 0
  done
}

step_security() { # NAME IS_NEW
  local name=$1 new=$2 pick pw pw2 cur_pw opts
  printf '\n' >&2
  info "Network exposure"
  if ((new == 0)); then say "Current: bind $(current "$name" bind), protected-mode $(current "$name" protected-mode)"; fi
  say "Recommended: bind $(local_bind) and protected-mode yes, so only processes on this server can connect." \
    "Risk: clients on other servers lose access. Applied at runtime on Redis 7+, needs a restart on older versions."
  local def=Y
  # On an instance that already listens on other addresses, changing that cuts off remote clients.
  if ((new == 0)) && ! is_local_bind "$(current "$name" bind)"; then def=N; fi
  if confirm "Restrict Redis to localhost?" "$def"; then
    pend_set bind "$(local_bind)"
    pend_set protected-mode yes
  else
    warn "Keeping the current binding. Make sure a firewall blocks this port from the internet."
  fi

  printf '\n' >&2
  info "Password (requirepass)"
  cur_pw=$(pend_get requirepass || conf_get "$(inst_conf "$name")" requirepass)
  say "Localhost binding already blocks remote access. A password also protects against other local users and SSRF." \
    "Apps then need REDIS_PASSWORD. Open connections stay authenticated; new ones without the password fail." \
    "Stored only in the config file (mode 640, readable by root and redis); never printed or logged. Runtime, no restart."
  opts=("Keep as is (currently $([[ -n $cur_pw ]] && echo set || echo 'not set'))" "Generate a strong random password" "Enter a password")
  if [[ -n $cur_pw ]]; then opts+=("Remove the password"); fi
  choose pick "Password" 1 "${opts[@]}"
  case $pick in
    2)
      pw=$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n') # 192 random bits as 48 hex chars
      pend_set requirepass "$pw"
      info "Generated. Read it later with: sudo grep '^requirepass' $(inst_conf "$name")"
      ;;
    3)
      while true; do
        ui_password pw "Password (16+ chars, no spaces, quotes or backslashes)"
        ui_password pw2 "Repeat"
        if [[ $pw != "$pw2" ]]; then
          warn "The two entries differ."
        elif ((${#pw} < 16)) || [[ $pw == *[[:space:]\"\'\\]* ]]; then
          warn "Use 16+ characters without spaces, quotes or backslashes."
        else
          break
        fi
      done
      pend_set requirepass "$pw"
      ;;
    4)
      warn "Without a password any local process can read and change all data."
      if confirm "Remove the password?" N; then pend_set requirepass ""; fi
      ;;
  esac
}

# step_optional "Title" Y|N "Explanation" KEY VALUE [KEY VALUE ...]
step_optional() {
  local title=$1 def=$2 text=$3 list="" i
  shift 3
  local kv=("$@")
  for ((i = 0; i + 1 < ${#kv[@]}; i += 2)); do list+="${kv[i]} ${kv[i + 1]}; "; done
  printf '\n' >&2
  info "$title"
  say "$text" "Settings: ${list%; }"
  confirm "Include these settings?" "$def" || return 0
  for ((i = 0; i + 1 < ${#kv[@]}; i += 2)); do pend_set "${kv[i]}" "${kv[i + 1]}"; done
}

show_val() { # show_val KEY VALUE: for the plan table, with passwords masked
  case $1 in
    requirepass) if [[ -n $2 ]]; then echo "(set)"; else echo "(not set)"; fi ;;
    maxmemory) if [[ $2 =~ ^[0-9]+$ ]]; then human "$2"; else echo "${2:-(unset)}"; fi ;;
    save) if [[ -z $2 ]]; then echo "(disabled)"; else echo "$2"; fi ;;
    *) echo "${2:-(unset)}" ;;
  esac
}

base_conf() { # base_conf NAME PORT: fixed settings of a new instance, outside the managed block
  cat <<EOF
# Redis instance "$1" created by setup-redis.sh on $(date '+%F %T').
# systemd unit: redis-server@$1.service
port $2
supervised systemd
daemonize no
pidfile /run/redis-$1/redis-server.pid
logfile $LOG_DIR/redis-server-$1.log
dir $DATA_ROOT/$1
dbfilename dump.rdb
EOF
}

# retire_rdb NAME: persistence was just turned off. Redis no longer saves on shutdown, so the old
# dump.rdb would be loaded again on the next start and serve stale data. Offer to rename it.
retire_rdb() {
  local d f
  d=$(current "$1" dir)
  f=$(current "$1" dbfilename)
  [[ -n $d && -n $f && -f $d/$f ]] || return 0
  warn "$d/$f is an old snapshot. With persistence off, the next restart would load it and serve stale data."
  if confirm "Rename it to $f.disabled-$STAMP so a restart starts empty?" Y; then
    run mv -f "$d/$f" "$d/$f.disabled-$STAMP"
    APPLIED=$((APPLIED + 1))
  fi
}

# apply_instance NAME ROLE IS_NEW PORT: show the plan, then validate, back up, apply and verify.
apply_instance() {
  local name=$1 role=$2 new=$3 port=$4 conf unit tmp i k want have owner
  local changed=() order=() same=0 restart=0 up=0 file_changed=1 count to_rdb=0 aof_bad=0 bak old_persist
  conf=$(inst_conf "$name")
  unit=$(inst_unit "$name")
  tmp="$WORK/$name.conf"

  if ((new)); then
    base_conf "$name" "$port" >"$tmp"
  else
    if ! block_ok "$conf"; then
      error "$conf has unbalanced managed-block markers. Fix it by hand, then rerun."
      FAILED=$((FAILED + 1))
      return 0
    fi
    strip_block "$conf" >"$tmp"
    if [[ -s $tmp && -n $(tail -c 1 "$tmp") ]]; then echo >>"$tmp"; fi
    if is_up "$name"; then up=1; fi
  fi
  write_block "$role" >>"$tmp"
  if ((new == 0)) && cmp -s "$tmp" "$conf"; then file_changed=0; fi

  section "Plan for $unit (port $port, role $role)"
  for i in ${PEND_K[@]+"${!PEND_K[@]}"}; do
    k=${PEND_K[i]}
    want=${PEND_V[i]}
    if ((new)); then
      have="(new)"
    else
      have=$(current "$name" "$k")
      if [[ $have == "$want" ]]; then
        same=$((same + 1))
        continue
      fi
      have=$(show_val "$k" "$have")
    fi
    changed+=("$i")
    say "$(printf '%-27s %s -> %s' "$k" "$have" "$(show_val "$k" "$want")")"
  done
  count=${#changed[@]}
  say "Unchanged: $same setting(s)."
  if ((count == 0 && file_changed == 0)); then
    success "$unit already matches the desired state."
    UNCHANGED=$((UNCHANGED + same))
    return 0
  fi
  if ((count == 0)); then
    say "Running values already match; only the config file is updated so they survive a restart."
    count=1
  fi
  if ((new)); then say "Creates $conf, data dir $DATA_ROOT/$name and enables $unit at boot."; fi
  # Changing an existing instance needs an explicit "y"; Enter alone does nothing.
  if ! confirm "Apply this plan to $unit?" "$( ((new)) && echo Y || echo N)"; then
    warn "Skipped $unit. Nothing changed."
    return 0
  fi

  validate_conf "$tmp" || {
    FAILED=$((FAILED + 1))
    return 0
  }
  if ((new)); then
    ensure_template_unit
    run install -d -o redis -g redis -m 750 "$DATA_ROOT/$name"
  fi
  if ((DRY_RUN)); then
    info "[dry-run] $unit: $count change(s) would be applied."
    APPLIED=$((APPLIED + count))
    UNCHANGED=$((UNCHANGED + same))
    return 0
  fi

  if ((new)); then
    put_file "$tmp" "$conf" 640 redis:redis
    if systemctl enable --now "$unit" && wait_up "$name"; then
      success "$unit created and running on port $port."
      APPLIED=$((APPLIED + count + 1))
    else
      error "$unit did not start. Last log lines:"
      journalctl -u "$unit" -n 15 --no-pager >&2 || true
      systemctl disable --now "$unit" >/dev/null 2>&1 || true
      mv -f "$conf" "$conf.failed-$STAMP"
      warn "Rolled back: $unit disabled; its config was moved to $conf.failed-$STAMP for inspection."
      FAILED=$((FAILED + 1))
    fi
    return 0
  fi

  backup "$conf"
  bak=$LAST_BACKUP
  owner=$(stat -c '%U:%G' "$conf")
  old_persist=$(persistence_desc "$name")
  # The file goes first, so an interrupted run never leaves a new password only in memory.
  # Until the runtime password changes, rcli keeps using the old one.
  RCLI_PW=$(conf_get "$conf" requirepass)
  put_file "$tmp" "$conf" 640 "$owner"
  success "Saved $conf (backup: $bak)."
  if ((up)); then
    # Runtime next (no restart). The password goes last.
    for i in ${changed[@]+"${changed[@]}"}; do
      if [[ ${PEND_K[i]} != requirepass ]]; then order+=("$i"); fi
    done
    for i in ${changed[@]+"${changed[@]}"}; do
      if [[ ${PEND_K[i]} == requirepass ]]; then order+=("$i"); fi
    done
    for i in ${order[@]+"${order[@]}"}; do
      k=${PEND_K[i]}
      want=${PEND_V[i]}
      if live_set "$name" "$k" "$want"; then
        say "Set at runtime: $k"
        if [[ $k == appendonly && $want == yes ]] && ! wait_aof "$name"; then aof_bad=1; fi
        if [[ $k == appendonly && $want == no ]]; then to_rdb=1; fi
      else
        warn "$k cannot be changed at runtime here; it needs a restart."
        restart=1
      fi
    done
  else
    restart=1
  fi
  unset RCLI_PW
  APPLIED=$((APPLIED + count))
  UNCHANGED=$((UNCHANGED + same))
  if ((to_rdb)) && [[ -n $(pend_get save || true) ]]; then bgsave_wait "$name"; fi
  if [[ $old_persist != None && $(pend_get save || echo keep) == "" && $(pend_get appendonly || true) == no ]]; then
    retire_rdb "$name"
  fi
  if ((aof_bad && restart)); then
    error "The first AOF rewrite did not complete, so a restart now could load an incomplete AOF and lose data."
    warn "Not restarting. Check INFO persistence and the Redis log, then restart $unit by hand."
    FAILED=$((FAILED + 1))
    restart=0
  fi

  if [[ $(unit_enabled "$unit") != enabled ]] && confirm "$unit is not enabled at boot. Enable it?" Y; then
    systemctl enable "$unit"
  fi
  if ((restart)); then
    if ((up)); then
      say "A restart disconnects clients briefly (usually 1-5 s; longer while a big dataset loads)."
    else
      say "$unit is not running, so the new settings take effect when it starts."
    fi
    if confirm "Restart $unit now?" Y; then
      restart_or_rollback "$name" "$bak"
    else
      warn "Restart postponed. Some settings apply only after $unit restarts."
    fi
  fi
}

configure_instance() { # NAME ROLE IS_NEW PORT
  local name=$1 role=$2 new=$3 port=$4
  section "Configure $(inst_unit "$name") as $role (port $port)"
  pend_reset
  # Start from the existing managed block, so "keep current" keeps earlier choices.
  if ((new == 0)); then load_block "$(inst_conf "$name")"; fi
  step_memory "$name" "$role" "$new"
  step_eviction "$name" "$role" "$new"
  step_persistence "$name" "$role" "$new"
  step_security "$name" "$new"
  step_optional "Client limits and timeouts" Y \
    "timeout 0 keeps idle connections open (queue workers, persistent PHP connections); tcp-keepalive 300 detects dead peers. maxclients stays at 10000. Runtime, no restart. Low risk: Redis defaults made explicit." \
    timeout 0 tcp-keepalive 300
  step_optional "Background freeing (lazyfree)" Y \
    "Frees deleted, expired and evicted keys in a background thread, so large deletes do not block Redis. Runtime, no restart. Low risk: memory comes back slightly later." \
    lazyfree-lazy-eviction yes lazyfree-lazy-expire yes lazyfree-lazy-server-del yes lazyfree-lazy-user-del yes
  step_optional "Logging and monitoring" Y \
    "loglevel notice; SLOWLOG keeps the last 128 commands slower than 10 ms; the latency monitor records events over 100 ms (LATENCY LATEST). Runtime, no restart. Negligible overhead." \
    loglevel notice slowlog-log-slower-than 10000 slowlog-max-len 128 latency-monitor-threshold 100
  apply_instance "$name" "$role" "$new" "$port"
  if [[ -e $(inst_conf "$name") ]]; then step_systemd "$name"; fi
}

# ---------------------------------------------------------------- flows

provision_role() { # ROLE DEFAULT_PORT
  local role=$1 port=$2 owner n pick keys
  while true; do
    ask port "Port for the $role instance" "$port"
    if ! [[ $port =~ ^[0-9]+$ ]] || ((port < 1024 || port > 65535)); then
      warn "Use a port from 1024 to 65535."
      port=$2
      continue
    fi
    if [[ $CLAIMED_PORTS == *" $port "* ]]; then
      warn "Port $port is already taken by another instance in this run."
      port=$((port + 1))
      continue
    fi
    owner=$(port_owner "$port")
    case $owner in
      "")
        if [[ -e $(inst_conf "$role") ]]; then
          warn "$(inst_conf "$role") already exists for port $(inst_port "$role"). Pick that port to adopt it."
          port=$(inst_port "$role")
          continue
        fi
        CLAIMED_PORTS+="$port "
        configure_instance "$role" "$role" 1 "$port"
        return 0
        ;;
      redis:*)
        n=${owner#redis:}
        keys=$(key_count "$n")
        info "Port $port belongs to $(inst_unit "$n") (role: $(inst_role "$n" | grep . || echo unmanaged), keys: ${keys:-unknown})."
        if [[ ${keys:-0} != 0 && $role != shared && $(guess_role "$n") != "$role" ]]; then
          warn "It looks like a $(guess_role "$n") instance. Adopting it as $role proposes $role defaults (you still approve each one)."
        fi
        choose pick "What should happen?" 1 "Adopt $(inst_unit "$n") as the $role instance (keeps its data)" "Use a different port"
        if ((pick == 1)); then
          CLAIMED_PORTS+="$port "
          configure_instance "$n" "$role" 0 "$port"
          return 0
        fi
        port=$((port + 1))
        ;;
      proc:*)
        warn "Port $port is used by ${owner#proc:}. Choose another port."
        port=$((port + 1))
        ;;
    esac
  done
}

setup_flow() {
  local pick
  section "Set up Redis instances"
  say "Single: one Redis for cache, queues and sessions. Simple, but a growing cache competes with queue data." \
    "Dual (recommended): a cache instance and a queue instance, each with its own config, systemd unit," \
    "memory limit and persistence, so a full cache never blocks jobs and cache data is never written to disk."
  choose pick "Mode" 2 "Single instance" "Dual instance (recommended)"
  CLAIMED_PORTS=" "
  if ((pick == 1)); then
    provision_role shared 6379
  else
    provision_role cache 6379
    provision_role queue 6380
  fi
  offer_overcommit
}

manage_instance() {
  local n=$1 role guess pick def=2
  role=$(inst_role "$n")
  if [[ -z $role ]]; then
    guess=$(guess_role "$n")
    if [[ $guess == cache ]]; then def=1; fi
    info "$(inst_unit "$n") is not managed by this script yet. Its settings look like a $guess instance."
    say "The role only picks defaults for the next questions. Nothing changes until you approve the plan."
    choose pick "Role for $(inst_unit "$n")" "$def" "cache" "queue" "shared (cache and queue in one)"
    case $pick in 1) role=cache ;; 2) role=queue ;; 3) role=shared ;; esac
  fi
  if ! is_up "$n"; then warn "$(inst_unit "$n") is not answering, so current values come from its config file."; fi
  configure_instance "$n" "$role" 0 "$(inst_port "$n")"
}

offer_overcommit() {
  local cur n persistent=0
  cur=$(sysctl -n vm.overcommit_memory 2>/dev/null || echo unknown)
  if [[ $cur == 1 ]]; then
    UNCHANGED=$((UNCHANGED + 1))
    return 0
  fi
  for n in $(list_instances); do
    if [[ $(persistence_desc "$n") != None ]]; then persistent=1; fi
  done
  if ((persistent == 0)); then return 0; fi
  section "Kernel setting: vm.overcommit_memory (currently $cur)"
  say "Redis forks to write snapshots and rewrite the AOF. With the current setting the kernel can refuse that" \
    "fork under memory pressure, and background saves fail (Redis logs a warning about this at startup)." \
    "Redis documents vm.overcommit_memory=1 as the fix. This is system-wide: it changes how the kernel grants" \
    "memory to every process. Writes $SYSCTL_FILE and applies at once; no restart."
  if ! confirm "Set vm.overcommit_memory=1?" Y; then
    warn "Skipped. Background saves can fail under memory pressure."
    return 0
  fi
  printf '# Added by setup-redis.sh: lets Redis fork for background saves.\nvm.overcommit_memory = 1\n' >"$WORK/sysctl.conf"
  backup "$SYSCTL_FILE"
  put_file "$WORK/sysctl.conf" "$SYSCTL_FILE" 644 root:root
  run sysctl -q -p "$SYSCTL_FILE"
  APPLIED=$((APPLIED + 1))
}

install_redis() {
  local codename
  codename=$(
    # shellcheck source=/dev/null
    . /etc/os-release
    echo "${VERSION_CODENAME:-}"
  )
  section "Install Redis"
  say "Redis is not installed." \
    "Plan: add the official Redis APT repository (packages.redis.io, signed key at $KEYRING), then install 'redis'." \
    "If packages.redis.io has no build for '${codename:-unknown}', you can install the distribution's redis-server instead." \
    "The package creates the 'redis' user, $STOCK_CONF and redis-server.service on port 6379 (localhost only)."
  confirm "Install Redis now?" Y || die "Redis is required. Nothing was changed."
  if ((DRY_RUN)); then
    info "[dry-run] would install Redis. Stopping here, because every later step needs it."
    exit 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  umask 022 # packages must install with normal permissions; the script's own files use 077
  apt-get update -q
  apt-get install -y -q ca-certificates curl gpg
  if [[ -n $codename ]] && curl -fsS --max-time 20 -o /dev/null "https://packages.redis.io/deb/dists/$codename/Release"; then
    if [[ ! -s $KEYRING ]]; then
      curl -fsSL --max-time 20 https://packages.redis.io/gpg | gpg --dearmor >"$WORK/redis.gpg"
      install -m 644 "$WORK/redis.gpg" "$KEYRING"
    fi
    if [[ ! -e $APT_LIST ]]; then
      echo "deb [signed-by=$KEYRING] https://packages.redis.io/deb $codename main" >"$WORK/redis.list"
      install -m 644 "$WORK/redis.list" "$APT_LIST"
    fi
    apt-get update -q
    apt-get install -y -q redis
  else
    warn "packages.redis.io has no packages for '${codename:-unknown}'."
    confirm "Install the distribution's redis-server package instead?" Y || die "Nothing was installed."
    apt-get install -y -q redis-server
  fi
  umask 077
  command -v redis-server >/dev/null || die "Installation finished, but redis-server was not found."
  success "Installed $(redis-server --version | sed -n 's/.*v=\([0-9.]*\).*/Redis \1/p')."
}

# ---------------------------------------------------------------- discovery, health, summary

detect_platform() {
  local id="" like="" c
  OS_NAME=unknown
  if [[ -r /etc/os-release ]]; then
    # shellcheck source=/dev/null
    id=$(. /etc/os-release && echo "${ID:-}")
    # shellcheck source=/dev/null
    like=$(. /etc/os-release && echo "${ID_LIKE:-}")
    # shellcheck source=/dev/null
    OS_NAME=$(. /etc/os-release && echo "${PRETTY_NAME:-$id}")
  else
    problem "Cannot read /etc/os-release. Only Ubuntu and Debian are supported."
  fi
  case " $id $like " in
    *" debian "* | *" ubuntu "*) ;;
    *) problem "Unsupported OS: $OS_NAME. This script supports Ubuntu and Debian only." ;;
  esac
  [[ -d /run/systemd/system ]] || problem "systemd is not running as init. This script needs systemd."
  for c in systemctl journalctl ss awk sed grep install flock timeout runuser od; do
    command -v "$c" >/dev/null || problem "Missing required command: $c"
  done
  RAM_MB=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo 2>/dev/null || true)
  RAM_MB=${RAM_MB:-0}
  CPUS=$(nproc 2>/dev/null || echo 0)
}

detect_redis() {
  REDIS_VER=""
  if command -v redis-server >/dev/null; then
    REDIS_VER=$(redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9.]*\).*/\1/p')
  fi
  command -v redis-cli >/dev/null || [[ -z $REDIS_VER ]] || die "redis-server is installed but redis-cli is missing (install redis-tools)."
}

thp_mode() { sed -n 's/.*\[\(.*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true; }

show_state() {
  local n unit role ports
  section "Current state"
  say "System:     $OS_NAME, $CPUS vCPU, $RAM_MB MB RAM" "Redis:      ${REDIS_VER:-not installed}"
  for n in $(list_instances); do
    unit=$(inst_unit "$n")
    role=$(inst_role "$n")
    if is_up "$n"; then
      say "$(printf '%-27s port %-5s %-8s role=%-9s maxmemory=%s policy=%s persistence=%s keys=%s' \
        "$unit" "$(inst_port "$n")" "$(unit_state "$unit")" "${role:-unmanaged}" \
        "$(human "$(current "$n" maxmemory)")" "$(live_get "$n" maxmemory-policy)" \
        "$(persistence_desc "$n")" "$(key_count "$n")")"
    else
      say "$(printf '%-27s port %-5s %-8s role=%-9s (not answering)' "$unit" "$(inst_port "$n")" "$(unit_state "$unit")" "${role:-unmanaged}")"
    fi
  done
  ports=$(ss -ltnpH 2>/dev/null | awk '/redis/ { print $4 }' | sort -u | tr '\n' ' ' || true)
  say "Listening:  ${ports:-no Redis ports}" \
    "Kernel:     vm.overcommit_memory=$(sysctl -n vm.overcommit_memory 2>/dev/null || echo '?'), transparent hugepages=$(thp_mode)"
}

health_check() {
  local n unit st en role i k want have bad fails=0 used max bind
  section "Health checks"
  for n in $(list_instances); do
    unit=$(inst_unit "$n")
    st=$(unit_state "$unit")
    en=$(unit_enabled "$unit")
    role=$(inst_role "$n")
    if [[ $st != active ]]; then
      if [[ $en == enabled || -n $role ]]; then
        error "$unit is $st (enabled: $en)."
        fails=$((fails + 1))
      else
        warn "$unit is $st and not enabled; ignored."
      fi
      continue
    fi
    if ! is_up "$n"; then
      error "$unit is active but does not answer PING on port $(inst_port "$n")."
      fails=$((fails + 1))
      continue
    fi
    success "$unit answers PING on port $(inst_port "$n")."
    if [[ -n $role ]]; then
      load_block "$(inst_conf "$n")"
      bad=0
      for i in ${PEND_K[@]+"${!PEND_K[@]}"}; do
        k=${PEND_K[i]}
        want=${PEND_V[i]}
        have=$(live_get "$n" "$k")
        if [[ $have != "$want" ]]; then
          bad=1
          if [[ $k == requirepass ]]; then
            error "$unit: the running password differs from the config file."
          else
            error "$unit: $k is '$have' but the config says '$want' (restart pending?)."
          fi
        fi
      done
      if ((bad)); then fails=$((fails + 1)); else success "$unit: running settings match the managed block."; fi
    fi
    if [[ $(info_field "$n" persistence rdb_last_bgsave_status) != ok ]]; then
      error "$unit: the last RDB save failed (see its log)."
      fails=$((fails + 1))
    fi
    if [[ $(live_get "$n" appendonly) == yes && $(info_field "$n" persistence aof_last_write_status) != ok ]]; then
      error "$unit: AOF writes are failing (disk full or permissions?)."
      fails=$((fails + 1))
    fi
    bind=$(live_get "$n" bind)
    if ! is_local_bind "$bind"; then
      if [[ -z $(conf_get "$(inst_conf "$n")" requirepass) && $(live_get "$n" protected-mode) != yes ]]; then
        error "$unit listens on '$bind' with no password and protected-mode off: open to the network."
        fails=$((fails + 1))
      else
        warn "$unit listens on non-local addresses ($bind). Keep the port firewalled."
      fi
    fi
    used=$(info_field "$n" memory used_memory)
    max=$(live_get "$n" maxmemory)
    if ((${max:-0} > 0 && ${used:-0} > max * 9 / 10)); then
      warn "$unit uses $(human "$used") of $(human "$max") (over 90%)."
    fi
  done
  if [[ $(sysctl -n vm.overcommit_memory 2>/dev/null) != 1 ]]; then
    warn "vm.overcommit_memory is not 1; background saves can fail under memory pressure."
  fi
  if [[ $(thp_mode) == always ]]; then
    warn "Transparent hugepages are 'always'; Redis recommends madvise or never (latency and memory spikes)."
  fi
  if ((fails == 0)); then HEALTH=PASSED; else HEALTH=FAILED; fi
}

print_summary() {
  local n unit role roles=" " mode count=0 title r changes result line="────────────────────────────────"
  for n in $(list_instances); do
    roles+="$(inst_role "$n") "
    count=$((count + 1))
  done
  if [[ $roles == *" cache "* && $roles == *" queue "* ]]; then
    mode="Dual Instance"
  elif [[ $roles == *" shared "* ]]; then
    mode="Single Instance"
  else
    mode="Not managed ($count instance(s))"
  fi
  printf '\n%s\n%s\n' "Redis Setup Summary" "$line"
  printf '%-15s%s\n' "Version:" "${REDIS_VER:-unknown}" "Mode:" "$mode"
  for r in cache queue shared ""; do
    for n in $(list_instances); do
      role=$(inst_role "$n")
      [[ $role == "$r" ]] || continue
      unit=$(inst_unit "$n")
      if [[ -n $role ]]; then title=$(echo "$role" | tr '[:lower:]' '[:upper:]'); else title="$unit (unmanaged)"; fi
      printf '\n%s\n' "$title"
      printf '  %-13s%s\n' "Port:" "$(inst_port "$n")"
      if is_up "$n"; then
        printf '  %-13s%s\n' "Memory:" "$(human "$(current "$n" maxmemory)")" \
          "Eviction:" "$(live_get "$n" maxmemory-policy)" \
          "Persistence:" "$(persistence_desc "$n")" \
          "Status:" "Running"
      else
        printf '  %-13s%s\n' "Status:" "Stopped ($(unit_state "$unit"))"
      fi
    done
  done
  if ((DRY_RUN)); then
    changes="$APPLIED planned (dry run), $UNCHANGED unchanged"
    result="DRY RUN (nothing changed)"
  else
    changes="$APPLIED applied, $UNCHANGED unchanged"
    result=SUCCESS
    if [[ $HEALTH != PASSED ]] || ((FAILED > 0)); then result=FAILED; fi
  fi
  if ((FAILED > 0)); then changes+=", $FAILED failed"; fi
  printf '\n%-15s%s\n' "Health Checks:" "$HEALTH"
  printf '%-15s%s\n' "Changes:" "$changes" "Result:" "$result"
  printf '%s\n' "$line"
  list_backups
  if ((DRY_RUN == 0)); then say "Session log: $LOG_FILE"; fi
  [[ $result != FAILED ]]
}

main_menu() {
  local n labels actions pick roles r
  while true; do
    show_state
    labels=()
    actions=()
    roles=" "
    for n in $(list_instances); do roles+="$(inst_role "$n") "; done
    if [[ $roles != *" shared "* ]] && [[ $roles != *" cache "* || $roles != *" queue "* ]]; then
      labels+=("Set up instances (single or dual)")
      actions+=(setup)
    fi
    for n in $(list_instances); do
      r=$(inst_role "$n")
      labels+=("Review and tune $(inst_unit "$n") (port $(inst_port "$n"), role: ${r:-unmanaged})")
      actions+=("manage:$n")
    done
    if [[ $(sysctl -n vm.overcommit_memory 2>/dev/null) != 1 ]]; then
      labels+=("Set vm.overcommit_memory=1 (reliable background saves)")
      actions+=(overcommit)
    fi
    labels+=("Run health checks" "Finish: verify and show the summary")
    actions+=(health finish)
    choose pick "What would you like to do?" "${#labels[@]}" "${labels[@]}"
    case ${actions[pick - 1]} in
      setup) setup_flow ;;
      manage:*) manage_instance "${actions[pick - 1]#manage:}" ;;
      overcommit) offer_overcommit ;;
      health) health_check ;;
      finish) return 0 ;;
    esac
  done
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

ST_DET="" ST_WARN="" ST_ACT="" ST_FIRST="" ST_HEALTH=healthy ST_INFO=""
st_s() { ST_DET+="${ST_DET:+,}$(json_str "$1"):$(json_str "$2")"; }
st_b() { ST_DET+="${ST_DET:+,}$(json_str "$1"):$([[ $2 == 1 ]] && echo true || echo false)"; }
st_act() { ST_ACT+="${ST_ACT:+,}$(json_str "$1")"; }
st_note() { ST_WARN+="${ST_WARN:+,}$(json_str "$1")"; }
# st_bad LEVEL MSG: record a finding; the worst level (unknown < degraded < unhealthy) wins.
st_bad() {
  st_note "$2"
  [[ -n $ST_FIRST ]] || ST_FIRST=$2
  case $ST_HEALTH:$1 in
    healthy:* | unknown:degraded | unknown:unhealthy | degraded:unhealthy) ST_HEALTH=$1 ;;
  esac
}
st_info() { printf '%s\n' "$ST_INFO" | sed -n "s/^$1://p"; }
# st_emit INSTALL_STATE HEALTH SUMMARY: the one JSON document, on stdout.
st_emit() {
  printf '{"schema":1,"module":"redis","version":%s,"observed_at":"%s","install_state":"%s","health":"%s","summary":%s,"details":{%s},"warnings":[%s],"actions":[%s]}\n' \
    "$(json_str "$SCRIPT_VERSION")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$(json_str "$3")" "$ST_DET" "$ST_WARN" "$ST_ACT"
}

# status_json: read-only snapshot for softsynk-server. It takes no lock, writes no log or temp file,
# needs neither root nor gum, and sends only PING, INFO and CONFIG GET to the local instances (the
# password goes in the environment, as everywhere else, and is never printed). Anything it cannot
# read makes the health "unknown", never a crash. Run as root it sees the most: the config files
# (mode 640, root:redis) hold the ports, roles and passwords.
status_json() {
  set +e +E
  trap - ERR
  local id="" like="" n unit conf role port st en bind max used insts cur cli=1
  local up=0 count=0 managed=0 unreadable=0 state names=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    id=$(. /etc/os-release 2>/dev/null && echo "${ID:-}")
    # shellcheck disable=SC1091
    like=$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-}")
  fi
  case " $id $like " in
    *" debian "* | *" ubuntu "*) ;;
    *)
      st_act "Use this module on Ubuntu or Debian with systemd."
      st_emit not_installed unsupported "Ubuntu or Debian only (found ${id:-unknown})"
      return 0
      ;;
  esac
  REDIS_VER=""
  if command -v redis-server >/dev/null 2>&1; then
    REDIS_VER=$(redis-server --version 2>/dev/null | sed -n 's/.*v=\([0-9.]*\).*/\1/p')
  fi
  insts=$(list_instances)
  if [[ -z $REDIS_VER && -z $insts ]]; then
    st_act "Run setup-redis.sh."
    st_emit not_installed unknown "not installed"
    return 0
  fi
  st_s redis_version "$REDIS_VER"
  if ! command -v redis-cli >/dev/null 2>&1; then
    cli=0
    st_bad unknown "redis-cli is missing, so no instance was probed"
  fi
  if [[ -z $insts ]]; then
    st_bad unknown "Redis is installed, but no instance config was found in $ETC_DIR"
    st_act "Run setup-redis.sh."
  fi

  for n in $insts; do
    count=$((count + 1))
    unit=$(inst_unit "$n")
    conf=$(inst_conf "$n")
    role=""
    if [[ -r $conf ]]; then
      role=$(inst_role "$n")
      [[ -z $role ]] || managed=1
    else
      unreadable=1
      st_bad unknown "cannot read $conf, so $unit is only partly checked (run as root)"
    fi
    port=$(inst_port "$n")
    st=$(unit_state "$unit")
    en=$(unit_enabled "$unit")
    st_s "$n.role" "${role:-unmanaged}"
    st_s "$n.port" "$port"
    st_s "$n.state" "${st:-unknown}"
    st_s "$n.enabled" "${en:-unknown}"
    if [[ -z $st ]]; then
      st_bad unknown "systemd did not answer for $unit"
      continue
    fi
    if [[ $st != active ]]; then
      [[ $en == enabled || -n $role ]] && st_bad unhealthy "$unit is $st (enabled: $en)"
      continue
    fi
    ((cli)) && [[ -r $conf ]] || continue
    if ! is_up "$n"; then
      st_bad unhealthy "$unit is active but does not answer PING on port $port"
      continue
    fi
    up=$((up + 1))
    names+="${names:+, }$n:$port"
    st_b "$n.password_set" "$([[ -n $(conf_get "$conf" requirepass) ]] && echo 1 || echo 0)"
    ST_INFO=$(rcli "$n" INFO 2>/dev/null | tr -d '\r')
    if [[ -z $ST_INFO ]]; then
      st_bad unknown "$unit answered PING but not INFO"
      continue
    fi
    max=$(st_info maxmemory)
    used=$(st_info used_memory)
    st_s "$n.used_memory" "${used:-}"
    st_s "$n.maxmemory" "${max:-}"
    st_s "$n.keys" "$(printf '%s\n' "$ST_INFO" | sed -n 's/^db[0-9]*:keys=\([0-9]*\).*/\1/p' | awk '{ s += $1 } END { print s + 0 }')"
    if [[ $(st_info rdb_last_bgsave_status) != ok ]]; then
      st_bad unhealthy "$unit: the last RDB save failed (see its log)"
    fi
    if [[ $(st_info aof_enabled) == 1 && $(st_info aof_last_write_status) != ok ]]; then
      st_bad unhealthy "$unit: AOF writes are failing (disk full or permissions?)"
    fi
    bind=$(live_get "$n" bind)
    if [[ -n $bind ]] && ! is_local_bind "$bind"; then
      if [[ -z $(conf_get "$conf" requirepass) && $(live_get "$n" protected-mode) != yes ]]; then
        st_bad unhealthy "$unit listens on '$bind' with no password and protected-mode off: open to the network"
      else
        st_note "$unit listens on non-local addresses ($bind). Keep the port firewalled."
      fi
    fi
    if ((${max:-0} > 0 && ${used:-0} > max * 9 / 10)); then
      st_bad degraded "$unit uses over 90% of its maxmemory"
    fi
  done

  cur=$(sysctl -n vm.overcommit_memory 2>/dev/null)
  [[ -z $cur || $cur == 1 ]] || st_note "vm.overcommit_memory is $cur, not 1; background saves can fail under memory pressure."
  [[ $(thp_mode) != always ]] || st_note "Transparent hugepages are 'always'; Redis recommends madvise or never."

  if ((managed)); then
    state=installed
  elif ((unreadable)); then
    state=unknown
  else
    state=adopted
    st_act "Run setup-redis.sh to manage these Redis settings."
  fi
  if [[ $ST_HEALTH == healthy ]]; then
    st_emit "$state" healthy "$up of $count instance(s) answering: $names"
  else
    st_emit "$state" "$ST_HEALTH" "$ST_FIRST"
  fi
}

usage() {
  cat <<EOF
setup-redis.sh $SCRIPT_VERSION - interactive, idempotent Redis setup for Ubuntu/Debian (systemd)

Usage:
  sudo ./setup-redis.sh             interactive setup or maintenance menu
  sudo ./setup-redis.sh --dry-run   walk through every question and show the plan; change nothing
  ./setup-redis.sh --status-json    print the module status as JSON (read-only; root sees the most)
  ./setup-redis.sh --self-test      check the script's internal helpers (no root, no Redis needed)
  ./setup-redis.sh --help           this text

Options:
  --root DIR          management root (default \$SOFTSYNK_ROOT or /opt/softsynk-server-management)
  --non-interactive   never prompt; a question without an answer exits 64
  --accept-defaults   with --non-interactive (or no terminal), take each question's default
  --confirm-no-persistence=NAME, --confirm-eviction=NAME
                      pre-approve the two data-losing choices for instance NAME (never implied by
                      --accept-defaults)

When not interactive, a chosen password comes from SOFTSYNK_ANSWER_pw and SOFTSYNK_ANSWER_pw2 in
the environment, never from an option.

Defaults: dual mode, cache on 6379 and queue on 6380, noeviction, cache without persistence,
queue with RDB snapshots (save $RDB_QUEUE_SAVE), localhost only. See README.md.
Exit codes: 0 ok, 1 failed, 64 missing or invalid input, 130 cancelled.
EOF
}

# ---------------------------------------------------------------- self-test

self_test() {
  local fails=0 t
  t=$(mktemp -d)
  WORK=$t
  check() {
    if [[ $1 != "$2" ]]; then
      printf 'FAIL %s: got [%s], want [%s]\n' "$3" "$1" "$2" >&2
      fails=$((fails + 1))
    fi
  }
  check "$(to_bytes 512mb)" 536870912 "to_bytes mb"
  check "$(to_bytes 2GB)" 2147483648 "to_bytes GB"
  check "$(to_bytes 1g)" 1000000000 "to_bytes g is decimal like redis.conf"
  check "$(to_bytes 08mb)" 8388608 "to_bytes leading zero"
  check "$(to_bytes 0)" 0 "to_bytes zero"
  check "$(to_bytes 12x || echo bad)" bad "to_bytes rejects junk"
  check "$(human 536870912)" "512 MB" "human MB"
  check "$(human 2147483648)" "2 GB" "human GB"
  check "$(human 1610612736)" "1.5 GB" "human fraction"
  check "$(human 0)" "unlimited" "human zero"
  check "$(bytes_to_conf 536870912)" 512mb "bytes_to_conf MiB"
  check "$(bytes_to_conf 1000)" 1000 "bytes_to_conf bytes"
  check "$(recommend_mb cache 1967)" 512 "recommend cache 2 GB box"
  check "$(recommend_mb queue 1967)" 256 "recommend queue 2 GB box"
  check "$(recommend_mb cache 7940)" 2048 "recommend cache 8 GB box"
  check "$(recommend_mb queue 7940)" 1024 "recommend queue 8 GB box"
  check "$(recommend_mb shared 3900)" 1024 "recommend shared 4 GB box"
  check "$(recommend_mb queue 300)" 128 "recommend floor"
  if is_local_bind "127.0.0.1 -::1"; then check ok ok "local bind"; else check bad ok "local bind"; fi
  if is_local_bind "0.0.0.0"; then check bad ok "public bind"; else check ok ok "public bind"; fi
  check "$(json_str 'a"b\c')" '"a\"b\\c"' json1
  check "$(json_str "$(printf 'x\ty\001z')")" '"x\tyz"' json2

  # A stock-like config with an older managed block appended.
  cat >"$t/r.conf" <<'EOF'
# port 1
port 6380
bind 127.0.0.1 -::1
save 3600 1 300 100 60 10000
requirepass "old"
EOF
  check "$(conf_get "$t/r.conf" save)" "3600 1 300 100 60 10000" "conf_get stock save"
  printf 'port 1\n' >"$t/nosave.conf"
  check "$(conf_get "$t/nosave.conf" save)" "$RDB_DEFAULT_SAVE" "conf_get absent save is the default"
  pend_reset
  pend_set maxmemory 536870912
  pend_set maxmemory-policy noeviction
  pend_set save "$RDB_QUEUE_SAVE"
  pend_set requirepass abc123
  pend_set maxmemory 268435456
  write_block queue >>"$t/r.conf"
  check "$(conf_get "$t/r.conf" port)" 6380 "conf_get port ignores comments"
  check "$(conf_get "$t/r.conf" save)" "$RDB_QUEUE_SAVE" "managed save overrides stock save"
  check "$(conf_get "$t/r.conf" maxmemory)" 256mb "pend_set replaces"
  check "$(conf_get "$t/r.conf" requirepass)" abc123 "later requirepass wins"
  check "$(sed -n 's/^# role=//p' "$t/r.conf")" queue "role line"
  if block_ok "$t/r.conf"; then check ok ok "block_ok"; else check bad ok "block_ok"; fi
  load_block "$t/r.conf"
  check "$(pend_get save)" "$RDB_QUEUE_SAVE" "load_block save"
  check "$(pend_get maxmemory)" 268435456 "load_block maxmemory in bytes"
  check "${#PEND_K[@]}" 4 "load_block key count"
  pend_set maxmemory 268435456
  { strip_block "$t/r.conf"; write_block queue; } >"$t/r2.conf"
  if cmp -s "$t/r.conf" "$t/r2.conf"; then check same same "rewrite is idempotent"; else check differs same "rewrite is idempotent"; fi
  pend_reset
  pend_set save ""
  write_block cache >"$t/c.conf"
  load_block "$t/c.conf"
  check "$(pend_get save; echo "|")" "|" "empty save survives a round trip"
  check "$(conf_get "$t/c.conf" save)" "" "save disabled"
  printf '%s\nport 1\n' "$MARK_BEGIN" >"$t/broken.conf"
  if block_ok "$t/broken.conf"; then check accepted rejected "unbalanced markers"; else check rejected rejected "unbalanced markers"; fi

  rm -rf -- "$t"
  WORK=""
  if ((fails)); then
    echo "self-test: $fails failure(s)" >&2
    return 1
  fi
  echo "self-test: all checks passed"
}

# ---------------------------------------------------------------- main

need_value() { (($# >= 2)) && [[ $2 != --* ]] || ui_need "$1 needs a value."; }

main() {
  local mode=run n
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
  LOG_FILE=$(ss_path log redis)/setup-redis.log
  LOCK_FILE=$(ss_path lock)/redis.lock
  readonly LOG_FILE LOCK_FILE
  umask 077
  WORK=$(mktemp -d /tmp/redis-setup.XXXXXX)
  # A dry run changes nothing, so it creates no log or lock under the root and takes no lock.
  if ((DRY_RUN == 0)); then
    install -d -m 0755 -- "${LOG_FILE%/*}" "${LOCK_FILE%/*}"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another setup-redis.sh run is in progress."
  fi

  detect_platform
  # Prompts need gum (a non-interactive run never prompts, so it never needs it).
  if ((DRY_RUN == 0)) && ui_interactive; then ui_install_gum || die "gum is needed for the prompts."; fi
  info "setup-redis.sh $SCRIPT_VERSION$( ((DRY_RUN)) && echo ' (dry run: nothing will be changed)' || true)"
  detect_redis
  if [[ -z $REDIS_VER ]]; then
    install_redis
    detect_redis
  fi
  # 6.0 is the oldest version whose directives (lazyfree-lazy-user-del, ...) this script writes.
  ver_ge 6.0 || die "Redis $REDIS_VER is too old. This script needs Redis 6.0 or newer."
  if [[ -z $(for n in $(list_instances); do inst_role "$n"; done) ]]; then
    show_state
    info "No instance is managed by this script yet."
    if confirm "Run the guided setup now?" Y; then setup_flow; fi
  fi
  main_menu
  health_check
  print_summary
}

main "$@"
