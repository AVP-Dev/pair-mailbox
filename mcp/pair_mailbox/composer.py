#!/usr/bin/env python3
"""Вбивает сообщение в композер соседнего агента и проверяет, что оно дошло.

Это перенос подхода umputun/agterm на tmux. Он нужен, когда агент не просто должен
проснуться, а когда сообщение должно прийти как обычная реплика в его окно.

Почему это не «просто отправить клавиши»:

- композер может быть занят. Если сосед что-то печатает, вбивка сожёт его текст;
- длинный текст ломается при вставке целиком — как у него, режем на куски;
- вставка может не сработать. Тогда нужен откат, иначе в промпте останется мусор.

Поэтому: снимок → проверка «свободен» → кусками → сверка хвоста → Enter или откат.
"""
import os
import re
import shutil
import subprocess
import time
import json
from pathlib import Path

# Живая реплика прокручивается; окно снимка берём с запасом.
CAPTURE_LINES = 40

# Сколько байт за одну вставку. У умпутуна 197: при 1627 байтах он ещё терял окна
# по 1022 на 1663-м. Чуть меньше — надёжнее.
CHUNK_BYTES = 170

# Сколько раз перечитывать панель, прежде чем признать вставку неподтверждённой.
# Замерено, а не выбрано: в обычном шелле текст появляется в capture через 0.05с,
# а в тяжёлом TUI (Gemini в панели 30x70) одиночный BSpace отражается около 2с.
# Окно 8 x 0.4 = 3.2с перекрывает худший случай с запасом. Одно чтение через
# 0.35с после ввода — это чаще устаревший кадр, чем отсутствие вставки.
VERIFY_ATTEMPTS = 8
VERIFY_PAUSE = 0.4

# Сколько строк над вопросом берём в вердикт. На Claude Code команда стоит
# предпоследней строкой, поэтому нужен минимум одна; взято две с запасом, потому
# что у разных CLI между командой и вопросом иногда есть ещё и рамка.
OVERSCAN_LINES = 3

# Что считаем приглашением ввести текст. Универсально: последняя строка, где
# после маркера пусто или почти пусто.
PROMPT_MARKERS = (">", "\u203a", "$", "#", "::", "ask>")


def looks_like_status_line(line: str) -> bool:
    """Строка статуса TUI распознаётся структурно, без названий брендов.

    Правка на марках брендов не держится: первая же смена темы возвращает исходный
    баг. Единственный устойчивый признак — `$`, за которым сразу идёт значение, то
    есть цена: `$0.00`. Настоящий промпт на `$` заканчивается знаком, как
    `user@host:~$`, и знака в конце строки быть не должно — иначе перестаёт
    работать доставка в обычный шелл.
    """
    text = line.strip()
    if not text:
        return True
    if re.fullmatch(r"[\u2500-\u259f\s═─━╹▀_-]+", text):
        return True
    if text.startswith("$"):
        return False
    if "$" not in text:
        return False
    return any(char.isdigit() for char in text.split("$", 1)[1][:3])


class ComposerBusy(Exception):
    pass


def _tmux() -> str:
    exe = shutil.which("tmux")
    if not exe:
        raise ComposerBusy("tmux не найден в PATH")
    return exe


def pane_alive(tmux: str, pane: str) -> bool:
    """Жива ли панель tmux.

    Правило ровно то же, что в server.pane_exists, и это не совпадение: tmux на
    запрос несуществующей панели отвечает кодом 0 и ПУСТЫМ выводом, поэтому
    проверка «код успеха и не „1“» объявляет живой любую фантомную панель. Раньше
    правка живла только в server.pane_exists, а pairpilot зовёт эту функцию — и
    фантом %999999 считался живым, из-за чего pilot_pane_dead не писался
    никогда. Расхождение двух одинаково названных функций и было дырой.
    """
    r = subprocess.run([tmux, "display-message", "-p", "-t", pane, "#{pane_dead}"],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and r.stdout.strip() in ("0", "false")


def pane_identity(tmux: str, pane: str) -> dict:
    """Кто именно сидит в панели: не «жива ли», а «наша ли».

    Одного `#{pane_dead}` мало, и это была дыра между проектами. Номер панели
    `%N` переживает смерть процесса, который его занимал: панель жива, но
    принадлежит уже другому проекту. На этой машине несколько проектов на
    pair-mailbox, поэтому звонок по записанному в состоянии номеру попадал в
    чужой композер, и один проект вбивал свои сообщения в агента другого.
    """
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane,
         "#{pane_dead}\t#{pane_current_path}"],
        capture_output=True, text=True, timeout=5)
    # tmux на НЕСУЩЕСТВУЮЩУЮ панель отвечает кодом 0 и пустым выводом — это
    # ровно тот же факт, из-за которого фантомная панель когда-то считалась
    # живой. Поэтому одного кода возврата мало: пустой вывод тоже означает,
    # что панели нет. Раньше exists выводился только из returncode и врал:
    # итог был безопасным случайно, а не по замыслу.
    parts = r.stdout.strip().split("\t") if r.stdout.strip() else []
    dead = parts[0].strip() if parts else ""
    if r.returncode != 0 or not dead:
        return {"exists": False, "alive": False, "path": ""}
    path = parts[1].strip() if len(parts) > 1 else ""
    return {"exists": True, "alive": dead in ("0", "false"), "path": path}


def pane_is_foreign(tmux: str, pane: str, project_root) -> tuple:
    """Панель доказуемо не наша? Fail-closed: не доказали своё — не печатаем.

    Возвращает (True, причина) если в панель печатать нельзя, и (False, "") если
    можно. Пустой cwd не доказывает ничего, поэтому трактуется как «чужая»:
    молча отказаться от доставки правильнее, чем вбить текст не туда.

    Строгость намеренная и вторая по важности после fail-closed: панель
    принимается, только если её cwd совпадает с корнем проекта или лежит внутри
    него. Панель, запущенная из ОБЩЕГО родителя вроде ~/work, отклоняется, хотя
    формально это родитель проекта. Иначе такую панель объявили бы своей сразу
    несколько проектов на этой машине, и межпроектная инъекция вернулась бы
    ровно тем же путём, который здесь закрывается.
    """
    ident = pane_identity(tmux, pane)
    if not ident["exists"]:
        return True, "панель отсутствует"
    if not ident["alive"]:
        return True, "панель мертва"

    try:
        ours = Path(project_root).resolve()
    except OSError as exc:
        return True, f"не резолвится корень проекта: {exc}"
    raw = ident["path"]
    if not raw:
        return True, "tmux не сообщил cwd панели, идентичность не доказана"
    try:
        theirs = Path(raw).resolve()
    except OSError as exc:
        return True, f"не резолвится cwd панели: {exc}"

    if theirs == ours or ours in theirs.parents:
        return False, ""
    return True, f"панель принадлежит другому проекту: {theirs}"


def capture(tmux: str, pane: str) -> list:
    r = subprocess.run([tmux, "capture-pane", "-p", "-t", pane],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return []
    lines = r.stdout.splitlines()
    return lines[-CAPTURE_LINES:]


# Рамки TUI: box-drawing и блочные глифы. Одна константа намеренно: вопрос
# «это часть рамки?» задавался в двух местах двумя разными диапазонами —
# 2500-259f в одном и 2500-257f в другом, — и разница в один символ была
# ровно тем, что делало занятый композер пустым. Тот же урок, что и с
# расхождением pane_alive / server.pane_exists: одна мысль, одно место.
BOX_GLYPH = r"\u2500-\u259f"


# Угловые глифы рамки поля ввода: вертикаль и повороты. Строка поля ввода
# начинается с одного из них и у пустой рамки, и у рамки с набранным текстом.
INPUT_BOX_CORNERS = "\u2579\u257a\u2578\u2576\u257b\u2502\u2503\u2551\u2552"


def is_input_box_row(line: str) -> bool:
    """Строка поля ввода TUI — и пустая, и с набранным текстом.

    Различать их здесь не нужно: нужно знать, что строка принадлежит полю ввода,
    а занятость определяет is_composer_empty. Раньше распознавание требовало, чтобы
    строка целиком состояла из рамочных глифов, и как только в поле ввода
    появлялся хоть один символ, composer_line отдавал None, то есть «печатать
    некуда»: начал человек печатать — доставка снова невозможна.
    """
    text = line.lstrip()
    if not text:
        return False
    return text[0] in INPUT_BOX_CORNERS


def is_input_box_border(line: str) -> bool:
    """Нижняя граница поля ввода TUI: рамка, которая расширяется при наборе.

    Признак структурный, без названий брендов: строка состоит только из
    рамочных и блочных глифов. Нужна потому, что у OpenCode нет ASCII-маркера
    промпта: поле ввода — рамка, а под ней строка статуса. Композер искался
    только как «строка, начинающаяся с маркера», поэтому панель OpenCode не
    опознавалась никогда и молча считалась занятой — доставка и звонок в неё
    были невозможны в принципе, а не изредка.
    """
    text = line.rstrip()
    if not text.strip():
        return False
    if re.search(r"[^\s" + BOX_GLYPH + r"|]", text):
        return False
    return True


def is_footer_or_status_line(line: str) -> bool:
    """Проверка, является ли строка служебным футером или рамкой TUI."""
    s = line.strip()
    if not s:
        return True
    # Чистые горизонтальные разделители и рамки (─, ━, ═, ▀, ▄, ╹, etc.)
    if re.fullmatch(rf"[{BOX_GLYPH}\s═─━╹▀_-]+", s):
        return True
    return False


def composer_line(lines: list) -> str | None:
    """Последняя строка, куда человек может печатать, или None если композера нет.

    Две формы панели, и ошибка здесь молча отключает доставку целиком:
    голый шелл, где последняя непустая строка и есть промпт, и TUI, который
    рисует строку статуса ПОД своим композером.

    Второй случай стоил нам ночи: строка статуса agy содержит вертикальную
    черту из PROMPT_MARKERS и хвост в десятки символов, поэтому «последняя
    непустая строка» объявляла СВОБОДНЫЙ композер занятым, и pair_deliver
    к соседу отказывал навсегда.

    Ловить футер по маркерам бренда (имя модели, эмодзи) не держится: первая
    же смена темы возвращает исходный баг. Поэтому единственное правило, которое
    работает, структурное: композер — это последняя строка, НАЧИНАЮЩАЯСЯ с
    маркера промпта, а всё, что ниже неё, — обстановка.

    None означает «композер не виден» и это не то же самое, что «свободен»:
    панель из одной рамки печатать некуда, и сообщить о ней как о свободной
    значит отправить keystrokes в никуда. Вызывающий обязан считать None занятой.
    """
    fallback = None
    above = None
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if fallback is None:
            fallback = stripped
        elif above is None:
            above = stripped
        for marker in PROMPT_MARKERS:
            if stripped.lstrip().startswith(marker):
                return stripped

    # Маркера нет вовсе. Здесь раньше была дыра, стоившая нам доставки в OpenCode
    # полностью: его поле ввода не начинается ни с одного маркера промпта, это
    # рамка, а под ней строка статуса. Последняя непустая строка — статус, он и
    # опознаётся как статус, и функция возвращала None, то есть «композера нет».
    # Вызывающий обязан считать None занятой, поэтому панель OpenCode была
    # непроходимо занята всегда, и ни звонок, ни deliver в неё не проходили.
    #
    # Поэтому рамка, под которой лежит строка статуса, и есть композер. Проверка
    # структурная и брендонезависимая: рамка + статус снизу.
    # Поле ввода идёт ABOVE статуса, поэтому в обратном обходе `above` — это
    # строка поля ввода, а `fallback` (последняя непустая строка) — статус под
    # ней. Берётся строка поля ввода целиком, а не только пустая рамка: с
    # набранным текстом она перестаёт быть «рамочной» в строгом смысле, но
    # остаётся тем же полем ввода — просто занятым.
    if above is not None and is_input_box_row(above) \
            and looks_like_status_line(fallback or ""):
        return above

    # Ни маркера, ни пары «рамка над статусом». Тогда это либо чистая рамка TUI,
    # где печатать некуда (None, то есть занят), либо незнакомый вид панели —
    # голый шелл, — где последняя строка и есть промпт, как было до всех правок.
    return None if is_footer_or_status_line(fallback or "") else (
        None if looks_like_status_line(fallback or "") else fallback)


def is_composer_empty(line: str) -> bool:
    """Грубая, но честная проверка: после маркера почти ничего нет."""
    s = line.rstrip()
    if not s.strip():
        return True
    # Пустая рамка TUI: '  ┃  ', '  │  ' или нижняя граница поля ввода OpenCode.
    # Диапазон здесь намеренно BOX_GLYPH, а не прежние 2500-257f: ▀ — это 2580, он
    # на единицу выше той границы, поэтому рамка поля ввода не проходила и
    # пустой композер OpenCode читался как занятый. Проверка живёт в
    # is_input_box_border, отдельного дубликата здесь нет намеренно: две
    # одинаковые проверки в двух местах — это ровно тот класс расхождения,
    # который уже стоил нам неработающей доставки.
    if is_input_box_border(s):
        return True
    for marker in PROMPT_MARKERS:
        idx = s.rfind(marker)
        if idx != -1:
            tail = s[idx + len(marker):].strip()
            # рамка TUI, рамки-разделители, курсор
            tail = re.sub(r"[\u2500-\u257f\u2588|]", "", tail).strip()
            return len(tail) <= 2
    return False


def chunks(text: str, size: int = CHUNK_BYTES) -> list:
    """Режем по границам слов, чтобы не разорвать идентификаторы."""
    out, buf = [], ""
    for word in text.split():
        candidate = (buf + " " + word) if buf else word
        if len(candidate.encode()) > size and buf:
            out.append(buf)
            buf = word
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def type_text(tmux: str, pane: str, text: str) -> None:
    """Type in chunks, restoring the separator that chunking consumed.

    chunks() joins words with a space and drops it at every boundary, so sending the
    pieces back to back loses one space per chunk: "должна найтись" arrived as
    "должнанайтись". Harmless in prose, corrupting in a patch, so put it back.
    """
    parts = chunks(text)
    for index, chunk in enumerate(parts):
        piece = chunk + " " if index < len(parts) - 1 else chunk
        subprocess.run([tmux, "send-keys", "-t", pane, "-l", piece],
                       capture_output=True, text=True, timeout=5)
        time.sleep(0.04)


def erase(tmux: str, pane: str, count: int) -> None:
    if count <= 0:
        return
    r = subprocess.run([tmux, "send-keys", "-t", pane, "-N", str(count), "BSpace"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        batch = 50
        remaining = count
        while remaining > 0:
            cur = min(remaining, batch)
            subprocess.run([tmux, "send-keys", "-t", pane] + ["BSpace"] * cur,
                           capture_output=True, text=True, timeout=5)
            remaining -= cur
            time.sleep(0.01)


def tail_present(lines: list, needle: str) -> bool:
    """Was the typed text actually on screen?

    Two things bite here, and both bit in practice:

    - A pane is taller than its content. The cursor sits at the bottom while the
      prompt, and the text just typed into it, stay near the top. So the last ten
      *physical* lines are usually blank. Look at the last non-empty lines instead.
    - A narrow pane wraps the text, so the tail of the message straddles a row
      boundary and never appears whole in one line. Join before comparing.
    """
    meaningful = [line for line in lines if line.strip()]
    joined = " ".join(" ".join(meaningful[-6:]).split())
    tail = " ".join(needle[-60:].split())
    return tail in joined


PERMISSION_PATTERNS = (
    r"\[[yY]/[nN]\]",
    r"\([yY]/[nN]\)",
    r"\[[yY]/[nN]/[a-zA-Z]+\]",
    r"\[yes/no\]",
    r"\b(?:Allow|Approve)\s+(?:execution\b|tool\b|bash\b|command\b|this\b)",
    r"\bDo you want to proceed\?",
    r"\bDo you allow this tool call\?",
)

# Промпт-меню: TUI показывает не «[y/N]», а список вариантов с подсказкой про
# выбор и подтверждение. Клавиши там другие, и «y» не значит согласие.
PERMISSION_MENU_MARKERS = (
    r"allow\s+once",
    r"always\s+allow",
    r"enter\s+confirm",
    r"(?:⇆|⇄|\btab\b)\s*select",
)
PERMISSION_HEADER = re.compile(r"permission\s+(?:required|needed)", re.IGNORECASE)


def detect_permission_menu(lines: list) -> str | None:
    """Промпт в виде меню, а не «y/N». Вернёт строку-заголовок или None.

    Отдельная функция вместо расширения detect_permission_prompt: тот смотрит
    только на две последние непустые строки, а в меню заголовок «Permission
    required» стоит на шесть строк выше самых нижних. Расширять его окно значило
    бы ловить эту строку в обычном выводе, где она ничего не разрешает.

    Нужен и заголовок, и признаки меню: по одному признаку это ещё не промпт,
    а по одному заголовку — обычный лог.
    """
    text = [line.strip() for line in lines if line.strip()]
    joined = "\n".join(text)
    if not PERMISSION_HEADER.search(joined):
        return None
    if not any(re.search(marker, joined, re.IGNORECASE) for marker in PERMISSION_MENU_MARKERS):
        return None
    for line in reversed(text):
        if PERMISSION_HEADER.search(line):
            return line
    return joined


def extract_permission_context(lines: list, prompt: str) -> str:
    """Текст, по которому выносится вердикт.

    Для меню нужен весь экран: команда, которую видно как «$ git status», живёт в
    теле модалки, а не в заголовке. По одному заголовке «Permission required» белый
    список не разрешит ничего и откажет всегда.

    Для «y/N» раньше хватало самой строки вопроса, и на Claude Code это было
    ошибкой: там вопрос идёт последней строкой, а команда — предпоследней, так
    что вердикт выносился по строке без единого слова о команде и отказывал
    100% безопасных команд. То есть режим auto на Claude Code просто не работал.

    Поэтому берём и строку вопроса, и строки над ней — но не весь экран: лишний
    текст мог бы разрешиться по слову, которое к команде отношения не имеет.
    Верхняя граница — OVERSCAN_LINES строк, и это решение открытое: agy спросил,
    две, три или до разделителя, и я выбрал три с запасом на рамку между
    командой и вопросом.
    """
    text = [line.strip() for line in lines if line.strip()]
    joined = "\n".join(text)
    if any(re.search(marker, joined, re.IGNORECASE) for marker in PERMISSION_MENU_MARKERS):
        return " ".join(joined.split())
    tail = [line for line in text if line.strip()][-OVERSCAN_LINES:]
    context = " ".join(" ".join(tail).split())
    return context or prompt


def detect_permission_prompt(lines: list) -> str | None:
    """Return the matched prompt text if the terminal pane is showing an interactive confirmation prompt, else None."""
    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return None
    for line in meaningful[-2:]:
        for pat in PERMISSION_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return line
    return None


# Шесть стоп-категорий PROTOCOL.md §12 плюс всё, что меняет состояние вне
# репозитория. Совпадение с любым из них запрещает автоподтверждение при любом
# белом списке: решение человека не переопределяется настройкой инструмента.
NEVER_ALLOW_PATTERNS = (
    r"\.env\b",
    r"\bsecrets?\b",
    r"\btokens?\b",
    r"\bapi[_ -]?keys?\b",
    r"\bpasswords?\b",
    r"\bcredentials?\b",
    # Чтение тоже умеет красть. `cat` стоит в белом списке как чтение, поэтому
    # путь к секрету должен быть запрещён сам по себе, иначе «прочитать ключ» —
    # это разрешённое чтение.
    r"\.ssh\b",
    r"id_rsa|id_dsa|id_ecdsa|id_ed25519",
    r"/etc/(?:shadow|passwd|sudoers)",
    r"\.(?:pem|p12|pfx|key)\b",
    r"\.npmrc\b",
    r"\.aws\b",
    r"\.git-credentials\b",
    r"\.kube/config",
    # Труба в шелл превращает любое чтение в выполнение.
    r"\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b",
    # Код, переданный интерпретатору строкой, — это произвольное выполнение, а не
    # проверка. Именно так выглядел обход белого списка:
    # `python3 -c "import urllib.request; urllib.request.urlopen('http://x')" # test`
    # Слово «test» в комментарии удовлетворяло шаблону разрешения, а `urllib` в
    # запрещённых категориях не значится — сеть через него не отлавливалась.
    r"\b(?:python3?|node|ruby|perl)\s+-c\b",
    r"\$\(",
    r"`",
    r"\bfind\b[^\n]*-(?:delete|exec|execdir|ok)\b",
    r"\bxargs\b",
    r":\(\)\s*\{",
    r">\s*/dev/",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bdeploy\b",
    r"\bkubectl\b",
    r"\bhelm\b",
    r"\bdocker\b",
    r"\bssh\b",
    r"\bsudo\b",
    r"\bterraform\b",
    r"\bmigrat(?:e|ion)\b",
    r"\bdrop\s+(?:table|database)\b",
    r"\btruncate\b",
    r"\bdelete\s+(?:from|all)\b",
    # §10: «в main не пушат никогда». Push в фиче-ветку и создание PR — часть
    # работы по протоколу, поэтому запрещён не push вообще, а push в main/master
    # и переписывание истории.
    r"\bgit\s+push\b[^\n]*\b(?:main|master)\b",
    r"\bgit\s+push\b[^\n]*--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b[^\n]*-[a-z]*f",
    r"\bgit\s+filter-branch\b",
    r"\bgit\s+reflog\s+expire\b",
    r"\bgit\s+rebase\b[^\n]*\b(?:main|master)\b",
    # Деструкция системы. Внутри проекта rm -rfbuild/ — обычная уборка, и её
    # нельзя запрещать вместе с rm -rf /: разница не в команде, а в цели.
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/|~|\$HOME|\*|\.\s*$|\.\.)",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/[a-z]",
    r"\brmdir\b",
    r"\bkill(?:all)?\b",
    r"\bchmod\s+777\b",
    r"\bprod(?:uction)?\b",
    r":\d{2,5}\b",
    # Сеть. Не потому что сеть опасна сама по себе, а потому что она позволяет
    # вынести наружу то, что §12 защищает, и скачать то, что не проверял никто.
    r"\bcurl\b",
    r"\bwget\b",
    r"\bnc\b\s+-",
    # Установка пакетов тянет чужой код в проект и меняет окружение оператора.
    r"\bpip[23]?\s+install\b",
    r"\bnpm\s+install\b",
    r"\b(?:gem|cargo|go\s+get)\s+(?:install|add|get)\b",
    r"\bnpm\s+publish\b",
)

# Белый список по умолчанию: чтение плюс локальная работа внутри проекта.
#
# Границу провели там, где она проходит по §10 и §12, а не там, где строго:
# §10 требует коммитов и PR, поэтому «только чтение» парализует пару, а §12
# запрещает не «работу», а конкретные вещи — секреты, деплой, миграции,
# удаление данных и истории git, прод. Запрещённое остаётся запрещённым при любом
# белом списке (см. NEVER_ALLOW_PATTERNS), а разрешённое здесь — это работа, которую
# протокол и так обязан делать без человека.
#
# Чего здесь нет и почему: сетевых вызовов (curl/wget) и `ssh`. «Позволить сеть» —
# это не про чтение файлов, это про возможность вынести их наружу, а §12 про секреты
# не делает исключений на «только чтение».
DEFAULT_ALLOW_PATTERNS = (
    # чтение
    r"\bRead\b",
    r"\bread_file\b",
    r"\bview\b",
    r"\bglob\b",
    r"\bgrep\b",
    r"\bsearch\b",
    r"\blist(?:_dir|ing)?\b",
    r"\bcat\b",
    r"\bhead\b",
    r"\btail\b",
    r"\bls\b",
    r"\bfind\b",
    # правка внутри проекта
    r"\bwrite\b",
    r"\bedit\b",
    r"\bcreate\b",
    r"\breplace\b",
    r"\bpatch\b",
    r"\bapply\b",
    r"\bformat\b",
    r"\blint\b",
    # локальные проверки: тесты и сборки
    r"\b(?:pytest|unittest|nose|tox|nox|jest|vitest|mocha|rspec|cargo\s+test|go\s+test)\b",
    r"\b(?:ruff|flake8|pylint|mypy|pyright|eslint|prettier|tsc|black|isort)\b",
    # Интерпретатор с проверкой в команде: `python3 -m pytest`,
    # `python3 mcp/pair_mailbox/test_protocol.py`, `node --test`. Ключевое слово
    # должно быть АРГУМЕНТОМ. Прежний `[^\n]*` позволял ему всплыть откуда угодно
    # дальше по строке, и хватало слова «test» в комментарии или внутри строковой
    # константы — см. NEVER_ALLOW_PATTERNS про `-c`.
    r"\b(?:python3?|node|ruby)\s+(?:-\S+\s+)*(?:[^\s]*[_./-]?)(?:test|tests|pytest|unittest|lint|check|build)",
    r"\bmake\b",
    r"\bnpm\s+(?:run\s+\w+|test|ci)\b",
    r"\bbuild\b",
    # Уборка артефактов внутри проекта — обычная часть работы. Цель обязана быть
    # относительной: `rm -rf /`, `rm -rf ~` и `rm -rf .` отсеяны запретами выше,
    # а эти lookahead'ы не дают им пройти второй раз. `./dist` — цель
    # относительная, поэтому проходит.
    r"\brm\s+-[a-zA-Z]+\s+(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s]+",
    # локальный git: §10 требует ветку и коммит, поэтому это разрешено
    r"\bgit\s+(?:status|diff|log|show|branch|remote|describe|rev-parse|add|commit|"
    r"checkout|switch|stash|tag|blame|shortlog|reflog)\b",
    # отправка работы: push в фиче-ветку и PR. Запрет push в main/master и
    # --force стоит в NEVER_ALLOW_PATTERNS и проверяется первым.
    r"\bgit\s+push\b",
    r"\bgh\s+(?:pr|issue|run|workflow)\b",
)


def load_allow_patterns(spec: str | None) -> tuple[list[str], bool]:
    """Прочитать белый список разрешений: JSON-файл или список шаблонов.

    Возвращает (шаблоны, работали_ли_мы_на_дефолтных).

    Раньше здесь стояло «пусто = не подтверждать ничего», и это привело к тому, что
    сверхвизор не подтверждал вообще ничего: файла `.pair/permissions.json` не
    существует, путь к нему превращался в единственный шаблон '.pair/permissions.json',
    он не находился в тексте промпта — и каждый запрос получал отказ. Снаружи это
    выглядело как «сосед перестал подтверждать».

    Теперь граница та, что задал человек: внутри проекта можно всё, что нужно для
    работы, запрещено то, что вредит системе, чужому проекту или необратимо стирает.
    Это ровно DEFAULT_ALLOW_PATTERNS плюс NEVER_ALLOW_PATTERNS поверх, который
    проверяется первым. Отсутствие файла означает «работай на списке по умолчанию»,
    а не «не подтверждать ничего»; файл по-прежнему сужает и расширяет.

    Явный пустой список — это осознанный отказ от автоподтверждения, и он
    отличается от отсутствующего файла. Нечитаемый файл даёт пустой: сломанная
    настройка не должна молча расширять права.
    """
    if spec is None:
        return list(DEFAULT_ALLOW_PATTERNS), True
    if not str(spec).strip():
        # Пустая строка — это осознанный отказ от автоподтверждения, и он отличается
        # от отсутствующего файла. Разница видна только в этой функции, поэтому
        # различаем здесь, а не в вызывающем коде.
        return [], False
    text = str(spec).strip()
    try:
        candidate = Path(text).expanduser()
    except (OSError, ValueError):
        return [], True
    if candidate.is_file():
        try:
            raw: object = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Файл есть и он сломан. Молча брать дефолты здесь нельзя: оператор
            # явно что-то настраивал, и тихо откатывать его настройку к дефолту —
            # хуже, чем остановиться и сказать.
            return [], False
    else:
        # Строка без запятых и без существующего файла. Раньше она становилась
        # шаблоном целиком, и это молча ломало автоподтверждение; теперь это
        # читается как список из одного элемента только если в строке есть запятые.
        parts = [part.strip() for part in text.split(",") if part.strip()]
        # Путь к несуществующему файлу — это тоже «настройки нет», а не шаблон.
        # Раньше именно он становился единственным шаблоном ('.pair/permissions.json')
        # и не находился в тексте промпта, то есть автоподтверждение не работало
        # никогда. Список через запятую — настоящая настройка и читается как список.
        if len(parts) == 1 and ("/" in parts[0] or "\\" in parts[0]):
            return list(DEFAULT_ALLOW_PATTERNS), True
        return parts, False
    if isinstance(raw, dict):
        raw = raw.get("allow") or []
    if not isinstance(raw, list):
        return [], False
    return [str(item) for item in raw if str(item).strip()], False


def permission_verdict(prompt: str, allow: list[str] | None = None) -> dict:
    """Можно ли сверхвизору без человека нажать «y» на этом промпте.

    Default-deny, и запрет проверяется раньше разрешения. Цена двух ошибок не
    симметрична: зависший агент стоит одной ночи, а подтверждённая опасная
    команда уничтожает то, что нельзя вернуть.

    Отказ возвращается с причиной: его читает человек, а необъяснённое «нет»
    неотличимо от сломанного сверхвизора.

    Известное ограничение, о котором честнее сказать, чем сделать вид: решение
    принимается по тексту строки, а не по разбору команды. Строка может содержать
    безобидное слово рядом с опасным — «Read» в описании задачи. Отсюда три
    правила: белый список по умолчанию состоит только из чтения, запреты
    проверяются первыми и не отменяются настройкой, а совпадение записывается в
    транскрипт вместе с самой строкой — по журналу видно, на что именно было
    сказано «да».
    """
    patterns = DEFAULT_ALLOW_PATTERNS if allow is None else tuple(allow)
    for pattern in NEVER_ALLOW_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return {"verdict": "refuse", "matched": pattern,
                    "why": f"запрещённая категория (§12): {pattern}"}
    for pattern in patterns:
        if re.search(pattern, prompt, re.IGNORECASE):
            return {"verdict": "allow", "matched": pattern, "why": "в белом списке"}
    return {"verdict": "refuse", "matched": None,
            "why": "не в белом списке разрешений"}


def resolve_permission_prompt(tmux: str, pane: str, allow: list[str] | None = None) -> dict:
    """Check if the pane is stalled waiting for a permission confirmation and confirm it.

    Returns a verdict dict: {approved, verdict, prompt, why, matched}. A refusal is
    a first-class outcome, not an error: the pane is still blocked, and somebody
    must be told that the supervisor deliberately left it that way.
    """
    idle = {"approved": False, "verdict": "none", "prompt": "", "why": "", "matched": None}
    if not pane_alive(tmux, pane):
        return {**idle, "why": "панель мертва"}
    lines = capture(tmux, pane)
    menu = detect_permission_menu(lines)
    prompt = menu or detect_permission_prompt(lines)
    if not prompt:
        return idle

    if menu:
        # В меню нет строки ввода, и жать «y» там бессмысленно: у этого TUI
        # «y» не значит согласие, а подтверждение — Enter по выбранному пункту.
        # Проверять композер нечего: его там просто нет, и это нормально.
        context = extract_permission_context(lines, prompt)
    else:
        line = composer_line(lines)
        # Нет композера — значит и печатать некому: молча подтверждать нельзя.
        if line is None:
            return {**idle, "prompt": prompt, "verdict": "refuse",
                    "why": "композера не видно, подтверждать нечем"}
        if not detect_permission_prompt([line]) and not is_composer_empty(line):
            return {**idle, "prompt": prompt, "verdict": "refuse",
                    "why": "строка ввода занята своим текстом"}
        context = extract_permission_context(lines, prompt)

    decision = permission_verdict(context, allow)
    if decision["verdict"] != "allow":
        return {"approved": False, "verdict": "refuse", "prompt": prompt,
                "why": decision["why"], "matched": decision["matched"]}

    if menu:
        subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
    else:
        subprocess.run([tmux, "send-keys", "-t", pane, "y"],
                       capture_output=True, text=True, timeout=5)
        subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
    return {"approved": True, "verdict": "allow", "prompt": prompt,
            "why": decision["why"], "matched": decision["matched"],
            "menu": bool(menu)}


def deliver(pane: str, text: str, wait_for_empty: float = 0.0, submit: bool = True) -> dict:
    """Доставить текст в композер соседа. Возвращает отчёт, что реально вышло.

    Бросает ComposerBusy, если композер занят — вбивать поверх чужого текста
    нельзя, это потеряет то, что человек печатает.
    """
    tmux = _tmux()
    if not pane_alive(tmux, pane):
        raise ComposerBusy("панель мертва или отсутствует")

    deadline = time.monotonic() + max(0.0, wait_for_empty)
    while True:
        line = composer_line(capture(tmux, pane))
        # None — композер не виден вовсе. Это не «свободно»: печатать некуда,
        # и отправлять клавиши мимо композера нельзя.
        if line is not None and is_composer_empty(line):
            break
        if time.monotonic() >= deadline:
            seen = line.strip()[-60:] if line else "композер не виден"
            raise ComposerBusy("композер занят: " + (seen or "?"))
        time.sleep(0.5)

    body = " ".join(text.split())
    type_text(tmux, pane, body)

    # Проверка вставки несколькими чтениями, а не одним.
    landed = False
    for attempt in range(VERIFY_ATTEMPTS):
        if tail_present(capture(tmux, pane), body):
            landed = True
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_PAUSE)
    if not landed:
        # Не подтверждено — значит НЕИЗВЕСТНО, дошёл ли текст, а не «не дошёл».
        # Стирать вслепую нельзя: erase(len(body)) не знает, чей это ввод, и
        # съедает текст соседа или человека, печатавшего в этой же панели. Прежний
        # откат на одном устаревшем кадре именно это и делал — и рапортовал
        # «откатил» при тексте, который лежал на экране.
        #
        # Цена ошибки несимметрична: лишнее сообщение в ящике стоит одного взгляда,
        # стёртый ввод соседа — его работы. Поэтому сомнение передаётся дальше, и
        # вызывающий уходит в ящик, где сообщение точно будет прочитано.
        return {"delivered": False, "unverified": True, "chars": len(body),
                "pane": pane, "submitted": submit,
                "why": "вставка не подтвердилась на экране; композер мог остаться с текстом, "
                       "откат вслепую не делаю — забери сообщение из ящика"}

    result = {"delivered": True, "chars": len(body), "chunks": len(chunks(body)),
              "pane": pane, "submitted": submit}
    if submit:
        subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
    return result
