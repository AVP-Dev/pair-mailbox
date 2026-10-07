#!/usr/bin/env bash
# json.sh - JSON one-liners on python3 stdlib (no jq dependency, so the
# skill works on hosts where jq is absent or is a jaq shim).
set -u

# Print the JSON string literal for $1 (safe embedding into documents).
json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# Validate: prints OK, exit 0 iff $1 parses.
json_valid() {
  python3 -c 'import json,sys; json.load(open(sys.argv[1])); print("OK")' "$1" 2>/dev/null
}

# Print value of top-level key $1 in file $2 (strings raw, rest compact).
json_get() {
  local key="$1" file="$2"
  python3 - "$key" "$file" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[2]))
v = d.get(sys.argv[1])
if isinstance(v, str):
    print(v)
else:
    print(json.dumps(v))
PY
}

json_help() {
  echo "lib/json.sh: json_escape <str> | json_valid <file> | json_get <key> <file>"
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then json_help; exit 0; fi
