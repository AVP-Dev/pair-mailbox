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
# Обсуждение до первого коммита (карточка pair-discussion-before-work п.3).
# send/advance поля round не несут (только evidence) — окно позиционное:
# последний advance(implement), далее план писателя, далее evidence(commit).
# План = subject содержит план|plan (case-insensitive): §9a допускает оба.
# discuss=ok — план внутри окна до коммита; miss — иначе.
discuss = "miss"
try:
    import os as _os
    import re as _re
    _plan_re = _re.compile(r"план|plan", _re.IGNORECASE)
    paths = [root + "/.pair/transcript.jsonl"] + sorted(
        glob.glob(root + "/.pair/transcript-archive/*.jsonl"))
    win_open, planned = False, False
    for p in paths:
        if not _os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if '"kind": "advance"' in ln and '"phase": "implement"' in ln:
                    win_open, planned = True, False
                elif '"kind": "send"' in ln and win_open:
                    try:
                        e = json.loads(ln)
                    except Exception:
                        continue
                    if e.get("role") == writer and _plan_re.search(str(e.get("subject", ""))):
                        planned = True
                elif '"kind": "evidence"' in ln and '"evidence_kind": "commit"' in ln and win_open:
                    discuss = "ok" if planned else "miss"
                    win_open = False
except Exception:
    discuss = "unknown"
line = f"{time.strftime('%m-%d %H:%M')} ph={phase} r={rnd} w_sil={ws}s c_sil={cs}s obl={obl} verdict={fresh} discuss={discuss}"
alarm = (phase != "done" and ws > 1200 and obl == 0)
# Затык — это наоборот: ОБЕ стороны молчат по MCP, а обязательства висят.
# last_seen меряет вызовы, а не работу (возражение критика: 31 мин молчания
# при активной работе в панели), поэтому второй фактор — хэш хвоста панели:
# изменился — работает, стоит — висит. Дешево: capture-pane + tail + sha256.
deadlock = ""
if phase != "done" and obl > 0 and ws > 600 and cs > 600:
    import hashlib as _hl
    _hashes, _changed = {}, False
    _hashfile = root + "/.pair/eye-pane-hashes.json"
    try:
        _hashes = json.load(open(_hashfile))
    except Exception:
        _hashes = {}
    for _role, _info in roles.items():
        _pane = (_info or {}).get("pane") or ""
        if not _pane:
            continue
        try:
            _cap = subprocess.run(["tmux", "capture-pane", "-p", "-t", _pane],
                                  capture_output=True, text=True, timeout=5).stdout
            _h = _hl.sha256("\n".join(_cap.splitlines()[-3:]).encode()).hexdigest()[:16]
        except Exception:
            continue
        if _role in _hashes and _hashes[_role] != _h:
            _changed = True
        _hashes[_role] = _h
    try:
        json.dump(_hashes, open(_hashfile, "w"))
    except Exception:
        pass
    if not _changed:
        deadlock = " DEADLOCK" if (ws > 1200 and cs > 1200) else " warn-deadlock"
# Проверка Budget Ceiling
budget_alarm = False
budget_str = ""
try:
    sys.path.insert(0, root + "/mcp/pair_mailbox")
    from budget import check_budget
    b_ok, b_why, _ = check_budget(root + "/.pair", st, phase)
    if not b_ok and phase != "done":
        budget_alarm = True
        budget_str = " BUDGET_EXCEEDED"
except Exception:
    pass
is_alarm = alarm or budget_alarm
print(("ALARM " if is_alarm else "ok ") + line + deadlock + budget_str)
EOF
exit 0
