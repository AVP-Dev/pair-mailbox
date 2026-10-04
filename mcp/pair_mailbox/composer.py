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

# Живая реплика прокручивается; окно снимка берём с запасом.
CAPTURE_LINES = 40

# Сколько байт за одну вставку. У умпутуна 197: при 1627 байтах он ещё терял окна
# по 1022 на 1663-м. Чуть меньше — надёжнее.
CHUNK_BYTES = 170

# Что считаем приглашением ввести текст. Универсально: последняя строка, где
# после маркера пусто или почти пусто.
PROMPT_MARKERS = (">", "\u203a", "$", "#", "::", "ask>")


class ComposerBusy(Exception):
    pass


def _tmux() -> str:
    exe = shutil.which("tmux")
    if not exe:
        raise ComposerBusy("tmux не найден в PATH")
    return exe


def pane_alive(tmux: str, pane: str) -> bool:
    r = subprocess.run([tmux, "display-message", "-p", "-t", pane, "#{pane_dead}"],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and r.stdout.strip() != "1"


def capture(tmux: str, pane: str) -> list:
    r = subprocess.run([tmux, "capture-pane", "-p", "-t", pane],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return []
    lines = r.stdout.splitlines()
    return lines[-CAPTURE_LINES:]


def is_footer_or_status_line(line: str) -> bool:
    """Проверка, является ли строка служебным футером или рамкой TUI."""
    s = line.strip()
    if not s:
        return True
    # Чистые горизонтальные разделители и рамки (─, ━, ═, ▀, ▄, ╹, etc.)
    if re.fullmatch(r"[\u2500-\u259f\s═─━╹▀_-]+", s):
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
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if fallback is None:
            fallback = stripped
        for marker in PROMPT_MARKERS:
            if stripped.lstrip().startswith(marker):
                return stripped
    # Маркера нет вовсе. Тогда это либо чистая рамка TUI, где печатать некуда
    # (None, то есть занят), либо незнакомый вид панели — голый шелл, — где
    # последняя строка и есть промпт, как было до всех этих правок.
    return None if is_footer_or_status_line(fallback or "") else fallback


def is_composer_empty(line: str) -> bool:
    """Грубая, но честная проверка: после маркера почти ничего нет."""
    s = line.rstrip()
    if not s.strip():
        return True
    # Если строка — пустая рамка TUI вида '  ┃  ' или '  │  '
    if re.fullmatch(r"[\s\u2500-\u257f|]+", s):
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


def resolve_permission_prompt(tmux: str, pane: str) -> str | None:
    """Check if the pane is stalled waiting for a permission confirmation and confirm it.

    Returns the prompt text that was approved, or None.
    """
    if not pane_alive(tmux, pane):
        return None
    lines = capture(tmux, pane)
    prompt = detect_permission_prompt(lines)
    if not prompt:
        return None

    line = composer_line(lines)
    # Нет композера — значит и печатать некому: молча подтверждать нельзя.
    if line is None:
        return None
    if not detect_permission_prompt([line]) and not is_composer_empty(line):
        return None

    subprocess.run([tmux, "send-keys", "-t", pane, "y"],
                   capture_output=True, text=True, timeout=5)
    subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                   capture_output=True, text=True, timeout=5)
    return prompt


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

    time.sleep(0.35)
    after = capture(tmux, pane)
    if not tail_present(after, body):
        written = len(body)
        erase(tmux, pane, written)
        return {"delivered": False, "why": "вставка не появилась в панели, откатил",
                "erased": written}

    result = {"delivered": True, "chars": len(body), "chunks": len(chunks(body)),
              "pane": pane, "submitted": submit}
    if submit:
        subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
    return result
