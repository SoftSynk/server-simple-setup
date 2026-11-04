#!/bin/sh
# Shared behaviour of the fixture modules. Copied into each module directory by
# the test helper. Usage: run.sh status MODE | install MODE | configure
op="$1"
mode="${2:-ok}"
[ "$op" = status ] && mode="${SOFTSYNK_TEST_STATUS:-$mode}"  # tests override the status mode
state="${SOFTSYNK_STATE_DIR:?}"
case "$op" in
status)
  case "$mode" in
    slow) echo $$ > "$SOFTSYNK_ROOT/slow.pid"; exec sleep 30 ;;
    badjson) echo "not json {"; exit 0 ;;
    empty) exit 0 ;;
    wrongmodule) echo '{"schema":1,"module":"someone-else","install_state":"installed","health":"healthy"}'; exit 0 ;;
    badschema) echo "{\"schema\":2,\"module\":\"$SOFTSYNK_MODULE\",\"install_state\":\"installed\",\"health\":\"healthy\"}"; exit 0 ;;
    badhealth) echo "{\"schema\":1,\"module\":\"$SOFTSYNK_MODULE\",\"install_state\":\"installed\",\"health\":\"great\"}"; exit 0 ;;
  esac
  if [ -e "$state/installed" ]; then installed=installed; else installed=not_installed; fi
  warnings='[]'
  [ "$mode" = degraded ] && warnings='["fixture warning"]'
  printf '{"schema":1,"module":"%s","version":"1.0.0","observed_at":"2026-01-01T00:00:00Z","install_state":"%s","health":"%s","summary":"fixture %s","details":{},"warnings":%s,"actions":[]}\n' \
    "$SOFTSYNK_MODULE" "$installed" "$mode" "$mode" "$warnings"
  # a non-zero exit with valid JSON must still be trusted
  [ "$mode" = unhealthy ] && exit 2
  exit 0
  ;;
install)
  case "$mode" in
    fail) echo "fixture install failure" >&2; exit 1 ;;
    cancel) exit 130 ;;
    hold) : > "$SOFTSYNK_ROOT/hold.marker"; sleep 3 ;;
    sigint) trap 'kill $! 2>/dev/null; exit 130' INT; : > "$SOFTSYNK_ROOT/hold.marker"; sleep 10 & wait $! ;;
  esac
  mkdir -p "$state"
  : > "$state/installed"
  echo "$SOFTSYNK_MODULE" >> "$SOFTSYNK_ROOT/install-order.log"
  env | grep '^SOFTSYNK_' | sort > "$SOFTSYNK_ROOT/env-$SOFTSYNK_MODULE.txt"
  exit 0
  ;;
configure)
  mkdir -p "$SOFTSYNK_ETC_DIR"
  echo "interactive=$SOFTSYNK_INTERACTIVE accept=$SOFTSYNK_ACCEPT_DEFAULTS dry=$SOFTSYNK_DRY_RUN" > "$SOFTSYNK_ETC_DIR/configured"
  exit 0
  ;;
esac
echo "unknown op: $op" >&2
exit 64
