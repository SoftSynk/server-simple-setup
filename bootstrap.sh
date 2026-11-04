#!/usr/bin/env bash
# bootstrap.sh: stage, verify and activate a softsynk-server release.
#
#   bootstrap.sh [--root DIR] [--from DIR|TARBALL --sha256 HEX] [--install]
#
# Used for the first install and (via `softsynk-server update`) for updates.
# Exit codes: 0 ok, 2 failed (nothing was switched), 64 invalid usage.
set -Eeuo pipefail

ROOT="${SOFTSYNK_ROOT:-/opt/softsynk-server-management}"
BIN_DIR="${SOFTSYNK_BIN_DIR:-/usr/local/bin}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC=""
SHA=""
DO_INSTALL=0
STAGING=""
WORK=""

say() { printf '%s\n' "$*"; }
warn() { printf 'bootstrap: warning: %s\n' "$*" >&2; }
die() { # die CODE MESSAGE...
  local code="$1"
  shift
  printf 'bootstrap: error: %s\n' "$*" >&2
  exit "$code"
}

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [--root DIR] [--from DIR|TARBALL --sha256 HEX] [--install]

  --root DIR         management root (default: $SOFTSYNK_ROOT or /opt/softsynk-server-management)
  --from SOURCE      directory or tarball to install (default: the directory of this script)
  --sha256 HEX       expected sha256 of the tarball (required for a tarball, refused for a directory)
  --install          also install gum via apt on Ubuntu, then run `softsynk-server install`
  -h, --help         show this help

Environment: SOFTSYNK_ROOT, SOFTSYNK_BIN_DIR (command links; default /usr/local/bin).
Needs root for the default paths. Safe to re-run.
EOF
}

cleanup() {
  if [[ -n $STAGING && -e $STAGING ]]; then
    chmod -R u+w "$STAGING" 2>/dev/null || true
    rm -rf "${STAGING:?}"
  fi
  if [[ -n $WORK && -e $WORK ]]; then
    rm -rf "${WORK:?}"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) [[ $# -ge 2 ]] || die 64 "--root needs a value"; ROOT="$2"; shift 2 ;;
    --from) [[ $# -ge 2 ]] || die 64 "--from needs a value"; SRC="$2"; shift 2 ;;
    --sha256) [[ $# -ge 2 ]] || die 64 "--sha256 needs a value"; SHA="$2"; shift 2 ;;
    --install) DO_INSTALL=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die 64 "unknown argument: $1" ;;
  esac
done

[[ $ROOT == /* ]] || die 64 "--root must be an absolute path: $ROOT"
if [[ -n $SHA && ! $SHA =~ ^[0-9a-fA-F]{64}$ ]]; then
  die 64 "--sha256 must be 64 hex characters"
fi
if [[ -n $SRC && -f $SRC && -z $SHA ]]; then
  die 64 "a tarball source needs --sha256 (see docs/architecture.md, trust model)"
fi
if [[ -n $SRC && -d $SRC && -n $SHA ]]; then
  die 64 "--sha256 applies to tarball sources only"
fi
if [[ -n $SRC && ! -e $SRC ]]; then
  die 64 "source not found: $SRC"
fi

# --- preflight ---------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die 2 "python3 is required"
python3 -c 'import tomllib' 2>/dev/null || die 2 "python3 >= 3.11 (tomllib) is required"

# SOFTSYNK_OS_RELEASE is a test hook; the engine honours it too.
# shellcheck disable=SC1090
OS_ID="$(. "${SOFTSYNK_OS_RELEASE:-/etc/os-release}" 2>/dev/null && printf '%s' "${ID:-}")" || true
if [[ $OS_ID != ubuntu ]]; then
  warn "this is not Ubuntu (${OS_ID:-unknown}); modules that need Ubuntu will report unsupported"
fi

if [[ $DO_INSTALL -eq 1 ]]; then
  if command -v gum >/dev/null 2>&1; then
    say "gum already installed"
  elif [[ $OS_ID == ubuntu ]]; then
    [[ $(id -u) -eq 0 ]] || die 2 "--install needs root to install gum (try sudo)"
    say "installing gum via apt"
    # Same sequence as ui_install_gum in lib/ui.sh: a fresh image has no package index, so update and retry once.
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y gum; then
      { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y gum; } ||
        die 2 "apt-get install gum failed; install gum manually and re-run"
    fi
  else
    warn "gum not found and automatic install is Ubuntu-only; menus fall back to plain prompts"
  fi
fi

# --- root tree and lock -------------------------------------------------------
mkdir -p "$ROOT/lock" 2>/dev/null || die 2 "cannot create $ROOT (need root? try sudo)"
if [[ -z ${SOFTSYNK_LOCK_HELD:-} ]]; then
  # Same flock(2) lock the engine uses; the open file description stays held by fd 9.
  exec 9>"$ROOT/lock/framework.lock"
  python3 -c '
import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' || die 2 "another softsynk-server command is running (lock: $ROOT/lock/framework.lock)"
fi

for d in releases etc state log data; do
  [[ -d $ROOT/$d ]] || install -d -m 0755 "$ROOT/$d"
done
for d in secrets tmp; do
  [[ -d $ROOT/$d ]] || install -d -m 0700 "$ROOT/$d"
done
chmod 0700 "$ROOT/secrets"

# --- stage ----------------------------------------------------------------------
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

STAGING="$ROOT/releases/.staging-$$"
rm -rf "${STAGING:?}"
mkdir "$STAGING"
SRC="${SRC:-$SELF_DIR}"

if [[ -f $SRC ]]; then
  actual="$(sha256_of "$SRC")"
  expected="$(printf '%s' "$SHA" | tr 'A-F' 'a-f')"
  if [[ $actual != "$expected" ]]; then
    die 2 "sha256 mismatch for $SRC: expected $expected, got $actual; nothing was changed"
  fi
  say "sha256 verified"
  WORK="$ROOT/releases/.extract-$$"
  mkdir "$WORK"
  tar -xf "$SRC" -C "$WORK" --no-same-owner || die 2 "cannot extract $SRC"
  if [[ ! -f $WORK/softsynk-server ]]; then
    # tarballs usually wrap everything in one top-level directory
    shopt -s nullglob dotglob
    entries=("$WORK"/*)
    shopt -u nullglob dotglob
    if [[ ${#entries[@]} -eq 1 && -d ${entries[0]} ]]; then
      SRC_TREE="${entries[0]}"
    else
      die 2 "tarball does not contain softsynk-server at its top level"
    fi
  else
    SRC_TREE="$WORK"
  fi
else
  SRC_TREE="$SRC"
fi

[[ -f $SRC_TREE/softsynk-server && -f $SRC_TREE/VERSION && -f $SRC_TREE/modules.toml ]] \
  || die 2 "$SRC_TREE is not a softsynk-server release (needs softsynk-server, VERSION, modules.toml)"
VERSION="$(tr -d '[:space:]' <"$SRC_TREE/VERSION")"
[[ $VERSION =~ ^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$ ]] || die 2 "invalid VERSION: $VERSION"

tar -C "$SRC_TREE" --exclude=.git --exclude=.DS_Store --exclude=__pycache__ --exclude=.claude -cf - . \
  | tar -C "$STAGING" -xf - --no-same-owner

# validate the staged release (its own engine, so the new rules apply) before anything switches
if ! check_out="$(PYTHONDONTWRITEBYTECODE=1 python3 "$STAGING/softsynk-server" --root "$ROOT" validate 2>&1)"; then
  printf '%s\n' "$check_out" >&2
  die 2 "staged release $VERSION failed manifest validation; nothing was changed"
fi

FINAL="$ROOT/releases/$VERSION"
if [[ -e $FINAL ]]; then
  if diff -rq "$STAGING" "$FINAL" >/dev/null 2>&1; then
    say "release $VERSION already staged"
  else
    die 2 "release $VERSION exists with different contents; bump VERSION (nothing was changed)"
  fi
else
  mv "$STAGING" "$FINAL"
  STAGING=""
  chmod -R a-w "$FINAL"
  say "staged release $VERSION"
fi

# --- activate --------------------------------------------------------------------
set_link() { # set_link NAME TARGET: atomic symlink switch (new link, then rename over)
  local tmp="$ROOT/.$1.new-$$"
  rm -f "$tmp"
  ln -s "$2" "$tmp"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp" "$ROOT/$1"
}

NEW_TARGET="releases/$VERSION"
OLD_TARGET="$(readlink "$ROOT/current" 2>/dev/null || true)"
if [[ $OLD_TARGET != "$NEW_TARGET" ]]; then
  [[ -z $OLD_TARGET ]] || set_link previous "$OLD_TARGET"
  set_link current "$NEW_TARGET"
  say "activated $VERSION${OLD_TARGET:+ (previous: ${OLD_TARGET#releases/})}"
else
  say "release $VERSION already active"
fi

# --- command links -----------------------------------------------------------------
link_cmd() { # link_cmd NAME TARGET: create, or refresh only when the link is ours
  local dest="$BIN_DIR/$1" target="$2"
  if [[ -L $dest ]]; then
    case "$(readlink "$dest")" in
      "$ROOT"/current/*) [[ $(readlink "$dest") == "$target" ]] || ln -sfn "$target" "$dest" ;;
      *) warn "$dest points elsewhere; left unchanged" ;;
    esac
  elif [[ -e $dest ]]; then
    warn "$dest exists and is not a symlink; left unchanged"
  else
    ln -s "$target" "$dest"
    say "linked $dest"
  fi
}

if mkdir -p "$BIN_DIR" 2>/dev/null && [[ -w $BIN_DIR ]]; then
  link_cmd softsynk-server "$ROOT/current/softsynk-server"
  # every other command comes from the manifests: "name<TAB>absolute path inside the release"
  if cmds="$(PYTHONDONTWRITEBYTECODE=1 python3 "$FINAL/softsynk-server" --root "$ROOT" commands 2>/dev/null)"; then
    real_final="$(cd "$FINAL" && pwd -P)" # the engine prints realpaths
    while IFS=$'\t' read -r name path; do
      [[ -n $name ]] || continue
      if [[ $path == "$real_final"/* ]]; then link_cmd "$name" "$ROOT/current/${path#"$real_final"/}"
      else warn "command $name points outside the release; not linked"; fi
    done <<<"$cmds"
  else
    warn "could not list module commands; only softsynk-server was linked"
  fi
else
  warn "cannot write to $BIN_DIR; command links skipped (need root?)"
fi

if [[ $DO_INSTALL -eq 1 ]]; then
  exec 9>&- # release the lock; the engine takes its own
  exec "$ROOT/current/softsynk-server" --root "$ROOT" install
fi
say "done. next: softsynk-server install   (or: softsynk-server --status)"
