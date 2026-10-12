"""Проверка свежести рантайма MCP-сервера относительно кода на диске.

Зачем: серверы pair-mailbox долгоживущие (запускаются при старте tmux-сессии).
Если файлы `mcp/pair_mailbox/*.py` или другие ключевые модули обновлены в репозитории
(например, добавлены новые гейты plan_gate / intent_gate / review_gate),
процесс сервера продолжает исполнять старый байт-код из памяти. В результате
все раунды после коммита проходят мимо машинных гейтов (инцидент раунда 63, notes/finding.md).

Модуль вынесен из `server.py` по правилу размера: монолит заморожен
(scripts/file-size-baseline.tsv), и логика свежести изолирована в отдельный компонент.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Время старта текущего процесса Python: инициализируется при загрузке модуля
_PROCESS_START_TIME: float = time.time()
try:
    _proc_stat = Path(f"/proc/{os.getpid()}")
    if _proc_stat.is_dir():
        _PROCESS_START_TIME = _proc_stat.stat().st_mtime
except (OSError, ValueError):
    pass


def get_process_start_time() -> float:
    """Вернуть метку времени (epoch seconds) старта текущего процесса."""
    return _PROCESS_START_TIME


def server_code_dir() -> Path:
    """Каталог исходников сервера mcp/pair_mailbox."""
    return Path(__file__).resolve().parent


def latest_code_mtime(code_dir: Path | None = None) -> tuple[float, str]:
    """Найти максимальный mtime среди файлов исходников *.py в каталоге сервера.

    Возвращает (max_mtime, path_of_newest_file).
    """
    target_dir = code_dir if code_dir is not None else server_code_dir()
    newest_mtime = 0.0
    newest_file = ""
    try:
        for p in target_dir.glob("*.py"):
            try:
                st = p.stat()
                if st.st_mtime > newest_mtime:
                    newest_mtime = st.st_mtime
                    newest_file = p.name
            except OSError:
                continue
    except OSError:
        pass
    return newest_mtime, newest_file


def check_server_freshness(code_dir: Path | None = None,
                           process_start_ts: float | None = None) -> dict[str, Any]:
    """Проверить, свеж ли исполняемый сервер относительно файлов на диске.

    Если mtime файлов исходников новее старта процесса с запасом толерантности
    (например, > 2 секунд), сервер считается устаревшим (stale).
    """
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    newest_mtime, newest_file = latest_code_mtime(code_dir)
    
    # Допуск 2.0 секунды на рассинхрон файловой системы при одновременном старте
    is_fresh = True
    lag_seconds = 0.0
    if newest_mtime > (start_ts + 2.0):
        is_fresh = False
        lag_seconds = newest_mtime - start_ts

    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    newest_iso = (datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
                  if newest_mtime > 0 else "")

    return {
        "fresh": is_fresh,
        "started_at": start_iso,
        "started_ts": start_ts,
        "newest_code_file": newest_file,
        "newest_code_at": newest_iso,
        "newest_code_ts": newest_mtime,
        "lag_seconds": lag_seconds,
        "why": ("сервер свеж" if is_fresh else
                f"код {newest_file} изменён {lag_seconds:.1f}с после старта сервера ({start_iso}) — "
                f"в памяти исполняется старая версия. Требуется перезапуск MCP-сервера.")
    }
