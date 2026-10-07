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

# Угловые глифы НИЖНЕЙ границы поля ввода. Боковые рамки панели используют
# вертикаль `┃`/`│` и в этот набор намеренно не входят: по вертикали панель
# отличить нельзя, по нижнему углу — можно.
INPUT_BOX_CORNERS_LOWER = "\u2579\u258c\u2588"

INPUT_BOX_CORNERS = "\u2579\u257a\u2578\u2576\u257b\u2502\u2503\u2551\u2552"

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
    if text[0] in INPUT_BOX_CORNERS_LOWER:
        return False
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
    """Жива ли панель tmux, и является ли она вообще панелью.

    Строка, которая не похожа на панель, не панель: без этой проверки tmux на
    `-t opencode` (имя процесса, а не ссылка) отвечал кодом 0 и ПУСТЫМ выводом,
    а вердикт «жива» выводился из поля `alive`, которое для такого ответа
    оказывалось правдой. Проверка формы стоит ДО разговора с tmux именно поэтому:
    нельзя отвечать за то, о чём не спросили.

    Правило ровно то же, что в server.pane_exists, и это не совпадение: tmux на
    запрос несуществующей панели отвечает кодом 0 и ПУСТЫМ выводом, поэтому
    проверка «код успеха и не „1“» объявляет живой любую фантомную панель. Раньше
    правка живла только в server.pane_exists, а pairpilot зовёт эту функцию — и
    фантом %999999 считался живым, из-за чего pilot_pane_dead не писался
    никогда. Расхождение двух одинаково названных функций и было дырой.

    Поэтому обе функции зовут pane_identity, а не читают tmux сами: одна мысль —
    одно место, иначе правка одной из них роняет ту самую проверку согласия.
    """
    if not pane_ref_format(pane):
        return False
    try:
        ident = pane_identity(tmux, pane)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(ident["exists"] and ident["alive"])


# Два формата ссылки на панель, оба принимаются всегда: `%N` и
# `<сессия>:<окно>.<панель>`. Второй устойчивее: номер сквозной внутри одного
# tmux-сервера, он перенумеровывается при пересоздании панелей и протухает в
# окружении процесса, а имя сессии с окном и панелью такой срок службы не имеет.
#
# Обратная совместимость не из вежливости: у живых ролей в состоянии лежат `%N`,
# и если бы запись перестала их принимать, обновление сломало бы доставку у всех
# сразу. Поэтому пишется предпочтительный формат, а читаются оба.
PANE_ID_FORMAT = "id"
PANE_ADDRESS_FORMAT = "address"

PANE_ID_RE = re.compile(r"%\d+\Z")
# Имя сессии в tmux не может содержать `:` (проверено: `new-session -s 'a:b'`
# отказывает), а `.` в нём заменяется на `_`. Поэтому двоеточие однозначно
# отделяет имя сессии, а `\d+\.\d+` в хвосте — окно и панель.
PANE_ADDRESS_RE = re.compile(r"(?P<session>[^:]+):(?P<window>\d+)\.(?P<pane>\d+)\Z")


def pane_ref_format(pane: str) -> str:
    """Какой это формат ссылки: `id`, `address` или "" (не панель вовсе).

    Отдельная функция, потому что её результат решает судьбу вызова, а не
    сообщает о факте: `_ring` отказывается звонить в собственную панель по
    сравнению строк, а `pairpilot` отказывается трогать панель, занятую другой
    ролью. Обе проверки обязаны понимать оба формата, иначе после перехода на
    адреса сессий они молча перестают срабатывать.
    """
    text = str(pane or "")
    if PANE_ID_RE.fullmatch(text):
        return PANE_ID_FORMAT
    if PANE_ADDRESS_RE.fullmatch(text):
        return PANE_ADDRESS_FORMAT
    return ""


def session_address(tmux: str, pane: str) -> str:
    """Устойчивый адрес панели: `<сессия>:<окно>.<панель>`, либо "" если не вышло.

    Ограничение, важное и измеренное: имя сессии изолирует проекты ТОЛЬКО если у
    каждого проекта своя сессия. При общей сессии `mail:0.0` может принадлежать
    чужому проекту, и ловится это только проверкой `pane_current_path`. Поэтому
    адрес сессии и проверка пути — две независимые линии, а не замена одна другой.
    """
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane,
         "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True, timeout=5)
    out = r.stdout.strip()
    if r.returncode != 0 or not out or out.startswith(":"):
        return ""
    # ЗАМЕРЕНО, и это ломает наивную реализацию: на несуществующую панель tmux
    # отвечает кодом 0, а не ошибкой, и ПЕРЕИМЕНОВЫВАЕТ запрос на соседнюю живую
    # панель того же окна. Сессия из двух панелей, панель `.1` умерла, и
    # `display-message -t S:0.1` спокойно отдаёт адрес `S:0.0`. На `%N` такого
    # нет: `display-message -t %499999` даёт пустой вывод.
    #
    # Значит адрес нельзя ни принять, ни записать, не сверив его с тем, о чём
    # спрашивали: иначе протухший адрес молча указывает на чужую панель, и
    # недоставленное сообщение уходит соседу вместо отказа.
    if pane_ref_format(pane) == PANE_ADDRESS_FORMAT and out != str(pane):
        return ""
    return out


def canonical_pane(tmux: str, pane: str) -> str:
    """Что записывать в состояние: адрес сессии, если он есть, иначе исходный вход.

    Обратная совместимость здесь обязательна не из вежливости: у живых ролей в
    состоянии лежат `%N`, и если бы запись перестала их принимать, обновление
    сломало бы доставку у всех сразу. Поэтому оба формата принимаются при
    обращении, а пишется предпочтительный.
    """
    address = session_address(tmux, pane)
    return address or pane


def same_pane(tmux: str, left, right) -> bool:
    """Одна и та же панель, записанная двумя форматами. Fail-closed.

    Сравнение строк здесь было тихой поломкой: своя панель из окружения приходит
    как `%1`, а в состоянии лежит `mail:0.1`, и `pane == TMUX_PANE` переставал
    быть верным. Проверки, построенные на этом равенстве, не падали — они просто
    переставали срабатывать: `_ring` начинал звонить в собственную панель, а
    `pairpilot` — считать чужую панель занятой и не трогать её, пока реальный
    сосед в ней сидел.

    Обе ссылки приводятся к адресу сессии: это единственное имя, которое даёт
    tmux обоим форматам. Если адрес не разрешился, остаётся точное сравнение
    строк — два одинаковых адреса сессии всё же одна панель, и отказ здесь
    сломал бы доставку соседу, который сидит рядом.
    """
    if not left or not right:
        return False
    if str(left) == str(right):
        return True
    if not (pane_ref_format(left) and pane_ref_format(right)):
        return False
    left_addr = pane_identity(tmux, left).get("address")
    right_addr = pane_identity(tmux, right).get("address")
    # Пустой адрес означает «панели нет», и две такие ссылки разными панелями не
    # считаются: иначе фантом совпал бы с фантомом.
    return bool(left_addr) and left_addr == right_addr


def pane_key(tmux, pane) -> str:
    """Ключ группировки панелей, одинаковый для обоих форматов ссылки.

    Нужен там, где панели сравниваются попарно или группируются в таблицу: после
    перехода на адреса сессий `%1` и `mail:0.1` — одна панель, а сравнение строк
    видело две. Дальше это ломало `ambiguous()` у сверхвизора, и тот, не увидев
    двух ролей на одной панели, считал её свободной и бил в чужой композер.

    tmux недоступен или панели нет — исходная строка: ключ группировки, а не
    приговор, и хуже, чем было, от этого не станет.
    """
    if not pane or not tmux:
        return str(pane or "")
    try:
        address = pane_identity(tmux, pane).get("address")
    except (OSError, subprocess.SubprocessError):
        return str(pane)
    return address or str(pane)


def pane_identity(tmux: str, pane: str) -> dict:
    """Кто именно сидит в панели: не «жива ли», а «наша ли».

    Одного `#{pane_dead}` мало, и это была дыра между проектами. Номер панели
    `%N` переживает смерть процесса, который его занимал: панель жива, но
    принадлежит уже другому проекту. На этой машине несколько проектов на
    pair-mailbox, поэтому звонок по записанному в состоянии номеру попадал в
    чужой композер, и один проект вбивал свои сообщения в агента другого.

    Один запрос tmux отвечает сразу на всё, и это не экономия ради экономии:
    `#{pane_dead}` и `#{session_name}` берутся из одного снимка панели. Два
    отдельных вызова означали бы два разных момента времени, между которыми
    панель могла умереть, и вердикт «жива, наша» собрался бы из двух снимков
    разных панелей.
    """
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane,
         "#{pane_dead}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_id}\t#{pane_id}"],
        capture_output=True, text=True, timeout=5)
    # tmux на НЕСУЩЕСТВУЮЩУЮ панель отвечает кодом 0 и пустым выводом — это
    # ровно тот же факт, из-за которого фантомная панель когда-то считалась
    # живой. Поэтому одного кода возврата мало: пустой вывод тоже означает,
    # что панели нет. Раньше exists выводился только из returncode и врал:
    # итог был безопасным случайно, а не по замыслу.
    #
    # Пустое ПЛАТОЕ поле cwd остаётся непустым ответом: `pane_current_path` может
    # быть пустым у живой панели, и это «не доказано», а не «отсутствует» —
    # разные отказы с разными причинами, и путать их нельзя.
    parts = r.stdout.strip("\n").split("\t")
    dead = parts[0].strip() if parts else ""
    if r.returncode != 0 or not dead:
        return {"exists": False, "alive": False, "path": "", "address": ""}
    path = parts[1].strip() if len(parts) > 1 else ""
    address = parts[2].strip() if len(parts) > 2 else ""
    # Тот же обман, что в session_address, только теперь он бьёт по линии
    # «наша ли панель»: протухший адрес сессии отдаёт cwd и alive СОСЕДНЕЙ панели,
    # и проверка пути прошла бы, печать пошла бы в чужой композер. Сверяем адрес
    # с тем, о чём спрашивали, и при расхождении панели просто нет.
    if address and pane_ref_format(pane) == PANE_ADDRESS_FORMAT and address != str(pane):
        return {"exists": False, "alive": False, "path": "", "address": ""}
    if not address:
        address = session_address(tmux, pane)
    extra = parts[3:]
    return {"exists": True, "alive": dead in ("0", "false"), "path": path,
            "address": address, "session_id": extra[0].strip() if len(extra) > 0 else "",
            "pane_uid": extra[1].strip() if len(extra) > 1 else ""}


def _ancestor_chain(pid: int) -> set:
    """Цепочка предков pid по /proc — общий корень обоих поисков панели.

    Пустое множество вместо [""] : различие «не знаю» и «знаю, что нет»
    здесь не нужно, оба означают «панель не найдена». pid<=1 отпадает сам:
    у init нет предков в панелях, и цикл не стартует.
    """
    chain: set = set()
    try:
        me = int(pid)
    except (TypeError, ValueError):
        return chain
    while me > 1 and me not in chain:
        chain.add(me)
        try:
            stat = Path(f"/proc/{me}/stat").read_text()
            me = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return chain


def find_pane_of_pid(tmux: str, pid: int) -> str:
    """Панель, в чьём дереве живёт pid — или "".

    Обратный путь: цепочка предков pid до pane_pid одной из живых панелей.
    Нужен дважды: роль ищет себя без TMUX_PANE (find_own_pane), пилот ищет
    чужую роль по pid из ростера. Вне Linux или без панели — честный "".
    """
    chain = _ancestor_chain(pid)
    if not chain:
        return ""
    try:
        out = subprocess.run([tmux, "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) in chain:
            return parts[0]
    return ""


def find_own_pane(tmux: str) -> str:
    """Панель, из чьего дерева процессов зовут — или "".

    TMUX_PANE в окружении MCP-сервера может не быть (наследует от CLI), а
    ростер без панели делает deliver/unblock слепыми: роль жива, а будить
    некуда. Тонкий вызов find_pane_of_pid на своём pid.
    """
    return find_pane_of_pid(tmux, os.getpid())


def pane_is_foreign(tmux: str, pane: str, project_root) -> tuple:
    """Панель доказуемо не наша? Fail-closed: не доказали своё — не печатаем.

    Логика уехала в `panes`: там же гонка при рождении панели и разделение
    «чужой проект» против «панель мертва». Здесь остаётся вызов, потому что
    `pair_deliver` и `pairctl` зовут это имя, а переименовывать его в четырёх
    местах ради одного переезда незачем.

    Импорт ленивый по той же причине, что и остальные здесь: `panes` берёт из
    `composer` `pane_identity`, и импорт на верхнем уровне был бы циклом.
    """
    from panes import pane_is_foreign as impl

    return impl(tmux, pane, project_root)


def pane_belongs_to_project(tmux: str, pane: str, project_root) -> tuple:
    """Принадлежит ли панель этому проекту. Логика — в `panes`."""
    from panes import pane_belongs_to_project as impl

    return impl(tmux, pane, project_root)


def pane_pid(tmux: str, pane: str) -> int:
    """pid ПРОЦЕССА ПАНЕЛИ — обычно это шелл, а не агент. Не «pid роли».

    Возвращать его и называть «pid процесса, который занимает панель» было бы
    верно только формально. Измерено на живых панелях: `#{pane_pid}` в панели с
    работающим агентом отдаёт bash, потому что агент живёт ребёнком шелла.
    Отсюда две беды, и обе приходят от того, что поле называли не тем, чем оно
    являлось:

    - в ростер, где лежит pid сервера роли, попадал pid шелла, и значения
      начинали флипать каждый тик, потому что `_register` перезаписывал своё;
    - хуже: упавший агент оставляет после себя ЖИВОЙ шелл, поэтому «в панели
      кто-то есть» всегда истинно, и роль, которая упала, никогда не
      перезапускалась бы.

    Для вопроса «жива ли роль» есть `pane_role_pid`. Эта функция остаётся как
    нижний слой: её честное имя — процесс панели, и больше она ничего не значит.
    """
    ident = pane_identity(tmux, pane)
    if not (ident["exists"] and ident["alive"]):
        return 0
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane, "#{pane_pid}"],
        capture_output=True, text=True, timeout=5)
    out = r.stdout.strip()
    if r.returncode != 0 or not out.isdigit():
        return 0
    pid = int(out)
    return pid if pid > 0 else 0


def _cmdline(pid: int) -> list:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_role_server(pid: int, role: str) -> bool:
    """Процесс это `server.py --role <role>`, или нет.

    Сверяются и имя файла, и соседние аргументы. Искать просто подстроку роли
    нельзя: ошибка здесь стоила бы чужого pid в чужой записи, а это ровно тот тип
    поломки, который проверка пути уже ловила между проектами.
    """
    args = _cmdline(pid)
    if not any(part.endswith("server.py") for part in args):
        return False
    for index, part in enumerate(args[:-1]):
        if part == "--role" and args[index + 1] == role:
            return True
    return False


def _descendants(root: int) -> list:
    """Все потомки процесса по `/proc`, в ширину.

    Порядок важен: ближайший предок идёт первым, и при нескольких кандидатах
    выигрывает тот, кто ближе к панели, а не тот, у кого pid больше.
    """
    children: dict = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, frontier = [], [root]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            out.append(child)
            frontier.append(child)
    return out


def find_role_pid(root: int, role: str) -> int:
    """Сервер роли среди потомков `root`, или 0.

    Отделено от `pane_role_pid` намеренно: tmux здесь нужен только чтобы узнать
    pid панели, а весь вопрос «кто в панели» решается по `/proc`. Разделение
    сделано ради проверяемости — сращивать их было бы ровно тем, из-за чего баг
    прожил: функция проверялась подменой, которая не влияла на код под ней.
    """
    if not root or not role:
        return 0
    for candidate in _descendants(root):
        if _is_role_server(candidate, role):
            return candidate
    return 0


def pane_role_pid(tmux: str, pane: str, role: str) -> int:
    """pid сервера РОЛИ, если её процесс живёт в этой панели, иначе 0.

    Это тот вопрос, который на самом деле нужен сверхвизору, и он не равен
    `pane_pid`: в панели всегда есть шелл, даже когда агент упал, поэтому «в
    панели кто-то есть» не доказывает «роль жива».

    Искать надо сервер роли, потому что именно его pid пишет `_register`
    (`os.getpid()` сервера) и именно его ждёт `is_pid_alive` в ростере. Отсюда
    смысл: обновление записи возвращает в неё значение того же рода, иначе pid
    двух разных процессов дрались бы за одно поле.

    Один запрос к tmux на всё: сведения о панели берутся из `pane_identity`, а не
    из второго `display-message`. Панель могла умереть между двумя вызовами, и
    тогда вердикт собрался бы из снимков разных панелей.

    Ноль означает «не нашёл», и это не ошибка: вызывающий обязан трактовать это
    как «не знаю», а не как «панели нет». Вне Linux дерева процессов нет, и
    честный ответ здесь 0.
    """
    return find_role_pid(pane_pid(tmux, pane), role)


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

    Три формы панели, и ошибка в любой из них молча отключает доставку целиком.

    **Голый шелл.** Последняя непустая строка и есть промпт.

    **TUI со строкой статуса ПОД композером.** Композер — последняя строка,
    начинающаяся с маркера промпта, всё ниже неё — обстановка.

    **TUI с полем ввода-рамкой** (opencode). Маркера промпта нет вообще: поле
    ввода это рамка, под ней строка статуса. Раньше композер искался только как
    «строка с маркером», поэтому такая панель не опознавалась никогда, функция
    отдавала None, а None по контракту вызывающего это «занято» — доставка и
    звонок в opencode были невозможны в принципе, а не изредка.

    Приоритет именно такой: рамка поля ввода сильнее строки-маркера. TUI рисует
    свёрнутый вызов инструмента как `› pair.pair_send (...)`, а `›` состоит в
    PROMPT_MARKERS, поэтому такая строка выглядит как промпт. Но композером она
    не является, и из-за неё панель opencode считалась занятой всегда, даже когда
    в неё только что положили текст.

    None означает «композер не виден», и это не то же самое, что «свободен»:
    панель из одной рамки печатать некуда, и сообщить о ней как о свободной
    значит отправить keystrokes в никуда. Вызывающий обязан считать None занятой.
    """
    # Геометрия opencode снизу вверх: строка пути/статуса, над ней нижняя
    # граница поля ввода (угол `╹`/`▌`), над ней боковые рамки. Хвост после
    # границы — обстановка, а не контент: пропускаем строки статуса и чистые
    # рамки, пока не встретим границу. Всё остальное внизу — не поле ввода.
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        text = stripped.lstrip()
        if text and text[0] in INPUT_BOX_CORNERS_LOWER:
            return line
        if looks_like_status_line(stripped):
            continue
        if text and text[0] in INPUT_BOX_CORNERS and is_input_box_border(stripped):
            continue
        if text.startswith("~/") or text.startswith("/"):
            continue
        break

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

    # Маркера нет вовсе. Тогда это либо чистая рамка TUI, где печатать некуда
    # (None, то есть занят), либо незнакомый вид панели — голый шелл, — где
    # последняя строка и есть промпт, как было до всех этих правок.
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


def composer_overflowed(lines: list) -> bool:
    """Свёрнут ли длинный ввод в «↑ N more lines».

    Проверка доставки ищет ХВОСТ набранного текста в строке композера. Когда текст
    длиннее видимой области, TUI сворачивает его, и хвоста на экране нет — проверка
    даёт ложный отказ при тексте, который на экране есть.

    Наблюдалось вживую: 4259 символов доставки легли в композер соседа, композер
    показал «↑ 14 more lines», доставка была объявлена неподтверждённой, сообщение
    продублировалось в ящик — и человек увидел одно и то же дважды.
    """
    line = composer_line(lines)
    if line is None:
        return False
    return bool(re.search(r"\u2191\s*\d+|more lines?", line))


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
    """Did the typed text actually appear on screen?

    The check is on the HEAD, not the tail, and это не переименование ради
    красоты. Измерено в своём же композере: текст длиннее экрана рисуется от
    начала, а всё, что ниже, уходит за нижнюю границу и в `capture` не попадает
    вовсе. Проверка хвоста для такого текста не может сработать никогда — она
    и не срабатывала, поэтому доставка длинных сообщений всякий раз объявлялась
    неподтверждённой, хотя текст доходил и сосед его выполнял.

    Старая версия искала хвост по другой причине: курсор стоит внизу, а напечатанное
    остаётся наверху, поэтому «последние строки» — это пустое. Но и «начало» там
    не искалось — просто не оказалось нужным, пока тексты были короткими.

    Проверяется начало, потому что оно единственное гарантированно видно. Хвост
    как дополнительный признак тоже годится: если текст поместился целиком, то
    видно и то, и другое, и совпадение по хвосту подтверждает доставку сильнее.
    """
    meaningful = [line for line in lines if line.strip()]
    joined = " ".join(" ".join(meaningful).split())
    head = " ".join(needle[:60].split())
    tail = " ".join(needle[-60:].split())
    if head and head in joined:
        return True
    return bool(tail) and tail in joined


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
    r">\s*/dev/(?!null\b)",
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
    # Деструкция системы. Внутри проекта rm -rf build/ — обычная уборка, и её
    # нельзя запрещать вместе с rm -rf /: разница не в команде, а в цели.
    # /tmp — именованные одноуровневые подпути без глобов (уборка своих
    # времянок): вырез ниже сужает гард ровно на них, голый /tmp, /tmp/*
    # и rm -rf / остаются refuse и проверяются тестами.
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:~|\$HOME|\*|\.\s*$|\.\.)",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?!tmp/[A-Za-z0-9_.-]+(?:\s|$))",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/tmp(?:\s|$|/\*|/\.?$)",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/tmp/[A-Za-z0-9_.-]+/.+",
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
    # Свои гейты через bash: verify-project, file-size-gate, eye-of-sauron,
    # comment-lint, security-scan и *_test.sh. Относительность в самом паттерне:
    # `scripts/` без `/` и `..` впереди — абсолютный путь и выход из проекта
    # не матчатся здесь, а не только в тестах. Задача seq 329: рутинные прогоны
    # требовали ручного Allow, хотя это та же работа, что pytest напрямую.
    r"\bbash\s+scripts/(?!/)(?![^\s]*\.\.)[A-Za-z0-9_.-]+\.sh\b",
    # Наблюдение без записи: чтение состояния, а не изменение (§12 не задевают).
    # Чистое наблюдение — без редиректа (>), без -i, без -o: printf > /etc,
    # sed -i /etc, sort -o /etc — это запись вне проекта, а не наблюдение.
    # awk — refuse (тот же класс, что python3 -c: system()/pipe исполняют);
    # trap/export — refuse (безопасность в значении, матчем не отличить).
    r"\b(?:date|du|wc|sleep|mktemp|which|command|echo|uptime|uniq|cut|tr|diff)\b",
    r"\b(?:mkdir|touch)\s+(?:-[a-zA-Z]+\s+)*(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s-]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\b(?:printf|sort)\b(?![^\n]*\s*>+\s*/(?!tmp/|dev/null))(?![^\n]*\s-o\s*/(?!tmp/))(?![^\n]*>+/(?!tmp/|dev/null))",
    r"\bsed\b(?![^\n]*-i[^\n]*\s/(?!tmp/))(?![^\n]*-i/(?!tmp/))",
    r"\bgit\s+check-ignore\b",
    r"\bpython3\s+--version\b",
    # Файловые операции внутри проекта и /tmp: cp/mv/ln, rm файла
    # без флагов, rm -rf одноуровневых именованных подпутей /tmp без глобов.
    # Голый /tmp, /tmp/* и rm -rf / остаются refuse (см. NEVER выше).
    # touch/mkdir — выше, в скоупе observe (те же границы).
    r"\b(?:cp|mv|ln)\s+(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\brm\s+(?:(?![/~$*./-])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\brm\s+-[a-zA-Z]+\s+(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+)",
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
    if composer_overflowed(capture(tmux, pane)):
        # Текст на экране, просто хвост свёрнут. Объявлять «не доставил» здесь
        # нельзя: это и есть двойной след — сообщение в ящике И текст в композере
        # одновременно, и именно это путает человека.
        result = {"delivered": True, "chars": len(body),
                  "chunks": len(chunks(body)), "pane": pane, "submitted": submit,
                  "tail_hidden": True}
        if submit:
            subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                           capture_output=True, text=True, timeout=5)
        return result
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
