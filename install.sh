#!/usr/bin/env python3
"""Нулевое касание: ставит pair-mailbox во все найденные CLI и проверяет, что
MCP действительно поднимается. Писать конфиг недостаточно — молчаливый отказ
формата мы уже ловили дважды, поэтому проверка настоящая: запускаем ровно ту
команду, которую записали в конфиг, и делаем handshake.

  ./install.sh              поставить всё найденное и проверить
  ./install.sh codex        только один профиль
  ./install.sh --check      только проверить, ничего не менять
  ./install.sh --list       показать профили
"""
import json, os, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
SERVER = ROOT / "mcp/pair_mailbox/serve.sh"
SKILLS = ROOT / "skills"
MARK = "<!-- pair-mailbox -->"

NOTE = """
## Парная работа

Рядом доступен соседний агент через инструменты `pair_*`.
Прежде чем браться за задачу, вызови `pair_state`, затем `pair_claim`.

- Захватил авторитет — ты писатель: правь файлы, гоняй тесты, коммить, открывай PR.
- Не захватил — ты критик: читай, спорь, пиши находки в `pair_note`, файлы не правь.

Раунд можно переоткрыть в любую сторону: `pair_advance("implement")` откатит фазу назад.
Это не требует правы записи. Если писатель ушёл молча — скажи об этом пользователю.

Если сосед молчит — работай один, это не блокер.
Правила целиком: `docs/PROTOCOL.md` в репозитории pair-mailbox.
"""


def expand(p):
    return Path(p).expanduser()


def binary(name):
    return shutil.which(name)


def present(profile):
    """CLI считается установленным, если его бинарь или каталог конфига есть."""
    spec = profile["mcp"]
    names = {"opencode": "opencode", "agy": "agy", "codex": "codex", "claude": "claude"}
    key = next((k for k, v in PROFILES.items() if v is profile), "")
    bin_name = profile.get("binary", names.get(key, key))
    if binary(bin_name):
        return True
    cfg = expand(spec["path"])
    return cfg.parent.exists()


def argv_from_toml(text):
    """Минимум, который нужен из блока [mcp_servers.pair]: command и args."""
    cmd, args, inside = None, [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            inside = s == "[mcp_servers.pair]"
            continue
        if not inside or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip().strip('"')
        if key == "command":
            cmd = val
        elif key == "args":
            args = [a.strip().strip('"') for a in val.strip("[]").split(",") if a.strip()]
    if not cmd:
        raise KeyError("command")
    return [cmd] + args


def write_mcp(profile, role):
    spec = profile["mcp"]
    path = expand(spec["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["sh", str(SERVER), role]

    if spec["format"] == "toml":
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines, skip = [], False
        for line in text.splitlines():
            if line.strip().startswith("[mcp_servers.pair]"):
                skip = True
                continue
            if skip:
                if line.strip().startswith("["):
                    skip = False
                else:
                    continue
            lines.append(line)
        block = '[mcp_servers.pair]\ncommand = "sh"\nargs = ["%s", "%s"]\n' % (SERVER, role)
        path.write_text("\n".join(lines).rstrip() + "\n\n" + block, encoding="utf-8")
        return argv

    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print("    !! не читается: " + str(exc))
            return None
    entry = dict(spec["entry"])
    if isinstance(entry.get("command"), list):
        entry["command"] = argv
    else:
        entry["command"], entry["args"] = argv[0], argv[1:]
    cfg.setdefault(spec["root_key"], {})["pair"] = entry
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return argv


def write_skills(profile):
    root = expand(profile["skills_dir"])
    out = []
    for d in sorted(SKILLS.glob("*/")):
        t = root / d.name
        t.mkdir(parents=True, exist_ok=True)
        shutil.copy(d / "SKILL.md", t / "SKILL.md")
        out.append(d.name)
    return out


def write_instructions(profile):
    path = expand(profile["instructions"])
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK in text:
        path.write_text(text.split(MARK)[0].rstrip() + "\n" + MARK + NOTE, encoding="utf-8")
        return "обновлена"
    sep = "\n" if text.strip() else ""
    path.write_text(text + sep + MARK + NOTE, encoding="utf-8")
    return "дописана"


def verify(argv):
    """Поднимаем сервер ровно так, как это сделает CLI, и жмём руку."""
    req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "pair-install"}},
    }) + "\n"
    try:
        proc = subprocess.run(argv, input=req, capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return False, "запуск не удался: " + str(exc)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reply = json.loads(line)
        except Exception:
            continue
        info = reply.get("result", {}).get("serverInfo", {})
        if info.get("name") == "pair-mailbox":
            return True, info.get("version", "?")
    return False, "сервер не ответил как pair-mailbox"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        for k, v in PROFILES.items():
            if not k.startswith("_"):
                print("%-10s %-20s %s" % (k, v["label"], "есть" if present(v) else "нет"))
        return 0

    check_only = "--check" in sys.argv
    targets = args or [k for k, v in PROFILES.items() if not k.startswith("_") and present(v)]

    if not targets:
        print("не найдено ни одного известного CLI")
        return 1

    failed = []
    for name in targets:
        profile = PROFILES.get(name)
        if not profile:
            print("нет профиля: " + name)
            failed.append(name)
            continue
        print("\n" + name + " — " + profile["label"])
        role = name
        if check_only:
            spec = profile["mcp"]
            try:
                text = expand(spec["path"]).read_text(encoding="utf-8")
                if spec.get("format") == "toml":
                    argv = argv_from_toml(text)
                else:
                    cfg = json.loads(text)
                    entry = cfg[spec["root_key"]]["pair"]
                    argv = entry["command"] if isinstance(entry["command"], list) else [entry["command"]] + entry.get("args", [])
            except Exception:
                print("    конфиг не найден или нечитаем")
                failed.append(name)
                continue
        else:
            argv = write_mcp(profile, role)
            if argv is None:
                failed.append(name)
                continue
            print("  MCP:      " + expand(profile["mcp"]["path"]).name)
            print("  навыки:   " + ", ".join(write_skills(profile)))
            print("  инструкции: " + write_instructions(profile))

        ok, detail = verify(argv)
        print("  проверка: " + ("ОК, pair-mailbox " + detail if ok else "НЕ РАБОТАЕТ — " + detail))
        if not ok:
            failed.append(name)

    print("\nтесты сервера")
    env = dict(os.environ, PAIR_DISABLE_RING="1")
    t_res = subprocess.run([sys.executable, str(ROOT / "mcp/pair_mailbox/test_protocol.py")], cwd=str(ROOT), env=env)
    if t_res.returncode != 0:
        print("\nТЕСТЫ НЕ ПРОШЛИ")
        return 1

    if failed:
        print("\nНЕ СДЕЛАНО для: " + ", ".join(failed))
        print("разбери эти вручную, остальные работают")
        return 1

    print("\nвсё работает. перезапусти агентов и скажи им: работаем вместе над X")
    return 0


if __name__ == "__main__":
    sys.exit(main())
