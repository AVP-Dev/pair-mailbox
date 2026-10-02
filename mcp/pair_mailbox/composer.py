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
PROMPT_MARKERS = (">", ">", "\u203a", "$", "#", "::", "ask>", "│")


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


def composer_line(lines: list) -> str:
    """Последняя строка, где пользователь может печатать."""
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        return stripped
    return ""


def is_composer_empty(line: str) -> bool:
    """Грубая, но честная проверка: после маркера почти ничего нет."""
    s = line.rstrip()
    if not s.strip():
        return True
    for marker in PROMPT_MARKERS:
        idx = s.find(marker)
        if idx != -1:
            tail = s[idx + len(marker):].strip()
            # рамка TUI, рамки-разделители, курсор
            tail = re.sub(r"[\u2500-\u257f\u2588]", "", tail).strip()
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
    for chunk in chunks(text):
        subprocess.run([tmux, "send-keys", "-t", pane, "-l", chunk],
                       capture_output=True, text=True, timeout=5)
        time.sleep(0.04)


def erase(tmux: str, pane: str, count: int) -> None:
    for _ in range(count):
        subprocess.run([tmux, "send-keys", "-t", pane, "BSpace"],
                       capture_output=True, text=True, timeout=5)
        time.sleep(0.01)


def tail_present(lines: list, needle: str) -> bool:
    tail = needle[-60:]
    return any(tail in line for line in lines[-6:])


def deliver(pane: str, text: str, wait_for_empty: float = 0.0) -> dict:
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
        if is_composer_empty(line):
            break
        if time.monotonic() >= deadline:
            raise ComposerBusy("композер занят: " + (line.strip()[-60:] or "?"))
        time.sleep(0.5)

    body = " ".join(text.split())
    type_text(tmux, pane, body)

    time.sleep(0.35)
    after = capture(tmux, pane)
    if not tail_present(after, body):
        written = len(body.encode())
        erase(tmux, pane, written)
        return {"delivered": False, "why": "вставка не появилась в панели, откатил",
                "erased": written}

    subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                   capture_output=True, text=True, timeout=5)
    return {"delivered": True, "chars": len(body), "chunks": len(chunks(body)),
            "pane": pane}
