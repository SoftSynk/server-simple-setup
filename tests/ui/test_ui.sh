#!/usr/bin/env bash
# shellcheck disable=SC2016  # test snippets are code strings, evaluated by a child shell
# Tests for lib/ui.sh. Run: bash tests/ui/test_ui.sh
#   A: non-interactive rules (no TTY; no gum needed)
#   B: interactive paths against a stub gum on a pty (exact gum arguments and exit codes)
#   C: the same paths against the real gum, when installed (keystrokes over a pty)
# Works with macOS bash 3.2.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UI=$HERE/../../lib/ui.sh
PTY=$HERE/pty_run.py
TMP=$(mktemp -d "${TMPDIR:-/tmp}/ui-test.XXXXXX")
trap 'rm -rf "${TMP:?}"' EXIT
mkdir -p "$TMP/stub"
unset SOFTSYNK_ROOT SOFTSYNK_NONINTERACTIVE SOFTSYNK_ACCEPT_DEFAULTS SOFTSYNK_INTERACTIVE
PASS=0
FAIL=0

# Stub gum: records its arguments, prints STUB_OUT, exits STUB_RC; reports STUB_VERSION.
cat >"$TMP/stub/gum" <<'EOF'
#!/bin/bash
if [ "${1-}" = "--version" ]; then echo "gum version ${STUB_VERSION:-0.17.0}"; exit 0; fi
echo "$*" >>"$STUB_LOG"
printf '%s' "${STUB_OUT-}"
exit "${STUB_RC:-0}"
EOF
chmod +x "$TMP/stub/gum"

# expect NAME WANT_RC PATTERN  (reads RC and OUT globals; PATTERN is a fixed string or empty)
expect() {
  local name=$1 want=$2 pat=${3-}
  if [[ $RC == "$want" ]] && { [[ -z $pat ]] || printf '%s' "$OUT" | grep -Fq -- "$pat"; }; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %s: rc=%s (want %s) pattern=[%s]\n---\n%s\n---\n' "$name" "$RC" "$want" "$pat" "$OUT"
  fi
}

# ni CODE...: run CODE after sourcing ui.sh with no TTY; stdout+stderr in OUT, exit in RC.
ni() {
  OUT=$(bash -c 'source "$1"; shift; eval "$*"' _ "$UI" "$@" </dev/null 2>&1)
  RC=$?
}

# pty KEYSFILE CODE...: run CODE after sourcing ui.sh on a pty, keys from KEYSFILE.
: >"$TMP/nokeys"
pty() {
  local k=$1
  shift
  python3 "$PTY" "$k" -- bash -c 'source "$1"; shift; eval "$*"' _ "$UI" "$@" >"$TMP/out" 2>&1
  RC=$?
  OUT=$(tr -d '\r' <"$TMP/out")
}
# tty_stub CODE...: pty with the stub gum first on PATH (stub reads STUB_OUT, STUB_RC, STUB_VERSION).
tty_stub() { PATH="$TMP/stub:$PATH" STUB_LOG="$TMP/stub.log" pty "$TMP/nokeys" "$@"; }
# logged NAME PATTERN: the stub gum log must contain PATTERN.
logged() {
  if grep -Fq -- "$2" "$TMP/stub.log"; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); printf 'FAIL %s: [%s] not in gum args: %s\n' "$1" "$2" "$(cat "$TMP/stub.log")"; fi
}

echo "== A: non-interactive"
ni 'ask X "Q"; echo "X=$X"'
expect "ask no default exits 64" 64 "--x"
ni 'ask X "Q" dflt; echo "X=$X"'
expect "ask default needs accept-defaults" 64 "--accept-defaults"
ni 'ui_flag --accept-defaults; ask X "Q" dflt; echo "X=$X"'
expect "ask accept-defaults flag" 0 "X=dflt"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; ask X "Q" dflt; echo "X=$X"'
expect "ask accept-defaults env" 0 "X=dflt"
ni 'SOFTSYNK_ANSWER_X="given value"; ask X "Q" dflt; echo "X=$X"'
expect "ask pre-supplied answer wins" 0 "X=given value"
ni 'SOFTSYNK_ANSWER_X=""; ask X "Q" dflt; echo "X=[$X]"'
expect "ask pre-supplied empty answer is kept" 0 "X=[dflt]"
ni 'SOFTSYNK_ANSWER_REMOTE_BUCKET=b; ask REMOTE_BUCKET "Q"; echo "$REMOTE_BUCKET"; ask REMOTE_BUCKET "Q" 2>&1'
expect "answer is single-use and option name is derived" 64 "--remote-bucket"

ni 'confirm "Go?" Y; echo "rc=$?"'
expect "confirm needs answer" 64 "--accept-defaults"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; confirm "Go?" Y; echo "rc=$?"'
expect "confirm default Y" 0 "rc=0"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; confirm "Go?" N; echo "rc=$?"'
expect "confirm default N" 0 "rc=1"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; confirm "Go?"; echo "rc=$?"'
expect "confirm default is N" 0 "rc=1"

ni 'choose C "Pick" 2 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose needs answer" 64 "--c"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; choose C "Pick" 2 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose accept-defaults gives default index" 0 "C=2"
ni 'SOFTSYNK_ANSWER_C=gamma; choose C "Pick" 2 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose by option word" 0 "C=3"
ni 'SOFTSYNK_ANSWER_C=BETA; choose C "Pick" 1 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose by option word is case-insensitive" 0 "C=2"
ni 'SOFTSYNK_ANSWER_C=1; choose C "Pick" 3 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose by index" 0 "C=1"
ni 'SOFTSYNK_ANSWER_C=9; choose C "Pick" 1 "alpha one" "beta two"; echo "C=$C"'
expect "choose out of range" 64 "Invalid value"
ni 'choose C "Pick" 5 "alpha one" "beta two"'
expect "choose bad default is a programming error" 1 "bad default"

ni 'ui_password P "Secret"'
expect "password needs env answer" 64 "SOFTSYNK_ANSWER_P"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; ui_password P "Secret"'
expect "password ignores accept-defaults" 64 "SOFTSYNK_ANSWER_P"
ni 'SOFTSYNK_ANSWER_P="pa ss"; ui_password P "Secret"; echo "P=$P"; echo "env=${SOFTSYNK_ANSWER_P-gone}"'
expect "password from env answer, then removed from env" 0 "env=gone"

ni 'confirm_danger restore mydb'
expect "danger needs specific confirm" 64 "--confirm-restore=mydb"
ni 'SOFTSYNK_ACCEPT_DEFAULTS=1; confirm_danger restore mydb'
expect "danger ignores accept-defaults" 64 "--confirm-restore=mydb"
ni 'ui_flag --confirm-restore=other; confirm_danger restore mydb'
expect "danger wrong target" 64 ""
ni 'ui_flag --confirm-restore=mydb; confirm_danger restore mydb; echo "rc=$?"'
expect "danger specific confirm" 0 "rc=0"
ni 'ui_flag --confirm-restore=mydb; confirm_danger wipe mydb'
expect "danger wrong action" 64 ""

ni 'ui_flag --non-interactive; echo "ni=$SOFTSYNK_NONINTERACTIVE"; ui_flag --bogus || echo "unknown=$?"'
expect "ui_flag" 0 "unknown=1"
ni 'ss_path state backup; ss_path etc; SOFTSYNK_ROOT=/x ss_path log m'
expect "ss_path default root" 0 "/opt/softsynk-server-management/state/backup"
ni 'ss_path nope'
expect "ss_path unknown kind" 1 "unknown kind"
ni 'r=""; ui_version_ge 0.17.0 0.13 && r=${r}a; ui_version_ge 0.9 0.13 || r=${r}b; ui_version_ge 1.0 0.13 && r=${r}c; ui_version_ge 0.13 0.13 && r=${r}d; echo "r=$r"'
expect "ui_version_ge" 0 "r=abcd"
ni "SOFTSYNK_ROOT=$TMP/r; ss_module_dirs backup; python3 -c 'import os,sys;print(*(oct(os.stat(sys.argv[1]+d).st_mode&0o777) for d in (\"/secrets/backup\",\"/tmp/backup\",\"/etc/backup\",\"/lock\")))' \"\$SOFTSYNK_ROOT\""
expect "ss_module_dirs modes" 0 "0o700 0o700 0o755 0o755"
# ui_install_gum only from setup scripts
printf '%s\n' 'source "$1"; ui_install_gum; echo "rc=$?"' >"$TMP/notsetup.sh"
OUT=$(bash "$TMP/notsetup.sh" "$UI" 2>&1 </dev/null)
RC=$?
expect "ui_install_gum refuses other callers" 0 "rc=2"
printf '%s\n' 'source "$1"; ui_install_gum; echo "rc=$?"' >"$TMP/setup-x.sh"
OUT=$(PATH="$TMP/stub:$PATH" bash "$TMP/setup-x.sh" "$UI" 2>&1 </dev/null)
RC=$?
expect "ui_install_gum is a no-op when gum is usable" 0 "rc=0"
if [[ $EUID -ne 0 ]]; then
  OUT=$(PATH="$TMP/stub:$PATH" STUB_VERSION=0.12.0 bash "$TMP/setup-x.sh" "$UI" 2>&1 </dev/null)
  RC=$?
  expect "ui_install_gum with old gum, non-root, fails with advice" 0 "rc=1"
fi

echo "== B: interactive, stub gum"
tty_stub 'ask X "Your name" dflt; echo "X=$X"'
expect "ask passes header and default" 0 "X=dflt"
: >"$TMP/stub.log"
STUB_OUT="typed" tty_stub 'ask X "Your name" dflt; echo "X=$X"'
expect "ask returns gum output" 0 "X=typed"
logged "ask gum args" 'input --header Your name --value dflt'
STUB_OUT="typed" tty_stub 'SOFTSYNK_ANSWER_X=given; ask X "Q" dflt; echo "X=$X"'
expect "pre-supplied answer skips gum even interactively" 0 "X=given"
STUB_RC=130 tty_stub 'ask X "Q"; echo "not reached"'
expect "ask Ctrl-C exits 130" 130 "Cancelled"
STUB_RC=1 tty_stub 'ask X "Q"; echo "not reached"'
expect "ask Esc exits 130" 130 "Cancelled"
STUB_RC=0 tty_stub 'confirm "Go?" Y; echo "rc=$?"'
expect "confirm yes" 0 "rc=0"
STUB_RC=1 tty_stub 'confirm "Go?" Y; echo "rc=$?"'
expect "confirm no is rc 1, not cancel" 0 "rc=1"
STUB_RC=130 tty_stub 'confirm "Go?" Y; echo "not reached"'
expect "confirm Ctrl-C exits 130" 130 "Cancelled"
: >"$TMP/stub.log"
STUB_RC=0 tty_stub 'confirm "Go?" N'
logged "confirm default arg" 'confirm --default=false Go?'
STUB_OUT="beta two" tty_stub 'choose C "Pick" 1 "alpha one" "beta two" "gamma"; echo "C=$C"'
expect "choose maps the selected option to its index" 0 "C=2"
: >"$TMP/stub.log"
STUB_OUT="gamma" tty_stub 'choose C "Pick" 3 "alpha one" "beta two" "gamma"'
logged "choose gum args" 'choose --header Pick --selected gamma alpha one beta two gamma'
STUB_RC=130 tty_stub 'choose C "Pick" 1 "a" "b"'
expect "choose cancel exits 130" 130 "Cancelled"
: >"$TMP/stub.log"
STUB_OUT="sekret" tty_stub 'ui_password P "Pw"; echo "len=${#P}"'
expect "password returns value" 0 "len=6"
logged "password gum args" 'input --password --header Pw'
STUB_OUT="yes" tty_stub 'confirm_danger restore mydb; echo "rc=$?"'
expect "danger: generic yes refused" 0 "rc=1"
STUB_OUT="mydb" tty_stub 'confirm_danger restore mydb; echo "rc=$?"'
expect "danger: typed target accepted" 0 "rc=0"
STUB_OUT="mydb" tty_stub 'SOFTSYNK_ACCEPT_DEFAULTS=1; confirm_danger restore mydb; echo "rc=$?"'
expect "danger: accept-defaults does not skip the typing" 0 "rc=0"
STUB_RC=130 tty_stub 'confirm_danger restore mydb; echo "not reached"'
expect "danger cancel exits 130" 130 "Cancelled"
STUB_VERSION=0.12.0 tty_stub 'ask X "Q"'
expect "old gum rejected with advice" 1 "gum 0.13 or newer"
PATH_NOGUM=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v -e '/homebrew' -e '/usr/local' | paste -sd: -)
PATH="$PATH_NOGUM" pty "$TMP/nokeys" 'ask X "Q"'
expect "missing gum rejected with advice" 1 "gum 0.13 or newer"
tty_stub 'SOFTSYNK_NONINTERACTIVE=1; ask X "Q"'
expect "SOFTSYNK_NONINTERACTIVE=1 wins over a TTY" 64 "--x"
tty_stub 'ui_flag --non-interactive; confirm "Q" Y'
expect "--non-interactive wins over a TTY" 64 "--accept-defaults"

echo "== C: real gum"
if command -v gum >/dev/null 2>&1 && bash -c 'source "$1"; ui_have_gum' _ "$UI"; then
  # real_rc KEYS CODE: KEYS are python-escaped, '|' separates keystroke steps.
  real_rc() {
    local keys=$1
    shift
    printf '%s\n' "$keys" | tr '|' '\n' >"$TMP/keys"
    pty "$TMP/keys" "$@"
  }
  real_rc 'hello\r' 'ask X "Q"; echo "X=$X"'
  expect "gum ask typed value" 0 "X=hello"
  real_rc '\r' 'ask X "Q" dflt; echo "X=$X"'
  expect "gum ask Enter keeps default" 0 "X=dflt"
  real_rc '\x1b' 'ask X "Q" dflt; echo "not reached"'
  expect "gum ask Esc cancels with 130" 130 "Cancelled"
  real_rc '\x03' 'ask X "Q" dflt; echo "not reached"'
  expect "gum ask Ctrl-C cancels with 130" 130 "Cancelled"
  real_rc '\r' 'confirm "Go?" Y; echo "rc=$?"'
  expect "gum confirm Enter takes default yes" 0 "rc=0"
  real_rc '\r' 'confirm "Go?" N; echo "rc=$?"'
  expect "gum confirm Enter takes default no" 0 "rc=1"
  real_rc 'n' 'confirm "Go?" Y; echo "rc=$?"'
  expect "gum confirm n is No" 0 "rc=1"
  real_rc '\x03' 'confirm "Go?" Y; echo "not reached"'
  expect "gum confirm Ctrl-C cancels with 130" 130 "Cancelled"
  real_rc '\r' 'choose C "Pick" 2 "alpha one" "beta two" "gamma"; echo "C=$C"'
  expect "gum choose Enter takes default index" 0 "C=2"
  real_rc '\x1b[B|\r' 'choose C "Pick" 1 "alpha one" "beta two" "gamma"; echo "C=$C"'
  expect "gum choose arrow down" 0 "C=2"
  real_rc '\x03' 'choose C "Pick" 1 "alpha one" "beta two"; echo "not reached"'
  expect "gum choose Ctrl-C cancels with 130" 130 "Cancelled"
  real_rc 's3cr3t!\r' 'ui_password P "Pw"; echo "len=${#P}"'
  expect "gum password value" 0 "len=7"
  if printf '%s' "$OUT" | grep -Fq 's3cr3t'; then FAIL=$((FAIL + 1)); echo "FAIL gum password echoed"; else PASS=$((PASS + 1)); fi
  real_rc 'yes\r' 'confirm_danger restore mydb; echo "rc=$?"'
  expect "gum danger: yes refused" 0 "rc=1"
  real_rc 'mydb\r' 'confirm_danger restore mydb; echo "rc=$?"'
  expect "gum danger: typed target accepted" 0 "rc=0"
else
  echo "skip: gum >= 0.13 not installed"
fi

echo "pass=$PASS fail=$FAIL"
[[ $FAIL -eq 0 ]]
