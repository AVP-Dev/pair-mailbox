"""Сборка страницы дашборда: снимок + ассеты -> один самодостаточный HTML.

Почему всё в один файл. Дашборд открывается двойным щелчком, и человек тащит его
на другую машину, кладёт в мессенджер, открывает с флешки. Всё, что страница
подгружает по пути, перестаёт работать в момент, когда перестаёт совпадать путь, а
`file://` к тому же запрещает `fetch()`. Поэтому CSS и JS вставляются внутрь
документа текстом, и на выходе остаётся ровно один файл без единой внешней ссылки.
Инлайн — не про экономию запросов, а про переносимость.

Ассеты лежат отдельными файлами (`assets/board.css`, `assets/board.js`), а не
строками в этом модуле. Причина та же, что и везде в проекте: строка с разметкой
в Python-файле не проверяется ни гейтом размеров, ни глазами ревьюера как код,
который можно сломать.

`refresh_seconds` идёт в `<meta http-equiv="refresh">` и делает страницу живой без
сервера: генератор перезаписывает файл, открытая страница перезагружается сама.
Ноль означает «не обновлять», и это разные режимы, а не один с флагом. То же число
дублируется в `data-watch-seconds` на `<body>`: пауза автообновления в клиенте
создаёт этот тег заново и не должна при этом выдумывать период.

Ничего не отдаётся наружу: ни одного обращения к сети, ни одного `fetch`. Проверяется
тестом `test_board_render.py`.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from board_snapshot import snapshot_json

ASSETS = Path(__file__).resolve().parent / "assets"
CSS_FILE = ASSETS / "board.css"
JS_FILE = ASSETS / "board.js"
LABELS_FILE = ASSETS / "labels.js"

# Ассеты в порядке выполнения: словарь обязан появиться раньше клиента, который
# читает его при загрузке. Порядок задан здесь, а не в шаблоне, иначе правка
# шаблона тихо ломает страницу, которая на этот раз загрузилась.
JS_ORDER = (LABELS_FILE, JS_FILE)

# Ассеты не должны молча превратиться в пустые: страница без CSS читается, но
# выглядит как отчёт, и потерянный `board.css` выглядел бы как «дашборд такой».
REQUIRED_TOKENS = ("--background", "--foreground", "--card", "--muted",
                   "--border", "--destructive", "--primary")


class AssetError(RuntimeError):
    """Ассета нет или он не выполняет свою часть контракта."""


def load_asset(path: Path, required: tuple[str, ...] = ()) -> str:
    """Прочитать ассет, проверив, что он на месте и не пустой.

    `required` — обязательные куски содержимого. Для CSS это токены из
    `docs/theme-tokens.md`: страница, потерявшая `--border`, не сломается молча, а
    просто станет другой, и заметить это можно только глазами. Проверка здесь
    превращает такую правку в ошибку сборки.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AssetError(f"ассет недоступен: {path}: {error}") from error
    if not text.strip():
        raise AssetError(f"ассет пустой: {path}")
    missing = [token for token in required if token not in text]
    if missing:
        raise AssetError(f"{path.name}: нет обязательных токенов: {', '.join(missing)}")
    return text


def render_page(snapshot: dict[str, Any], *, refresh_seconds: int = 0,
                css: str | None = None, js: str | None = None) -> str:
    """Собрать страницу. Данные встраиваются как JSON в `type="application/json"`.

    Текст идёт через `json.dumps` с последующим экранированием `<`, `>`, `&` в
    `snapshot_json`, а не через `html.escape` внутри JSON: HTML-экранирование
    превратило бы `&` в `&amp;`, и после разбора JSON в тексте задачи осталась бы
    именно сущность, а не амперсанд.

    `css` и `js` переопределяются текстом — это нужно тесту, который собирает
    страницу целиком из маленьких заглушек и проверяет саму сборку. Пустая строка
    считается переопределением, а не «прочитать ассет», иначе тест подставил бы
    пустоту вместо проверки и прошёл бы, ничего не собрав.
    """
    css_text = load_asset(CSS_FILE, REQUIRED_TOKENS) if css is None else css
    scripts = "".join(load_asset(path) for path in JS_ORDER) if js is None else js
    data = snapshot_json(snapshot)
    info = snapshot.get("round") or {}
    title = f"{snapshot.get('project') or 'пара'} — раунд {info.get('number', '?')}"
    meta = ""
    if refresh_seconds > 0:
        meta = f'\n<meta http-equiv="refresh" content="{int(refresh_seconds)}">'

    return f"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{meta}
<style>
{css_text}
</style>
</head>
<body data-watch-seconds="{int(refresh_seconds)}">
<header class="bar">
  <h1 id="project"></h1>
  <div class="bar-right">
    <span id="stamp" class="muted"></span>
    <button type="button" id="watch-toggle" class="btn" aria-pressed="false">пауза</button>
    <button type="button" id="theme-toggle" class="btn" aria-pressed="false">светлая</button>
  </div>
</header>
<main id="app" aria-live="polite"></main>
<noscript>
  <p class="muted">Дашборд рисует снимок состояния на JavaScript. Без него видна
  только эта строка — включите JS, чтобы увидеть состояние пары.</p>
</noscript>
<script id="snapshot" type="application/json">{data}</script>
<script>
{scripts}
</script>
</body>
</html>
"""


def write_page(target: Path, snapshot: dict[str, Any], *, refresh_seconds: int = 0) -> Path:
    """Записать страницу целиком, через временный файл и переименование.

    Атомарность здесь не про красоту: дашборд открыт в браузере в этот же момент, и
    обычная запись обрезает файл на середине, после чего страница показывает
    половину снимка и выглядит сломанной сама собой. Переименование либо видно
    целиком, либо не видно вовсе.
    """
    target = Path(target)
    page = render_page(snapshot, refresh_seconds=refresh_seconds)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(target)
    return target