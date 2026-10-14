#!/bin/bash
# The Eye-of-Sauron test: the deadlock signal on a script copy in tmp.
# No live tmux needed: "none" panes give a still hash — like idle panes.
set -u
EYE="$(cd "$(dirname "$0")" && pwd)/eye-of-sauron.sh"
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
  ALARM*DEADLOCK*) echo "ok deadlock alarms on two-silent-plus-obligation";;
  *) echo "FAIL: no ALARM DEADLOCK: $out"; fail=1;;
esac
out="$(run_eye "$ST" 0)"
case "$out" in
  *DEADLOCK*) echo "FAIL: deadlock without obligations: $out"; fail=1;;
  # Plain ALARM here is the ordinary silence rule (ws > 1200, obl == 0),
  # not the deadlock branch — only DEADLOCK text is this card's subject.
  *) echo "ok no deadlock without obligations";;
esac
FRESH="$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())')"
ST2='{"phase":"done","round":32,"writer":"w","roles":{"w":{"last_seen":"'"$OLD"'","pane":"none:0.0"},"c":{"last_seen":"'"$OLD"'","pane":"none:0.0"}},"verdicts":[]}'
out="$(run_eye "$ST2" 1)"
case "$out" in
  ALARM*) echo "FAIL: alarm in done: $out"; fail=1;;
  *DEADLOCK*) echo "FAIL: deadlock in done: $out"; fail=1;;
  *) echo "ok done never alarms";;
esac
ST3='{"phase":"implement","round":32,"writer":"w","roles":{"w":{"last_seen":"'"$FRESH"'","pane":"none:0.0"},"c":{"last_seen":"'"$FRESH"'","pane":"none:0.0"}},"verdicts":[]}'
out="$(run_eye "$ST3" 0)"
case "$out" in
  ALARM*) echo "FAIL: alarm on a live round: $out"; fail=1;;
  *) echo "ok live round stays quiet";;
esac
exit "$fail"
