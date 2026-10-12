---
title: untracked-файлы в review.dirty_files приводили к вечному stale вердикту и вытеснению head_files
found: 2026-10-12
severity: medium
round: 61
resolved_in_round: 61
status: resolved
---

## Где

- `mcp/pair_mailbox/review.py`:
  - `dirty_files(root: Path, include_untracked: bool = False) -> list[str]` (`mcp/pair_mailbox/review.py:48-73`).
  - `snapshot(root: Path) -> dict[str, Any]` (`mcp/pair_mailbox/review.py:101-115`).
  - `is_stale(old, current_head, changed_since, current_dirty)` (`mcp/pair_mailbox/review.py:118-175`).
- `mcp/pair_mailbox/review_gate.py`:
  - `check_pr_merge(store: Any, repo_dir: Path)` (`mcp/pair_mailbox/review_gate.py:127-140`).

## Что это

1. **Вечный stale вердикта из-за скретч/бэклог-файлов**:
   Ранее функция `review.dirty_files` включала untracked-файлы (префикс `??` из `git status --porcelain`).
   Если агент создавал новую карточку бэклога или скретч-заметку перед отправкой на ревью, этот файл попадал в `dirty` вердикта. После коммита этого файла (или создания новых untracked файлов) `dirty` менялся, и `is_stale` объявлял вердикт устаревшим: `затронуты те же файлы: docs/backlog/...`.
2. **Вытеснение файлов коммита из снимка**:
   В `snapshot(root)` вычисление файлов производилось как `files = dirty or head_files(root, head)`. Любой временный untracked-файл приводил к тому, что `files` содержал только этот скретч-файл, а весь список файлов из коммита `head` терялся. В результате вердикт терял coverage.

## Решение в раунде 61

1. **Параметр `include_untracked` в `dirty_files`**:
   По умолчанию `dirty_files(root, include_untracked=False)` фильтрует строки `??` и отслеживает только реальные tracked-изменения (`M`, `D`, `A`, `R`).
2. **Объединение coverage в `snapshot`**:
   `snapshot()` объединяет `head_files ∪ tracked_dirty`, чтобы файлы коммита никогда не вытеснялись при наличии dirty файлов.
3. **Защита пути слияния в `check_pr_merge`**:
   `check_pr_merge` явно вызывает `dirty_files(repo_dir, include_untracked=True)` (исключая внутреннюю директорию заметок `store.notes_root` / `.pair`), гарантируя, что PR не сливается при наличии незакоммиченных изменений любого типа.
