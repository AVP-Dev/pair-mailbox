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


def list_stale_files(code_dir: Path | None = None,
                     process_start_ts: float | None = None,
                     tolerance: float = 2.0) -> list[str]:
    """Вернуть список имен *.py файлов, чей mtime новее старта процесса с учетом допуска."""
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    target_dir = code_dir if code_dir is not None else server_code_dir()
    cutoff = start_ts + tolerance
    stale: list[str] = []
    try:
        for p in sorted(target_dir.glob("*.py")):
            try:
                if p.stat().st_mtime > cutoff:
                    stale.append(p.name)
            except OSError:
                continue
    except OSError:
        pass
    return stale


def check_server_freshness(code_dir: Path | None = None,
                           process_start_ts: float | None = None) -> dict[str, Any]:
    """Проверить, свеж ли исполняемый сервер относительно файлов на диске.

    Если mtime файлов исходников новее старта процесса с запасом толерантности
    (например, > 2 секунд), сервер считается устаревшим (stale).
    """
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    newest_mtime, newest_file = latest_code_mtime(code_dir)
    stale_files = list_stale_files(code_dir, start_ts)
    
    # Допуск 2.0 секунды на рассинхрон файловой системы при одновременном старте
    is_fresh = len(stale_files) == 0
    lag_seconds = max(0.0, newest_mtime - start_ts) if not is_fresh else 0.0

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
        "stale_files": stale_files,
        "why": ("сервер свеж" if is_fresh else
                f"код {newest_file} изменён {lag_seconds:.1f}с после старта сервера ({start_iso}) — "
                f"в памяти исполняется старая версия. Требуется перезапуск MCP-сервера.")
    }


def assert_server_fresh(reopening: bool = False, backward: bool = False) -> None:
    """Гейт свежести сервера для tool_pair_advance.

    Откаты назад (backward) разрешены всегда для аварийного спасения,
    за исключением reopening (done -> implement), где запуск нового раунда
    на устаревшем сервере запрещен.
    Любые движения вперед блокируются при stale сервере.
    """
    if backward and not reopening:
        return
    status = check_server_freshness()
    if not status["fresh"]:
        from server import ProtocolError
        files = ", ".join(status["stale_files"]) or status["newest_code_file"]
        action = "переоткрытием раунда" if reopening else "переводом фазы"
        raise ProtocolError(
            f"сервер устарел: {status['why']}",
            f"Файлы с изменениями: {files}. Перезапустите MCP-сервер перед {action}."
        )
