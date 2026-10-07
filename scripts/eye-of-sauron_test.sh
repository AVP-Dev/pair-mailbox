#!/bin/bash
# Тест Ока Саурона: deadlock-сигнал на копии скрипта в tmp.
# Живой tmux не нужен: панели "none" дают стоящий хэш — как idle-панели.
set -u
EYE="/home/alexios/work/umputum/scripts/eye-of-sauron.sh"
fail=0
run_eye() {  # $1 = state.json content, $2 = obligations count
  local t; t="$(mktemp -d)"
  mkdir -p "$t/.pair/obligations" "$t/scripts"
  cp "$EYE" "$t/scripts/"
  printf '%s' "$1" > "$t/.pair/state.json"
  : > "$t/.pair/transcript.jsonl"
  for _ in $(seq 1 "$2"); do : > "$t/.pair/obligations/x.json"; done
  bash "$t/scripts/eye-of-sauron.sh"
  cat "$t/.pair/EYE.log"
  rm -rf "$t"
}
OLD="$(python3 -c 'from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(minutes=25)).isoformat())')"
ST='{"phase":"implement","round":32,"writer":"w","roles":{"w":{"last_seen":"'"$OLD"'","pane":"none:0.0"},"c":{"last_seen":"'"$OLD"'","pane":"none:0.0"}},"verdicts":[]}'
out="$(run_eye "$ST" 1)"
case "$out" in
  *DEADLOCK*) echo "ok deadlock on two-silent-plus-obligation";;
  *) echo "FAIL: no DEADLOCK: $out"; fail=1;;
esac
out="$(run_eye "$ST" 0)"
case "$out" in
  *DEADLOCK*) echo "FAIL: deadlock without obligations: $out"; fail=1;;
  *) echo "ok silent without obligations";;
esac
FRESH="$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())')"
ST2='{"phase":"done","round":32,"writer":"w","roles":{"w":{"last_seen":"'"$OLD"'","pane":"none:0.0"},"c":{"last_seen":"'"$OLD"'","pane":"none:0.0"}},"verdicts":[]}'
out="$(run_eye "$ST2" 1)"
case "$out" in
  *DEADLOCK*) echo "FAIL: deadlock in done: $out"; fail=1;;
  *) echo "ok done never alarms";;
esac
exit "$fail"
