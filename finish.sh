#!/bin/sh
# Собирает репозиторий из заметок, применяет правки сервера, коммитит.
set -eu
cd "$HOME/work/umputum"
N=.pair/notes

echo "== 1. убираю устаревшие заметки install.sh"
rm -f "$N/z-install.md" "$N/file-e-install.md" "$N/probe.md"

echo "== 2. распаковываю"
for f in "$N"/*.md; do
  p=$(sed -n 's/^>>> //p' "$f" | head -1)
  [ -z "$p" ] && continue
  mkdir -p "$(dirname "$p")"
  sed -n '/^>>> /,$p' "$f" | tail -n +2 > "$p"
  echo "   $p"
done

echo "== 3. правки server.py"
python3 -c "
import json, pathlib, re, subprocess, sys
N = pathlib.Path('.pair/notes')

def json_pairs(name):
    raw = (N / name).read_text(encoding='utf-8')
    body = raw.split('>>>', 1)[1].split(chr(10), 1)[1]
    return json.loads(body.strip())

raw = (N / 'f-srv.md').read_text(encoding='utf-8')
t = raw.split('>>> f-srv', 1)[1].split(chr(10), 1)[1]
m = re.search(chr(39)*3 + r'(.*?)' + chr(39)*3 + chr(92)+'n' + chr(39)*3 + r'(.*?)' + chr(39)*3 + chr(92)+'n', t, re.S)
if not m:
    raise SystemExit('ПРОВАЛ: в f-srv нет OLD1/NEW1')
pairs = [{'old': m.group(1), 'new': m.group(2)}]
for n in ('f-adv.md', 'f-peer.md', 'f-claim.md'):
    pairs += json_pairs(n)

p = pathlib.Path('mcp/pair_mailbox/server.py')
src = p.read_text(encoding='utf-8')
for i, pr in enumerate(pairs, 1):
    n = src.count(pr['old'])
    if n != 1:
        raise SystemExit('ПРОВАЛ замена #' + str(i) + ': совпадений ' + str(n))
    src = src.replace(pr['old'], pr['new'])
p.write_text(src, encoding='utf-8')
print('   server.py: правок ' + str(len(pairs)))
for n in ('f-srv.md','f-adv.md','f-peer.md','f-claim.md'):
    (N / n).unlink(missing_ok=True)
"

echo "== 4. install.sh не должен писать конфиг неустановленному CLI"
python3 -c "
import pathlib
p = pathlib.Path('install.sh')
s = p.read_text(encoding='utf-8')
old = '''    cfg = expand(spec["path"])
    return cfg.parent.exists()'''
new = '''    return False'''
if old in s:
    s = s.replace(old, new)
    p.write_text(s, encoding='utf-8')
    print('   поправлено: присутствие = только бинарь')
else:
    print('   уже поправлено или не найдено — проверь вручную')
"
rm -f "$N/finish.sh" "$N/z3-doctor.md" 2>/dev/null || true

echo "== 5. права и git"
chmod +x install.sh bin/* 2>/dev/null || true
git init -q
git add -A
git -c user.name="pair-mailbox" -c user.email="noreply@localhost" \
  commit -q -m "pair-mailbox: канал, протокол и навыки парной работы двух агентов" || echo "   коммит не прошёл"

echo "== 6. проверка"
./install.sh --check || true
echo
bin/pairdoctor
