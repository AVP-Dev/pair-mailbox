#!/bin/bash
# Око Саурона: безтокенный сторож пары. Cron: */5 * * * * /path/scripts/eye-of-sauron.sh
# Пишет ОДНУ строку в .pair/EYE.log за тик. Ноль токенов: чистый bash + python stdlib.
# ALARM — только если писатель молчит >20 мин, открытых obligations нет и раунд не done
# (значит пилот пропустил — смотреть человеку).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/.pair/EYE.log"
STATE="$ROOT/.pair/state.json"
[ -f "$STATE" ] || exit 0
python3 - "$STATE" "$ROOT" >> "$LOG" 2>/dev/null <<'EOF'
import json, sys, time, subprocess
state_path, root = sys.argv[1], sys.argv[2]
now = time.time()
st = json.load(open(state_path))
phase = st.get("phase", "?")
rnd = st.get("round", "?")
roles = st.get("roles", {})
def silent(role):
    try:
        from datetime import datetime
        ls = roles.get(role, {}).get("last_seen", "")
        dt = datetime.fromisoformat(ls)
        return int(now - dt.timestamp())
    except Exception:
        return -1
ws = silent(st.get("writer", ""))
crit = roles.copy()
writer = st.get("writer")
crit.pop(writer, None)
cs = max([silent(r) for r in crit] or [-1])
import glob
obl = len(glob.glob(root + "/.pair/obligations/*.json"))
head = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
vrev = ""
for v in (st.get("verdicts") or [])[::-1]:
    vrev = ((v.get("rev") or {}).get("head") or "")[:7]
    if vrev:
        break
fresh = "fresh" if (vrev == head) else ("stale(" + vrev + "->" + head + ")" if vrev else "none")
line = f"{time.strftime('%m-%d %H:%M')} ph={phase} r={rnd} w_sil={ws}s c_sil={cs}s obl={obl} verdict={fresh}"
alarm = (phase != "done" and ws > 1200 and obl == 0)
print(("ALARM " if alarm else "ok ") + line)
EOF
exit 0
