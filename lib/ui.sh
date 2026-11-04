# shellcheck shell=bash
#
# lib/ui.sh - shared UI for the softsynk bash modules: gum prompts with the same signatures as the
# old plain-read helpers, output helpers, non-interactive rules and root-relative paths.
#
# Source it; it installs no traps and runs nothing. Bash 3.2 safe (the Mac self-tests use it).
#
#   ask VAR "Question" [default]        VAR gets the answer
#   confirm "Question" [Y|N]            0 = yes, 1 = no
#   choose VAR "Title" DEFAULT OPT...   VAR gets the 1-based index; DEFAULT is an index
#   ui_password VAR "Question"          hidden input; never a default
#   confirm_danger ACTION TARGET [WHY]  0 = confirmed, 1 = refused; see below
#   info success warn error die section say
#
# A prompt is answered, in this order, by a pre-supplied answer (env SOFTSYNK_ANSWER_<VAR>, which
# the script fills from its --<var-name> options), then by gum when interactive. Non-interactive
# means: no TTY on stdin/stderr, --non-interactive, SOFTSYNK_NONINTERACTIVE=1 or SOFTSYNK_INTERACTIVE=0;
# prompts never wait for input then, use the default only when --accept-defaults /
# SOFTSYNK_ACCEPT_DEFAULTS=1 is set, and otherwise the script exits 64 naming the missing option. Cancelling (Ctrl-C, Esc) exits 130, which is not "No".
# confirm_danger is never satisfied by defaults or a general --yes: it needs the operator to type the
# target, or a specific --confirm-<action>=<target> option (recorded by ui_flag).

if [[ -n ${_SOFTSYNK_UI_LOADED:-} ]]; then return 0; fi
_SOFTSYNK_UI_LOADED=1

readonly UI_MIN_GUM="0.13"
SOFTSYNK_ROOT=${SOFTSYNK_ROOT:-/opt/softsynk-server-management}
SOFTSYNK_NONINTERACTIVE=${SOFTSYNK_NONINTERACTIVE:-0}
SOFTSYNK_ACCEPT_DEFAULTS=${SOFTSYNK_ACCEPT_DEFAULTS:-0}
export SOFTSYNK_ROOT SOFTSYNK_NONINTERACTIVE SOFTSYNK_ACCEPT_DEFAULTS
SOFTSYNK_CONFIRMED=""   # newline-separated "action=target" from --confirm-<action>=<target>
export UI_CANCELLED=0   # set to 1 just before exit 130 so an EXIT trap can say "cancelled"
_UI_LOGDIR_DONE=0
_UI_ANS=""

if [[ -t 2 ]]; then
  C_INFO=$'\e[34m' C_OK=$'\e[32m' C_WARN=$'\e[33m' C_ERR=$'\e[31m' C_OFF=$'\e[0m'
else
  C_INFO="" C_OK="" C_WARN="" C_ERR="" C_OFF=""
fi

# ---------------------------------------------------------------- output

# Callers never pass secrets to these functions: with LOG_FILE set (and DRY_RUN not 1) every line is
# appended to the session log.
_log() {
  local level=$1 color=$2
  shift 2
  printf '%s[%s]%s %s\n' "$color" "$level" "$C_OFF" "$*" >&2
  if [[ -n ${LOG_FILE:-} && ${DRY_RUN:-0} != 1 ]]; then
    if ((_UI_LOGDIR_DONE == 0)); then
      mkdir -p -- "${LOG_FILE%/*}" 2>/dev/null || true
      _UI_LOGDIR_DONE=1
    fi
    printf '%s [%s] %s\n' "$(date '+%F %T')" "$level" "$*" >>"$LOG_FILE" 2>/dev/null || true
  fi
}
info() { _log INFO "$C_INFO" "$@"; }
success() { _log SUCCESS "$C_OK" "$@"; }
warn() { _log WARN "$C_WARN" "$@"; }
error() { _log ERROR "$C_ERR" "$@"; }
die() {
  error "$@"
  exit 1
}
section() { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_OFF" >&2; }
say() { printf '    %s\n' "$@" >&2; }

# ---------------------------------------------------------------- paths

# ss_path KIND [MODULE]: etc secrets state log tmp lock data under SOFTSYNK_ROOT.
ss_path() {
  case $1 in
    etc | secrets | state | log | tmp | lock | data) ;;
    *) die "ss_path: unknown kind '$1'" ;;
  esac
  if [[ -n ${2-} ]]; then
    printf '%s/%s/%s\n' "$SOFTSYNK_ROOT" "$1" "$2"
  else
    printf '%s/%s\n' "$SOFTSYNK_ROOT" "$1"
  fi
}

# ss_module_dirs MODULE: create the module's own directories only (secrets and tmp are 0700).
ss_module_dirs() {
  local k
  for k in etc state log; do install -d -m 0755 -- "$(ss_path "$k" "$1")"; done
  for k in secrets tmp; do install -d -m 0700 -- "$(ss_path "$k" "$1")"; done
  install -d -m 0755 -- "$(ss_path lock)"
}

# ---------------------------------------------------------------- modes and flags

# ui_flag ARG: 0 if ARG is a shared UI option (recorded), 1 otherwise. Use it in the option loop.
ui_flag() {
  case $1 in
    --non-interactive) SOFTSYNK_NONINTERACTIVE=1 ;;
    --accept-defaults) SOFTSYNK_ACCEPT_DEFAULTS=1 ;;
    --confirm-?*=?*) SOFTSYNK_CONFIRMED="$SOFTSYNK_CONFIRMED${1#--confirm-}"$'\n' ;;
    *) return 1 ;;
  esac
}

ui_interactive() {
  [[ ${SOFTSYNK_INTERACTIVE:-1} != 0 && $SOFTSYNK_NONINTERACTIVE != 1 && -t 0 && -t 2 ]]
}

# ui_opt_name VAR: the option a script should offer for VAR (REMOTE_BUCKET -> --remote-bucket).
ui_opt_name() { printf -- '--%s' "$(printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-')"; }

# Exit 64 (bad request): a prompt cannot be answered without a person.
ui_need() {
  error "$1"
  exit 64
}

ui_cancel() {
  printf '\n' >&2
  warn "Cancelled."
  UI_CANCELLED=1
  exit 130
}

# Pre-supplied answer: sets _UI_ANS from SOFTSYNK_ANSWER_<VAR> and unsets it, so a retry loop cannot
# reuse a rejected value and secrets do not stay in the environment of child processes.
_ui_take() {
  local __n="SOFTSYNK_ANSWER_$1" __v
  __v=${!__n-__ui_unset__}
  if [[ $__v == __ui_unset__ ]]; then return 1; fi
  _UI_ANS=$__v
  unset "$__n"
}

# ---------------------------------------------------------------- gum

# ui_gum_version: print the installed gum's version (empty if none).
ui_gum_version() {
  command -v gum >/dev/null 2>&1 || return 0
  gum --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1
}

# ui_version_ge A B: dotted versions, compared on major.minor.
ui_version_ge() {
  local a=$1 b=$2 amaj amin bmaj bmin
  amaj=${a%%.*}
  amin=${a#*.}
  amin=${amin%%.*}
  bmaj=${b%%.*}
  bmin=${b#*.}
  bmin=${bmin%%.*}
  ((10#$amaj > 10#$bmaj || (10#$amaj == 10#$bmaj && 10#$amin >= 10#$bmin)))
}

ui_have_gum() {
  local v
  v=$(ui_gum_version)
  [[ -n $v ]] && ui_version_ge "$v" "$UI_MIN_GUM"
}

_ui_require_gum() {
  ui_have_gum && return 0
  die "gum $UI_MIN_GUM or newer is needed for interactive prompts. Run the module's setup script as root (it installs gum), or: sudo apt-get install -y gum. For scripted use pass --non-interactive."
}

# ui_install_gum: install gum with apt. Only setup-*.sh and bootstrap.sh may call it; a usable gum is
# never replaced. Returns non-zero (with a message) when gum cannot be made available.
ui_install_gum() {
  local top=${BASH_SOURCE[${#BASH_SOURCE[@]} - 1]##*/}
  case $top in
    setup-*.sh | bootstrap.sh) ;;
    *)
      error "ui_install_gum may only be called from setup-*.sh or bootstrap.sh (called from $top)."
      return 2
      ;;
  esac
  ui_have_gum && return 0
  if [[ $EUID -ne 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
    error "gum $UI_MIN_GUM or newer is missing. Install it as root: sudo apt-get install -y gum"
    return 1
  fi
  info "Installing gum with apt-get"
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y gum; then
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y gum || true
  fi
  if ! ui_have_gum; then
    error "gum $UI_MIN_GUM or newer is still not available after apt-get. Install it manually."
    return 1
  fi
}

# _ui_gum VAR gum-args...: run gum, capture stdout into VAR. Esc / Ctrl-C (exit 1 / 130) cancel.
_ui_gum() {
  local __v=$1 __o __rc=0
  shift
  __o=$(gum "$@") || __rc=$?
  case $__rc in
    0) printf -v "$__v" '%s' "$__o" ;;
    1 | 130) ui_cancel ;;
    *) die "gum failed (exit $__rc)." ;;
  esac
}

# ---------------------------------------------------------------- prompts

# ask VAR "Question" [default]
ask() {
  local __var=$1 __q=$2 __has=0 __def="" __ans
  if (($# >= 3)); then
    __has=1
    __def=$3
  fi
  if _ui_take "$__var"; then
    __ans=$_UI_ANS
  elif ui_interactive; then
    _ui_require_gum
    _ui_gum __ans input --header "$__q" --value "$__def"
  elif ((__has)) && [[ $SOFTSYNK_ACCEPT_DEFAULTS == 1 ]]; then
    __ans=$__def
  elif ((__has)); then
    ui_need "Non-interactive: \"$__q\" has a default; pass $(ui_opt_name "$__var") VALUE (or SOFTSYNK_ANSWER_$__var=VALUE) or --accept-defaults."
  else
    ui_need "Non-interactive: \"$__q\" has no default; pass $(ui_opt_name "$__var") VALUE (or SOFTSYNK_ANSWER_$__var=VALUE)."
  fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

# confirm "Question" [Y|N]  -> 0 for yes
confirm() {
  local def=${2:-N} rc=0
  if ! ui_interactive; then
    if [[ $SOFTSYNK_ACCEPT_DEFAULTS == 1 ]]; then
      [[ $def == Y ]]
      return
    fi
    ui_need "Non-interactive: \"$1\" needs an answer; pass --accept-defaults to take the default ($def)."
  fi
  _ui_require_gum
  if [[ $def == Y ]]; then
    gum confirm --default=true "$1" || rc=$?
  else
    gum confirm --default=false "$1" || rc=$?
  fi
  case $rc in
    0) return 0 ;;
    1) return 1 ;;
    130) ui_cancel ;;
    *) die "gum failed (exit $rc)." ;;
  esac
}

# choose VAR "Title" DEFAULT_INDEX OPTION...  -> VAR gets the 1-based index.
# A pre-supplied answer is an index or the first word of an option (case-insensitive).
choose() {
  local __var=$1 __title=$2 __def=$3 __i=1 __o __ans="" __pick="" __n
  shift 3
  __n=$#
  if [[ ! $__def =~ ^[0-9]+$ ]] || ((10#$__def < 1 || 10#$__def > __n)); then
    die "choose: bad default '$__def' for \"$__title\"."
  fi
  __def=$((10#$__def))
  if _ui_take "$__var"; then
    __ans=0
    if [[ $_UI_ANS =~ ^[0-9]+$ ]] && ((10#$_UI_ANS >= 1 && 10#$_UI_ANS <= __n)); then
      __ans=$((10#$_UI_ANS))
    else
      for __o in "$@"; do
        if [[ $(printf '%s' "${__o%% *}" | tr '[:upper:]' '[:lower:]') == "$(printf '%s' "$_UI_ANS" | tr '[:upper:]' '[:lower:]')" ]]; then
          __ans=$__i
          break
        fi
        __i=$((__i + 1))
      done
    fi
    ((__ans > 0)) || ui_need "Invalid value for $(ui_opt_name "$__var") (\"$__title\"): use 1-$__n or the option's first word."
    printf -v "$__var" '%s' "$__ans"
    return 0
  fi
  if ! ui_interactive; then
    if [[ $SOFTSYNK_ACCEPT_DEFAULTS == 1 ]]; then
      printf -v "$__var" '%s' "$__def"
      return 0
    fi
    ui_need "Non-interactive: \"$__title\" needs an answer; pass $(ui_opt_name "$__var") VALUE (or SOFTSYNK_ANSWER_$__var=VALUE) or --accept-defaults."
  fi
  _ui_require_gum
  for __o in "$@"; do
    if ((__i == __def)); then __pick=$__o; fi
    __i=$((__i + 1))
  done
  _ui_gum __ans choose --header "$__title" --selected "$__pick" "$@"
  __i=1
  for __o in "$@"; do
    if [[ $__o == "$__ans" ]]; then
      printf -v "$__var" '%s' "$__i"
      return 0
    fi
    __i=$((__i + 1))
  done
  die "choose: gum returned an unknown option."
}

# ui_password VAR "Question": hidden input. Non-interactively only SOFTSYNK_ANSWER_<VAR> works;
# defaults never apply and secrets are not accepted as command-line options.
ui_password() {
  local __var=$1 __q=$2 __ans
  if _ui_take "$__var"; then
    printf -v "$__var" '%s' "$_UI_ANS"
    return 0
  fi
  if ! ui_interactive; then
    ui_need "Non-interactive: \"$__q\" is a secret; set SOFTSYNK_ANSWER_$__var in the environment (never as an option)."
  fi
  _ui_require_gum
  _ui_gum __ans input --password --header "$__q"
  printf -v "$__var" '%s' "$__ans"
}

# confirm_danger ACTION TARGET [WHY]: for destructive steps. 0 = confirmed, 1 = refused (typed text
# differed; "yes" does not count), exit 130 on cancel, exit 64 when non-interactive without the
# specific --confirm-ACTION=TARGET option. Defaults and --accept-defaults never satisfy it.
confirm_danger() {
  local action=$1 target=$2 why=${3-} typed
  if printf '%s\n' "$SOFTSYNK_CONFIRMED" | grep -Fxq -- "$action=$target"; then return 0; fi
  if ! ui_interactive; then
    ui_need "Non-interactive: destructive step '$action' on '$target' needs --confirm-$action=$target."
  fi
  _ui_require_gum
  if [[ -n $why ]]; then warn "$why"; fi
  _ui_gum typed input --header "Type $target to confirm (anything else cancels this step)" --placeholder "$target"
  if [[ $typed == "$target" ]]; then return 0; fi
  warn "Not confirmed; this step was skipped."
  return 1
}
