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
import json, os, re, shutil, subprocess, sys, tempfile
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
    names = {"opencode": "opencode", "agy": "agy", "codex": "codex", "claude": "claude",
             "pi": "pi", "omp": "omp"}
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


def strip_jsonc(text):
    """Убрать комментарии и висячие запятые, не трогая строковые литералы.

    .jsonc — это JSON с комментариями, а открывающая скобка и косая черта внутри
    строки обязаны остаться строкой. Регулярка без разбора кавычек режет URL
    вида "https://..." пополам, поэтому здесь маленький конечный автомат, а не
    замена по регулярному выражению.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_jsonc(text):
    """Разобрать .jsonc: сначала попытка как обычный JSON, потом со снятием
    комментариев. Второй путь нужен для файлов, где комментарии есть."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r",(\s*[}\]])", r"\1", strip_jsonc(text))
    return json.loads(cleaned)

def _json_skip_value(text, i):
    """Конец JSON-значения с позиции i: строки, комментарии и висячие
    запятые учитываются, иначе замена блока режет URL или // в строке."""
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if text.startswith("//", i):
        nl = text.find("\n", i)
        return _json_skip_value(text, n if nl == -1 else nl + 1)
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return _json_skip_value(text, n if end == -1 else end + 2)
    if i < n and text[i] in "{[":
        open_c, close_c = text[i], "}" if text[i] == "{" else "]"
        depth, j, instr, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif text.startswith("//", j):
                nl = text.find("\n", j)
                j = n if nl == -1 else nl
            elif text.startswith("/*", j):
                end = text.find("*/", j + 2)
                j = n - 1 if end == -1 else end + 1
            elif ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    if i < n and text[i] == '"':
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                return j + 1
            j += 1
        return n
    j = i
    while j < n and text[j] not in ",}]":
        j += 1
    return j


def _json_find_key(text, start, end, key):
    """Диапазон значения ключа в объекте [start, end): позиция ключа вне
    строк и комментариев плюс конец его значения. Нет ключа — None."""
    i = start
    while i < end:
        if text[i] == '"':
            j = i + 1
            while j < end:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            word = text[i + 1:j]
            k = j + 1
            while k < end and text[k] in " \t\r\n":
                k += 1
            if k < end and text[k] == ":" and word == key:
                v = _json_skip_value(text, k + 1)
                return (i, v)
            i = j + 1
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = end if nl == -1 or nl >= end else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = end if close == -1 or close + 2 >= end else close + 2
            continue
        i += 1
    return None


def splice_json_block(text, root_key, server_key, entry_json):
    """Вставить entry в сырой текст конфига, не трогая остальное.

    json.dumps переписывал бы файл целиком и стирал комментарии человека,
    поэтому правится только диапазон значения mcp.<server>: замена при
    наличии, вставка перед закрывающей скобкой объекта при отсутствии.
    Возвращает (новый_текст, действие): replaced|inserted|created."""
    stripped = text.strip()
    if not stripped:
        return ('{\n  "%s": {\n    "%s": %s\n  }\n}\n' % (root_key, server_key, entry_json), "created")
    found = _json_find_key(text, 0, len(text), root_key)
    if found is None:
        # Вставка перед закрывающей скобкой корня в сырой текст: висячая
        # запятая перед ней снимается, иначе получится ",," и битый JSON.
        close = text.rfind("}")
        inner = text[:close].rstrip()
        if inner.endswith(","):
            inner = inner[:-1]
        sep = "," if inner.strip().strip("{}").strip() else ""
        tail = text[close:]
        return ("%s%s\n  \"%s\": {\n    \"%s\": %s\n  }\n%s" % (inner, sep, root_key, server_key, entry_json, tail), "inserted-root")
    _, root_end = found
    inner_start = text.find("{", found[0] + len(root_key) + 2)
    inner_end = root_end - 1
    pair = _json_find_key(text, inner_start + 1, inner_end, server_key)
    if pair is not None:
        return (text[:pair[0]] + '"%s": %s' % (server_key, entry_json) + text[pair[1]:], "replaced")
    before = text[inner_start + 1:inner_end]
    # Висячая запятая перед } снимается, иначе вставка даёт ",," и битый JSON.
    head = text[:inner_end].rstrip()
    if head.endswith(","):
        head = head[:-1]
    sep = "" if not head[inner_start + 1:].strip() else ","
    return (head + "%s\n    \"%s\": %s\n" % (sep, server_key, entry_json) + text[inner_end:], "inserted")

def write_mcp(profile, role):
    spec = profile["mcp"]
    path = expand(spec["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Без --state by design: в общие конфиги (~/.config) абсолютный pin
    # проекта нельзя — все проекты делили бы одно состояние. Резолв на
    # рантайме: walk-up до .pair/state.json в serve.sh и extract_state.
    argv = ["sh", str(SERVER), role]
    path.parent.mkdir(parents=True, exist_ok=True)

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

    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    if raw.strip():
        try:
            parse_jsonc(raw)
        except Exception as exc:
            print("    !! не читается: " + str(exc))
            print("       " + str(path) + " не тронут. Правь конфиг руками или")
            print("       удали блок mcp.pair из него и запусти установку снова.")
            return None
    entry = dict(spec["entry"])
    if isinstance(entry.get("command"), list):
        entry["command"] = argv
    else:
        entry["command"], entry["args"] = argv[0], argv[1:]
    entry_json = json.dumps(entry, indent=2, ensure_ascii=False)
    # Правка по месту, а не json.dumps всего файла: иначе комментарии
    # и форматирование человека стираются при каждой установке.
    backup = path.with_suffix(path.suffix + ".bak")
    if raw.strip():
        backup.write_text(raw, encoding="utf-8")
    new_text, _action = splice_json_block(raw, spec["root_key"], spec.get("server_key", "pair"), entry_json)
    path.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")
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


def replace_owned_block(text, body):
    """Заменить ТОЛЬКО наш блок, всё после маркера сохранить.

    Прежний код делал `text.split(MARK)[0]`, то есть удалял всё, что человек
    написал после маркера, молча и на каждом запуске. Удаление содержимого
    чужого файла — ровно то, PROTOCOL §12 требует спросить, поэтому здесь
    хвост остаётся, а перед перезаписью ещё и снимается копия.
    """
    head, _, tail = text.partition(MARK)
    kept = tail[len(NOTE):] if tail.startswith(NOTE) else tail
    return head.rstrip() + "\n" + MARK + NOTE + ("\n" + kept.lstrip("\n") if kept.strip() else "")


def write_instructions(profile):
    path = expand(profile["instructions"])
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK in text:
        updated = replace_owned_block(text, NOTE)
    else:
        # Тот же join, что и в replace_owned_block, иначе первая установка даёт
        # пустую строку перед маркером, а вторая её убирает — и файл меняется
        # при каждом запуске, обещая идемпотентность в шапке.
        head = text if not text.strip() else text.rstrip()
        updated = head + ("\n" if head else "") + MARK + NOTE
    if updated == text:
        return "без изменений"
    # Копия перед перезаписью: файл принадлежит человеку, а не нам.
    try:
        path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
    except OSError as error:
        print("    !! не смог сохранить копию: " + str(error))
        return "НЕ ЗАПИСАНО (нет копии)"
    path.write_text(updated, encoding="utf-8")
    return "обновлена"


def verify(argv):
    """Поднимаем сервер ровно так, как это сделает CLI, и жмём руку.

    Проверка изолирована от настоящего ростера, и это не аккуратность, а
    требование. Найдено вживую: `verify` поднимал `serve.sh <role>` с
    унаследованным окружением и без явного состояния, то есть писал в НАСТОЯЩИЙ
    `.pair/state.json` того проекта, из которого запущен install.sh, и наследовал
    `TMUX_PANE` запускающего процесса.

    Дальше это ударило дважды. Панель `opencode` перезаписалась адресом панели
    `agy` — прогон тестов шёл из его окна, — и после перехода на адреса сессий
    перезапись стала ещё и канонической: роль получала чужую панель не на один
    запуск, а прочно. Плюс в ростер попадал pid проверочного процесса, который
    сразу умирает, то есть роль выглядела «мёртвой при живом окне».

    Проверка транспорта не имеет права менять состояние. Поэтому состояние
    временное, а из окружения вычищено всё, что делает процесс агентом.
    """
    req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "pair-install"}},
    }) + "\n"
    scratch = tempfile.mkdtemp(prefix="pair-verify-")
    # --state в argv (явный pin проекта) побеждает PAIR_STATE, поэтому для
    # проверки он переписывается на временный: иначе handshake пишет в живой
    # ростер (регрессия pin, ловит TestVerifyDoesNotTouchTheRoster).
    clean = []
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a == "--state":
            skip_next = True
            continue
        if a.startswith("--state="):
            continue
        clean.append(a)
    argv = clean + ["--state", scratch]
    try:
        env = {k: v for k, v in os.environ.items()
               if k not in ("PAIR_STATE", "PAIR_ROLE", "PAIR_DISABLE_RING")}
        env["PAIR_STATE"] = scratch
        # TMUX_PANE — это «кто я», и проверке знать о ней незачем: без неё сервер
        # честно не считает себя живущим в панели. TMUX убираем вместе с ней,
        # иначе сервер решит, что он внутри tmux, по одним переменным окружения.
        for name in ("TMUX_PANE", "TMUX"):
            env.pop(name, None)
        proc = subprocess.run(argv, input=req, capture_output=True, text=True,
                              timeout=15, env=env)
    except Exception as exc:
        return False, "запуск не удался: " + str(exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
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


def write_native_permissions(name):
    """Нативный permission-слой CLI: allow-list вместо прокликивания человеком.

    codex/pi/omp: нативных allow/deny списков нет (проверено раундом 54:
    codex — только approval_policy, pi — доверие файлам), поэтому установщик
    их не трогает: они остаются на пилоте (§13b). Это решение, а не недоделка.
    """
    if name not in ("opencode", "agy", "claude"):
        return None
    sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
    try:
        import autoperms
        return autoperms.write_native_perms(name, ROOT, Path.home())
    except Exception as exc:  # noqa: BLE001 - установка не должна падать из-за прав
        print("    права:   НЕ ЗАПИСАНЫ (" + str(exc) + ") — агент будет спрашивать")
        return None


def main():

    # Версия Python проверяется ДО установки, а не пачкой тестов в конце.
    # Установка на macOS давала 210 ошибок импорта, и ни одна из них не
    # говорила, что системный python3 там 3.9, а код написан под 3.8+.
    if sys.version_info < (3, 8):
        print("нужен python3 3.8 или новее, а запущен " + sys.version.split()[0])
        print("поставь новее и повтори: brew install python   (проверь: python3 -V)")
        return 1
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        for k, v in PROFILES.items():
            if not k.startswith("_"):
                print("%-10s %-20s %s" % (k, v["label"], "есть" if present(v) else "нет"))
        return 0

    print(r"""
  ____       _        __  __       _ _ _               
 |  _ \ __ _(_)_ __  |  \/  | __ _(_) | |__   _____  __
 | |_) / _` | | '__| | |\/| |/ _` | | | '_ \ / _ \ \/ /
 |  __/ (_| | | |    | |  | | (_| | | | |_) | (_) >  < 
 |_|   \__,_|_|_|    |_|  |_|\__,_|_|_|_.__/ \___/_/\_\

 Autonomous Two-Agent Pair Programming over MCP & tmux
 Created by Aliaksei Patskevich (AVP-Dev) • https://avpdev.com
 GitHub: https://github.com/AVP-Dev/pair-mailbox
""".strip())

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
                    server_key = spec.get("server_key", "pair")
                    entry = cfg[spec["root_key"]][server_key]
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
            perms_path = write_native_permissions(name)
            if perms_path:
                print("  права:    " + str(perms_path))

        ok, detail = verify(argv)
        print("  проверка: " + ("ОК, pair-mailbox " + detail if ok else "НЕ РАБОТАЕТ — " + detail))
        if not ok:
            failed.append(name)

    # Вложенная сюита (~3 мин) в тестах установщика пропускается через
    # PAIR_INSTALL_SKIP_SUITE: её гоняет verify-слой отдельно, а каждый
    # тестовый install нёс бы её заново (698с на install_test.py целиком).
    if not os.environ.get("PAIR_INSTALL_SKIP_SUITE"):
        print("\nтесты сервера")
        print("  проверка ядра и протокола...", end="", flush=True)
        env = dict(os.environ, PAIR_DISABLE_RING="1")
        # Сюита едет в подменённом HOME (install-тесты изолируются), а PyNaCl
        # живёт в user-site настоящего HOME. Без наследования 2 evidence-теста
        # раунда 42 падают с ModuleNotFoundError: nacl — не кодом, окружением.
        # _site внутри этого процесса тоже видит подменённый HOME, поэтому
        # настоящий user-site вычисляем через pwd, а не через site.
        import site as _site
        import pwd as _pwd
        real_home = _pwd.getpwuid(os.getuid()).pw_dir
        real_site = os.path.join(real_home, ".local", "lib",
                                 f"python{sys.version_info.major}.{sys.version_info.minor}",
                                 "site-packages")
        candidates = set(_site.getsitepackages() + [_site.getusersitepackages()] + [real_site])
        live = [p for p in candidates if p and os.path.isdir(p)]
        if live:
            old = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(live + ([old] if old else []))
        t_res = subprocess.run([sys.executable, str(ROOT / "mcp/pair_mailbox/test_protocol.py")],
                               cwd=str(ROOT), env=env, capture_output=True, text=True)
        if t_res.returncode != 0:
            print(" ОШИБКА")
            print("\nТЕСТЫ НЕ ПРОШЛИ:")
            if t_res.stdout:
                print(t_res.stdout)
            if t_res.stderr:
                print(t_res.stderr)
            return 1
        print(" ОК")

    if failed:
        print("\nНЕ СДЕЛАНО для: " + ", ".join(failed))
        print("разбери эти вручную, остальные работают")
        return 1

    print("\n" + "=" * 70)
    print("✓ pair-mailbox успешно установлен и готов к работе!")
    print("\nБыстрый старт:")
    print("  1. Запустите пару агентов в tmux:")
    print("     ./bin/pairup opencode agy")
    print("  2. В окне любого агента задайте цель:")
    print('     "Работаем вместе над задачей X"')
    print("  3. Веб-доска мониторинга раундов:")
    print("     ./bin/pairboard")
    print("\nДокументация: docs/PROTOCOL.ru.md | docs/PROTOCOL.md")
    print("Автор: Aliaksei Patskevich (AVP-Dev) • https://avpdev.com • https://t.me/AVP_Dev")
    print("GitHub: https://github.com/AVP-Dev/pair-mailbox")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
