"""Тесты сборки страницы (`board_render.py`) и её ассетов.

Здесь проверяется не «красиво ли», а четыре вещи, на которых страница держится или
на которых она молча ломается.

1. **Переносимость.** Собранный файл открывается двойным щелчком: ни одной внешней
   ссылки, ни одного `fetch`, ни одного `XMLHttpRequest`. Проверка ловит саму
   вероятную поломку этой схемы: кто-то добавит `<link>` к внешнему CSS «для
   скорости», и страница будет работать у него и не работать ни у кого.
2. **Данные не могут закрыть страницу.** Текст из журнала встраивается как JSON, и
   `</script>` внутри письма обязан остаться текстом. Считается не «есть ли
   подстрока», а разбирается ли результат и совпадает ли значение.
3. **Инвариант токенов.** `docs/theme-tokens.md` требует «ноль жёстких цветов
   вне токенов», а «требует» без проверки — это пожелание. Проверяется и
   отсутствие `#hex`/`rgb()`, и то, что каждый `var(--x)` действительно объявлен:
   опечатка в имени токена иначе даёт прозрачный фон и тихую поломку.
4. **Никакого разбора HTML из данных.** `innerHTML` в клиенте означал бы, что
   текст задачи может выполнить код на машине человека.

Ассеты проверяются настоящие: подставив заглушки в `render_page`, легко получить
зелёный набор, который ничего не проверял, поэтому заглушки используются только
там, где проверяется сама сборка.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from board_render import (CSS_FILE, SCRIPT_END_TERMINATORS, JS_ORDER, JS_FILE,
                          LABELS_FILE, REQUIRED_TOKENS, AssetError, breaks_out_of_script,
                          load_asset, render_page, write_page)
from board_snapshot import build_snapshot
from pair_test_base import PairTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SNAPSHOT_RE = re.compile(
    r'<script id="snapshot" type="application/json">(.*?)</script>', re.S)

# Закрывающая последовательность script по спецификации HTML5: "</script", после
# которого идёт tab, LF, FF, пробел, "/" или ">". Регулярное выражение с прогнозом,
# потому что сам lookahead и есть часть правила, а не украшение.
SCRIPT_END_RE = re.compile(r"</script(?=[\t\n\f />])", re.I)


def _role_line(tree: str, role: str) -> str:
    """Строка роли в распечатанном дереве.

    Дерево печатается построчно с отступом, и закрывающих тегов в нём нет: конец
    строки роли — это первая следующая строка с отступом не глубже, чем у самой
    строки роли. Ищется по имени роли внутри блока, а не «первым вхождением
    буквы»: буква встречается и в заголовке панели, и в слове writer на другой
    строке, и такой разбор проверял бы не то место, о котором думает.
    """
    lines = tree.splitlines()
    for position, line in enumerate(lines):
        if f'class=role-name "{role}"' not in line:
            continue
        indent = len(line) - len(line.lstrip())
        collected = []
        for follow in lines[position:]:
            depth = len(follow) - len(follow.lstrip())
            if follow.strip() and depth < indent:
                break
            collected.append(follow)
        return "\n".join(collected)
    raise AssertionError(f"роль {role!r} не найдена в дереве:\n{tree}")


def _script_blocks_per_html5(page: str) -> list[str]:
    """Содержимое блоков `<script>` так, как их увидит браузер.

    Именно эта функция повторяет правило из спецификации, а не полагается на
    разбор HTML-парсером: разрывы и незакрытые блоки — ровно тот дефект, который
    здесь ловится, и он обязан быть виден в тесте, а не в браузере человека.
    """
    blocks: list[str] = []
    position = 0
    while True:
        start = page.find("<script>", position)
        if start < 0:
            return blocks
        start += len("<script>")
        match = SCRIPT_END_RE.search(page, start)
        if not match:
            return blocks
        blocks.append(page[start:match.start()])
        position = match.end()

# Глобалы, которые вызываются, но не объявляются в файле.
BUILTINS = {"isNaN", "parseInt", "parseFloat", "setTimeout", "setInterval",
            "String", "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date"}
# Слова, которые выглядят как вызовы, но вызовами не являются.
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof",
            "await", "else", "do", "try"}


def _stub_snapshot(**overrides) -> dict:
    base = {"generated_at": "2026-01-01T00:00:00+00:00", "project": "demo",
            "read_error": "", "watch": False, "stale_after": 90,
            "round": {"number": 3, "phase": "implement", "phases": ["scoping", "plan",
                      "implement", "verify", "done"], "phase_index": 3, "mode": "fix",
                      "autonomy": "auto", "writer": "a", "writer_reason": "",
                      "task": "задача", "state_age": "1 с", "state_age_seconds": 1,
                      "event_age": "1 с", "event_age_seconds": 1, "closed": False},
            "roles": [{"role": "a", "is_writer": True, "pane": "%0", "pane_alive": True,
                       "pane_note": "", "pid": 1, "pid_alive": True, "silence": "1 с",
                       "silence_seconds": 1, "restored_by": ""}],
            "pilot": {"running": False, "pid": None, "pids": [], "log_size": 0,
                      "log_age": "1 с", "log_age_seconds": 1, "report_exists": False,
                      "report_age": "—", "report_age_seconds": None, "refusals": 0,
                      "last": "", "last_ts": None},
            "tasks": [], "events": [],
            "counts": {"events_shown": 0, "events_total": 0, "tasks_open": 0, "roles": 1}}
    base.update(overrides)
    return base


class AssembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _stub_snapshot()
        # Свой каталог для двух тестов, которые пишут ассет на диск: поднимать
        # ради них `Store` было бы приспособлением без нужды.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_page_is_one_self_contained_file(self):
        page = render_page(self.snapshot)
        self.assertTrue(page.startswith("<!doctype html>"))
        for tag in ("<style>", "application/json", "</html>"):
            self.assertIn(tag, page)

    def test_no_external_references_at_all(self):
        page = render_page(self.snapshot)
        # Ни src, ни href на внешний ресурс: с `file://` страница не переживёт
        # ни одного из них, и поломка выглядит как «у меня пустая страница».
        for match in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', page):
            self.assertFalse(match.startswith(("http", "//", "./", "/")),
                             f"внешняя ссылка: {match}")
        self.assertNotIn("<link", page)

    def test_data_does_not_reach_the_network(self):
        page = render_page(self.snapshot)
        self.assertNotRegex(page, r"\bfetch\s*\(")
        self.assertNotIn("XMLHttpRequest", page)
        self.assertNotIn("EventSource", page)
        self.assertNotIn("import(", page)

    def test_snapshot_is_embedded_as_parsable_json(self):
        page = render_page(self.snapshot)
        raw = SNAPSHOT_RE.search(page).group(1)
        self.assertEqual(json.loads(raw)["round"]["number"], 3)

    def test_closing_tag_in_data_stays_text(self):
        hostile = _stub_snapshot()
        hostile["round"] = dict(hostile["round"], task='</script><img src=x onerror=alert(1)>')
        page = render_page(hostile)
        raw = SNAPSHOT_RE.search(page).group(1)
        self.assertNotIn("</script>", raw.lower())
        self.assertEqual(json.loads(raw)["round"]["task"],
                         '</script><img src=x onerror=alert(1)>')

    def test_html_parses_and_declares_the_page(self):
        class Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.ids: list[str] = []
            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if name == "id" and value:
                        self.ids.append(value)
        parser = Parser()
        parser.feed(render_page(self.snapshot))
        parser.close()
        for needed in ("snapshot", "app", "project", "stamp", "theme-toggle",
                       "watch-toggle"):
            self.assertIn(needed, parser.ids)

    def test_inlined_js_contains_no_script_closer(self):
        # Находка agy: литерал закрывающего тега script в КОММЕНТАРИИ ассета.
        # Ассет инлайнится внутрь <script>, и HTML5 закрывает элемент на такой
        # строке независимо от того, комментарий это или код: всё остальное уезжает
        # в страницу сырым текстом, DOM не строится, человек видит исходник.
        # Проверка идёт по собранной странице, а не по исходнику, потому что
        # именно собранная страница отдаётся браузеру.
        page = render_page(self.snapshot)
        head, _, rest = page.partition("<script>\n")
        self.assertTrue(rest, "в странице нет блока скриптов")
        body, _, _ = rest.partition("\n</script>")
        self.assertIsNone(breaks_out_of_script(body),
                          "в инлайненном скрипте есть закрывающий тег script")
        # Ровно три закрывающих тега на страницу: снимок, скрипты, и ничего лишнего.
        self.assertEqual(page.lower().count("</script"), 2)

    def test_script_closer_in_a_comment_is_refused_by_the_renderer(self):
        hostile = self.root / "hostile.js"
        hostile.write_text(
            "/* упоминание закрывающего тега: </script> идёт в комментарии */\n"
            "console.log(1);\n", encoding="utf-8")
        with self.assertRaises(AssetError) as caught:
            load_asset(hostile, in_script=True)
        message = str(caught.exception)
        self.assertIn("hostile.js", message)
        self.assertIn("строка 1", message)

    def test_a_working_slash_sequence_is_not_a_breaker(self):
        # Регулярка `/<\\/script/` содержит ту же букву, но страницу не ломает.
        # Проверка по подстроке запрещала бы писать в ассете рабочий код, поэтому
        # отличать надо по следующему символу, а не по наличию букв.
        safe = self.root / "safe.js"
        safe.write_text("var re = /<\\/script/;\nconsole.log(re);\n", encoding="utf-8")
        self.assertIsNone(breaks_out_of_script(safe.read_text(encoding="utf-8")))
        self.assertTrue(load_asset(safe, in_script=True))

    def test_every_breaker_shape_is_detected(self):
        # Правило из спецификации: за "</script" закрывает tab, LF, FF, пробел,
        # "/" или ">". Каждая форма проверена, иначе правило проверяется на одном
        # примере и не значит ничего.
        for terminator in SCRIPT_END_TERMINATORS:
            self.assertIsNotNone(breaks_out_of_script("x</script" + terminator + "y"),
                                 f"не замечен разделитель {terminator!r}")
        # Регистр не важен, и не-разделитель после букв не закрывает.
        self.assertIsNotNone(breaks_out_of_script("</SCRIPT>"))
        self.assertIsNone(breaks_out_of_script("</scripta>"))

    def test_real_assets_are_clean(self):
        for path in JS_ORDER:
            self.assertIsNone(breaks_out_of_script(path.read_text(encoding="utf-8")),
                              f"{path.name} содержит закрывающий тег script")

    def test_client_code_survives_as_one_whole_script_block(self):
        """Сильная форма той же проверки: блоки скриптов вырезаются по правилу HTML5.

        `HTMLParser` из stdlib разрывает `<script>` на литерале `</script>` так же,
        как браузер, — но молча: тест, проверяющий только id элементов, такой разрыв
        не замечает, потому что атрибуты разметки никуда не делись, и страница проходит
        проверку с кашей из исходника вместо отрисовки. Именно это и произошло с
        нашим набором до правки: 41 тест были зелёными на сломанной странице.

        Здесь проверяется не «есть ли в файле подстрока», а то, что браузер видит:
        блок скриптов ровно один, и клиентский код в нём целый — от `function` до
        финального вызова `render`.
        """
        page = render_page(self.snapshot)
        blocks = _script_blocks_per_html5(page)
        # Блок снимка открывается тегом с атрибутами, а не голым <script>, поэтому
        # регулярка находит ровно один блок — клиентский. Снимок проверяется
        # отдельно, через SNAPSHOT_RE и json.loads.
        self.assertEqual(len(blocks), 1, "клиентский скрипт разорван на части")
        client = blocks[0]
        for fragment in ("function readSnapshot", "function renderRound",
                         "function renderEvents", "render(readSnapshot());"):
            self.assertIn(fragment, client,
                          f"клиент потерял {fragment!r}: блок разорван")

    def test_toggles_are_real_buttons_with_types(self):
        page = render_page(self.snapshot)
        # Кликабельный div — прямо запрещённый §4 docs/theme-tokens.md.
        self.assertEqual(len(re.findall(r'<button type="button"', page)), 2)

    def test_refresh_meta_appears_only_in_watch_mode(self):
        # Ищется сам тег, а не подстрока: клиент содержит селектор
        # `meta[http-equiv="refresh"]`, и проверка по строке искала бы его.
        tag = re.compile(r'<meta\s+http-equiv="refresh"\s+content="(\d+)"\s*>')
        self.assertEqual(tag.findall(render_page(self.snapshot)), [])
        self.assertEqual(tag.findall(render_page(self.snapshot, refresh_seconds=5)), ["5"])

    def test_watch_seconds_reach_the_body_attribute(self):
        # Клиент пересоздаёт тег refresh при снятии паузы и не должен при этом
        # выдумывать период: он берётся отсюда.
        self.assertIn('<body data-watch-seconds="5">',
                      render_page(self.snapshot, refresh_seconds=5))
        self.assertIn('<body data-watch-seconds="0">', render_page(self.snapshot))

    def test_scripts_are_inlined_in_load_order(self):
        page = render_page(self.snapshot)
        first = page.index("PAIRBOARD_LABELS")
        second = page.index("restoreTheme()")
        self.assertLess(first, second, "словарь обязан грузиться раньше клиента")

    def test_assets_can_be_replaced_for_assembly_checks(self):
        page = render_page(_stub_snapshot(), css="/*c*/", js="/*j*/")
        self.assertIn("/*c*/", page)
        self.assertIn("/*j*/", page)
        self.assertNotIn("--background", page)

    def test_title_is_html_escaped(self):
        page = render_page(_stub_snapshot(project="<b>проект</b>"))
        self.assertIn("&lt;b&gt;проект&lt;/b&gt;", page.split("</title>")[0])


class AssetTest(PairTestCase):
    def test_missing_asset_is_an_error_not_an_empty_page(self):
        with self.assertRaises(AssetError):
            load_asset(CSS_FILE.parent / "нет-такого.css")

    def test_empty_asset_is_an_error(self):
        empty = self.root / "empty.css"
        empty.write_text("   \n", encoding="utf-8")
        with self.assertRaises(AssetError):
            load_asset(empty)

    def test_missing_theme_token_is_named_in_the_error(self):
        partial = self.root / "partial.css"
        partial.write_text(":root { --background: 0 0% 100%; }", encoding="utf-8")
        with self.assertRaises(AssetError) as caught:
            load_asset(partial, REQUIRED_TOKENS)
        self.assertIn("--border", str(caught.exception))

    def test_real_assets_load(self):
        self.assertTrue(load_asset(CSS_FILE, REQUIRED_TOKENS).strip())
        for path in JS_ORDER:
            self.assertTrue(load_asset(path).strip())


class ThemeContractTest(unittest.TestCase):
    """Инвариант `docs/theme-tokens.md`: ноль жёстких цветов вне токенов.

    Проверяются ОБЪЯВЛЕНИЯ, а не весь файл: комментарий, который объясняет запрет,
    разумеется, пишется словами «#hex» и «rgb()», и проверка по всему тексту
    запрещала бы писать объяснение инварианта. Комментарии вырезаются, и всё
    остальное — уже утверждения, где голый цвет означает именно поломку.
    """

    def setUp(self) -> None:
        raw = CSS_FILE.read_text(encoding="utf-8")
        self.css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        # Удаление комментариев не должно оставлять файл пустым: иначе все
        # проверки ниже проходили бы на пустоте.
        self.assertIn("--background", self.css)

    def test_no_hex_colors(self):
        self.assertNotRegex(self.css, r"#[0-9a-fA-F]{3,8}\b")

    def test_no_rgb_colors(self):
        self.assertNotRegex(self.css, r"\brgba?\s*\(")

    def test_every_hsl_reads_a_token(self):
        for use in re.findall(r"hsl\(([^)]*)\)", self.css):
            self.assertTrue(use.lstrip().startswith("var("),
                            f"hsl() без токена: hsl({use})")

    def test_every_referenced_token_is_declared(self):
        declared = set(re.findall(r"(--[\w-]+)\s*:", self.css))
        used = set(re.findall(r"var\((--[\w-]+)", self.css))
        missing = sorted(used - declared)
        self.assertEqual(missing, [], f"используются необъявленные токены: {missing}")

    def test_both_themes_declare_the_contract_tokens(self):
        # Тёмная тема обязана быть полной: `docs/theme-tokens.md` §1 требует полного
        # набора от каждой темы, а не «остальное наследуется от светлой».
        dark = self.css.split('[data-theme="dark"]')[1]
        for token in REQUIRED_TOKENS:
            self.assertIn(f"{token}:", dark, f"в тёмной теме нет {token}")

    def test_both_themes_exist(self):
        self.assertIn(":root", self.css)
        self.assertIn('[data-theme="dark"]', self.css)

    def test_status_colours_are_tokens_too(self):
        # Статусные цвета не входят в контракт, но объявляются токенами, иначе
        # индикаторы пришлось бы красить голыми цветами и сломать инвариант выше.
        self.assertRegex(self.css, r"--ok\s*:")
        self.assertRegex(self.css, r"--bad\s*:")
        self.assertIn("hsl(var(--ok))", self.css)


class ClientBehaviourTest(unittest.TestCase):
    """Клиент запускается настоящий, а не «прочитан глазами».

    Статические проверки инвариантов ловят опечатки в именах, но не ловят
    `TypeError` на третьей строке отрисовки: до человека такой баг доходит
    страницей с пустым `#app` и без единого слова об ошибке. Поэтому клиент
    исполняется на минимальном DOM-заглушке — `document`, `localStorage`,
    `createElement` — и дерево панели проверяется по факту.

    Заглушка живёт в `scripts/`, а не в `assets/`: это приспособление для
    проверки, у него нет соответствия исходнику, и в `assets/` оно попало бы в
    сборку страницы, то есть в код, который увидят живые люди.

    Набор не поднимает `Store`: заглушке нужны только файлы ассетов и готовый
    снимок, а проверка чтения состояния — работа `test_board_snapshot.py`.
    """

    def setUp(self) -> None:
        self.snapshot = _stub_snapshot()
        self.snapshot["events"] = [{"ts": "2026-01-01T00:00:00+00:00", "kind": "pilot_pane_dead",
                                    "role": "pairpilot", "to": "", "summary": "панель мертва"}]
        self.snapshot["tasks"] = [{"id": 1, "text": "задача", "status": "open", "round": 16,
                                   "added_by": "agy", "closed_by": "", "reason": "важно"}]
        self.tree, self.state = self._run_client(self.snapshot)

    def _run_client(self, snapshot: dict, seconds: str = "5"):
        runner = REPO_ROOT / "scripts" / "board-dom-stub.js"
        node = shutil.which("node")
        if node is None:
            # Без node клиент нечем запустить, и проверка обязана честно сказать
            # «пропущено», а не тихо стать зелёной, ни разу не выполнив код.
            self.skipTest("node недоступен: клиент проверен только статически")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False),
                                     encoding="utf-8")
            proc = subprocess.run([node, str(runner), str(LABELS_FILE), str(JS_FILE),
                                   str(snapshot_path), seconds],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, f"клиент упал: {proc.stderr[:400]}")
        lines = proc.stdout.splitlines()
        tree = "\n".join(line for line in lines if line.startswith((" ", "<")))
        meta = [line for line in lines if line.startswith("---")]
        return tree, meta

    def test_client_renders_without_throwing(self):
        self.assertIn("раунд 3", self.tree)
        for heading in ("агенты", "сверхвизор", "задачи", "события"):
            self.assertIn(f"<h2 \"{heading}", self.tree)

    def test_client_shows_roles_tasks_and_events(self):
        self.assertIn("панель %0", self.tree)
        self.assertIn("#1 задача", self.tree)
        self.assertIn("причина: важно", self.tree)
        self.assertIn("панель мертва", self.tree)

    def test_theme_defaults_to_dark_and_is_stored(self):
        self.assertIn("--- theme: dark", self.state)
        self.assertIn("pairboard.theme", " ".join(self.state))

    def test_watch_period_comes_from_the_body_attribute(self):
        # Клиент не должен выдумывать период: он берёт его из data-watch-seconds.
        self.assertIn('--- refresh meta: ["7"]',
                      " ".join(self._run_client(self.snapshot, "7")[1]))

    def test_static_mode_still_gets_the_tag_at_the_servers_period(self):
        # В статическом режиме генератор ставит период 0, и клиент не должен
        # «помогать»: он создаёт тег refresh только по данным разметки и молчал
        # бы на нулевом периоде, то есть страница получила бы content="0" —
        # немедленную перезагрузку по кругу. Поэтому ноль обязан означать «тега
        # нет вовсе», и это проверяется фактом, а не договорённостью.
        _, meta = self._run_client(self.snapshot, "0")
        self.assertIn('--- refresh meta: []', " ".join(meta))

    def test_role_tags_are_mutually_exclusive(self):
        """Писатель получает тег writer, остальные — critic, но не оба сразу.

        Регрессия, найденная на живом экране и приехавшая патчем agy: строка роли
        печатала `writer` и `critic` рядом, то есть утверждала, что один агент
        одновременно пишет и критикует. Формально это ложь, которую читает человек,
        а не дефект отрисовки: слово «critic» на строке писателя отвечает на
        вопрос «кто сейчас проверяет», и два ответа на один вопрос — это уже не
        ответ.
        """
        snapshot = _stub_snapshot()
        snapshot["roles"] = [
            {"role": "writer-agent", "is_writer": True, "pane": "%0", "pane_alive": True,
             "pane_note": "", "pid": 1, "pid_alive": True, "silence": "1 с",
             "silence_seconds": 1, "restored_by": ""},
            {"role": "peer-agent", "is_writer": False, "pane": "%1", "pane_alive": True,
             "pane_note": "", "pid": 2, "pid_alive": True, "silence": "1 с",
             "silence_seconds": 1, "restored_by": ""},
        ]
        tree, _ = self._run_client(snapshot)
        writer_line = _role_line(tree, "writer-agent")
        peer_line = _role_line(tree, "peer-agent")
        self.assertIn('class=tag writer "writer"', writer_line)
        self.assertNotIn("critic", writer_line)
        self.assertIn('class=tag critic "critic"', peer_line)
        self.assertNotIn('"writer"', peer_line)

    def test_writer_role_is_labelled_even_without_other_roles(self):
        # Роль одна, критик ещё не зарегистрировался: тег critic на строке
        # писателя всё равно не должен появляться — это всё тот же вопрос.
        tree, _ = self._run_client(_stub_snapshot())
        role_line = _role_line(tree, "a")
        self.assertIn("writer", role_line)
        self.assertNotIn("critic", role_line)

    def test_unreadable_snapshot_shows_a_banner_instead_of_a_blank_page(self):
        broken = _stub_snapshot()
        broken["read_error"] = "state.json не читается: сломан"
        tree, _ = self._run_client(broken)
        self.assertIn("state.json не прочитан", tree)


class ClientContractTest(unittest.TestCase):
    """Инварианты `assets/board.js` — то, что страница делает с данными."""

    def setUp(self) -> None:
        self.js = JS_FILE.read_text(encoding="utf-8")

    def test_no_html_injection_api(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                       "eval("):
            # Имена запрещённых API допускаются только в комментариях, где
            # объясняется, почему их тут нет.
            for line in self.js.splitlines():
                if banned in line:
                    self.assertTrue(line.strip().startswith(("*", "/*", "//")),
                                    f"{banned} в коде: {line.strip()}")

    def test_all_text_goes_through_text_content(self):
        self.assertIn("node.textContent = String(text)", self.js)
        # Создание элементов идёт одним помощником, а не россыпью innerHTML.
        self.assertIn("document.createElement(tag)", self.js)

    def test_calls_only_defined_functions(self):
        # Опечатка в имени функции проходит синтаксическую проверку и падает в
        # браузере как ReferenceError, то есть у человека, а не в гейте.
        source = self.js
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", source))
        defined |= set(re.findall(r"var\s+([A-Za-z_$][\w$]*)", source))
        # Параметры анонимных функций тоже не объявлены в файле: `draw` ниже
        # объявлен ровно в момент вызова. Без них проверка ругалась бы на код,
        # который совершенно корректен.
        defined |= set(re.findall(r"function\s*\(\s*([A-Za-z_$][\w$]*)", source))
        defined |= BUILTINS | KEYWORDS
        called = re.findall(r"(?<![\.\w$])([a-z][\w$]*)\s*\(", source)
        missing = sorted({name for name in called if name not in defined})
        self.assertEqual(missing, [], f"вызывается, но не объявлено: {missing}")

    def test_theme_choice_survives_a_reload(self):
        # Страница перезагружается целиком, поэтому выбор темы обязан лежать в
        # localStorage, а не в переменной: переменная пережила бы одну загрузку.
        self.assertIn("localStorage", self.js)
        self.assertRegex(self.js, r'var\s+THEME_KEY\s*=\s*"')
        self.assertRegex(self.js, r"remember\(THEME_KEY, theme\)")

    def test_watch_pause_is_separate_from_theme(self):
        # Две кнопки, две настройки: иначе нажатие «светлая» тихо останавливает
        # автообновление, и человек удивляется замершей ленте.
        self.assertIn('getElementById("theme-toggle")', self.js)
        self.assertIn('getElementById("watch-toggle")', self.js)
        theme_handler = self.js.split('getElementById("theme-toggle")')[1][:400]
        self.assertNotIn("setWatch", theme_handler)

    def test_status_has_three_states_not_two(self):
        # «Не знаю» обязан быть отдельным состоянием: у агента без панели нет
        # данных, и это не то же самое, что «панель мертва».
        self.assertIn('return "unknown"', self.js)
        self.assertIn('return "bad"', self.js)
        self.assertIn('return "ok"', self.js)

    def test_labels_asset_is_present_and_used(self):
        labels = (CSS_FILE.parent / "labels.js").read_text(encoding="utf-8")
        self.assertIn("window.PAIRBOARD_LABELS", labels)
        self.assertIn("window.PAIRBOARD_LABELS || {}", self.js)


class WritePageTest(PairTestCase):
    def test_file_is_written_and_leaves_no_temporary_behind(self):
        target = self.root / "board.html"
        written = write_page(target, _stub_snapshot(), refresh_seconds=5)
        self.assertEqual(written, target)
        self.assertTrue(target.exists())
        self.assertFalse(target.with_name("board.html.tmp").exists())

    def test_written_file_is_the_same_page(self):
        target = self.root / "board.html"
        write_page(target, _stub_snapshot())
        self.assertEqual(target.read_text(encoding="utf-8"),
                         render_page(_stub_snapshot()))

    def test_missing_parent_directory_is_created(self):
        target = self.root / "глубоко" / "внутри" / "board.html"
        write_page(target, _stub_snapshot())
        self.assertTrue(target.exists())

    def test_rewriting_replaces_the_previous_snapshot(self):
        target = self.root / "board.html"
        write_page(target, _stub_snapshot())
        changed = _stub_snapshot()
        changed["round"] = dict(changed["round"], number=99)
        write_page(target, changed)
        raw = SNAPSHOT_RE.search(target.read_text(encoding="utf-8")).group(1)
        self.assertEqual(json.loads(raw)["round"]["number"], 99)


class EndToEndTest(PairTestCase):
    def test_real_state_produces_a_page_a_browser_can_open(self):
        self.store.mutate(lambda state: state.update(
            task="дашборд", phase="implement", writer="writer-agent", round=16,
            roles={"writer-agent": {"pid": 1, "pane": "", "last_seen": "2026-01-01T00:00:00+00:00"}}))
        self.store.append_transcript("send", "agy", to="opencode", subject="привет")
        page = render_page(build_snapshot(self.root, limit=5), refresh_seconds=5)
        raw = SNAPSHOT_RE.search(page).group(1)
        data = json.loads(raw)
        self.assertEqual(data["round"]["number"], 16)
        self.assertEqual(data["events"][0]["summary"], "привет")
        self.assertIn("PAIRBOARD_LABELS", page)
        self.assertIn("http-equiv=\"refresh\"", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)