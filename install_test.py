#!/usr/bin/env python3
"""Тесты установщика.

Проверяют не «файл существует», а что установка НЕ ЛОМАЕТ чужие файлы: это
единственное, где ошибка стоит пользователю данных. Каждый тест гоняет
настоящий install.sh в отдельном HOME и смотрит на результат.

Запуск:  python3 install_test.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INSTALL = ROOT / "install.sh"

sys.path.insert(0, str(ROOT))
import importlib.util as _ilu


def _install_mod():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("install_sh", str(INSTALL))
    spec = _ilu.spec_from_loader("install_sh", loader)
    mod = _ilu.module_from_spec(spec)
    mod.__dict__["__file__"] = str(INSTALL)
    loader.exec_module(mod)
    return mod


_INSTALL = _install_mod()

def run_install(home: Path, *args: str) -> subprocess.CompletedProcess:
    # Вложенный прогон test_protocol.py (~3 мин) здесь не нужен: сюиту гоняет
    # verify-слой отдельно, а каждый install нёс бы её заново (698с на файл).
    # Один тест ниже проверяет, что пропуск работает, и один — что сюита
    # реально запускается без пропуска.
    env = dict(os.environ, HOME=str(home), PAIR_INSTALL_SKIP_SUITE="1")
    return subprocess.run([sys.executable, str(INSTALL), *args], capture_output=True,
                          text=True, env=env, cwd=str(ROOT), timeout=300)


def _roles_claiming(raw: str | None, pane: str) -> set[str]:
    """Роли, у которых записана эта панель.

    Нужен именно владелец панели, а не сам факт её присутствия: панель запускающего
    набора процесса законно стоит в ростере, когда набор запущен из панели самой
    пары, и «есть в ростере» против «принадлежит этой роли» — разные утверждения.
    Битый json даёт пустое множество: проверка не должна падать из-за того, что
    читает не то, а не из-за того, что установщик что-то сделал.
    """
    if not raw:
        return set()
    try:
        roles = json.loads(raw).get("roles") or {}
    except (ValueError, AttributeError):
        return set()
    return {name for name, info in roles.items()
            if isinstance(info, dict) and info.get("pane") == pane}


def _dead_roles(raw: str | None) -> set[str]:
    """Роли без живого процесса: такие не появляются и не исчезают сами.

    Пара пишет в живое состояние прямо во время установки — свои last_seen,
    свои регистрации, — поэтому «роль появилась» само по себе ничего не
    доказывает: это мог быть собственный агент набора. Доказывает обратное:
    роль, чей pid мёртв, не могла появиться или исчезнуть ничьей рукой, кроме
    установщика. Именно эту регрессию (verify писал в настоящий state.json,
    найдено вживую в round 31) сравнение по мёртвым ролям и ловит, а живая
    саморегистрация её не роняет.
    """
    if not raw:
        return set()
    try:
        roles = json.loads(raw).get("roles") or {}
    except (ValueError, AttributeError):
        return set()
    out = set()
    for name, info in roles.items():
        pid = (info or {}).get("pid") if isinstance(info, dict) else None
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True  # чужой процесс: проверить нечем, считается живым
            except OSError:
                alive = False
        if not alive:
            out.add(name)
    return out


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="pair-install-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        cfg = self.home / ".config" / "opencode"
        cfg.mkdir(parents=True)
        self.jsonc = cfg / "opencode.jsonc"

    def write_jsonc(self, text: str) -> None:
        self.jsonc.write_text(text, encoding="utf-8")

    def read_jsonc(self) -> dict:
        return json.loads(self.jsonc.read_text(encoding="utf-8"))

    def agents(self) -> Path:
        return self.home / ".config" / "opencode" / "AGENTS.md"


class TestForeignFilesSurvive(InstallCase):
    """Установщик обещает в шапке: «чужие ключи конфигов не трогает».

    Обещание проверяется здесь, а не читанием кода."""

    def test_unrelated_keys_in_jsonc_survive(self) -> None:
        self.write_jsonc(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "my_plugin": {"setting": "value"},
            "permission": {"bash": {"sudo *": "deny"}},
        }, indent=2))
        run_install(self.home, "opencode")
        cfg = self.read_jsonc()
        self.assertEqual(cfg.get("my_plugin"), {"setting": "value"})
        self.assertEqual(cfg["permission"]["bash"]["sudo *"], "deny",
                         "deny-правила человека должны пережить установку")

    def test_jsonc_comments_do_not_break_the_install(self) -> None:
        """jsonc — это JSON с комментариями. `json.loads` на таком файле падал,
        и установка обрывалась, оставив половину работы."""
        self.write_jsonc(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            '  // мой комментарий\n  "model": "test-model",\n}\n')
        result = run_install(self.home, "opencode")
        self.assertNotIn("не читается", result.stdout,
                         "комментарий в jsonc не должен ронять установку")
        cfg = _INSTALL.parse_jsonc(self.jsonc.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("model"), "test-model")

    def test_urls_inside_strings_are_not_mangled(self) -> None:
        """Комментарии снимаются конечным автоматом, а не регуляркой: иначе
        "https://..." внутри строки разрезается пополам."""
        self.write_jsonc(json.dumps({"server": "https://example.com/a//b"}, indent=2))
        run_install(self.home, "opencode")
        self.assertEqual(self.read_jsonc()["server"], "https://example.com/a//b")

    def test_jsonc_comments_survive_the_install(self) -> None:
        """json.dumps переписывал файл целиком и стирал комментарии человека —
        вопреки обещанию «чужие ключи не трогает». Правится только блок pair."""
        self.write_jsonc(
            '{\n  // мой комментарий\n  "model": "test-model",\n'
            '  /* блочный */\n  "mcp": {\n    // внутри mcp\n    "context7": {"url": "https://x/a//b"},\n  },\n}\n')
        run_install(self.home, "opencode")
        raw = self.jsonc.read_text(encoding="utf-8")
        self.assertIn("// мой комментарий", raw)
        self.assertIn("/* блочный */", raw)
        self.assertIn("// внутри mcp", raw)
        self.assertIn("https://x/a//b", raw)
        cfg = _INSTALL.parse_jsonc(self.jsonc.read_text(encoding="utf-8"))
        self.assertEqual(cfg["model"], "test-model")
        self.assertIn("pair", cfg["mcp"])
        self.assertIn("context7", cfg["mcp"])

    def test_second_install_keeps_comments_and_replaces_pair(self) -> None:
        """Повтор не дублирует блок и не ест комментарии: замена, не вставка."""
        self.write_jsonc('{\n  // держи меня\n  "model": "m",\n}\n')
        run_install(self.home, "opencode")
        first = self.jsonc.read_text(encoding="utf-8")
        run_install(self.home, "opencode")
        second = self.jsonc.read_text(encoding="utf-8")
        self.assertIn("// держи меня", second)
        self.assertEqual(second.count('"pair"'), 1)
        self.assertEqual(first, second)


    def test_instructions_tail_after_the_marker_is_kept(self) -> None:
        """Прежний код делал split(MARK)[0] и удалял всё после маркера — тихо и
        на каждом запуске. Удаление содержимого чужого файла §12 требует спросить."""
        self.agents().write_text(
            "# Мои правила\n\nне трогать.\n\n<!-- pair-mailbox -->\n"
            "## Парная работа\n\nстарый блок\n\n"
            "## Моя секция после маркера\n\nэто важно и не должно пропасть.\n",
            encoding="utf-8")
        run_install(self.home, "opencode")
        text = self.agents().read_text(encoding="utf-8")
        self.assertIn("Моя секция после маркера", text,
                      "хвост после нашего блока принадлежит человеку")
        self.assertIn("не должно пропасть", text)

    def test_a_backup_is_written_before_overwriting(self) -> None:
        self.agents().write_text("# было\n\n<!-- pair-mailbox -->\nстарый\n", encoding="utf-8")
        run_install(self.home, "opencode")
        backup = self.agents().with_suffix(".md.bak")
        self.assertTrue(backup.exists(), "файл человека перезаписывается — нужна копия")
        self.assertIn("было", backup.read_text(encoding="utf-8"))

    def test_reinstall_is_idempotent(self) -> None:
        """Идемпотентность заявлена в шапке установщика."""
        self.agents().write_text("# мои правила\n", encoding="utf-8")
        run_install(self.home, "opencode")
        first = self.agents().read_text(encoding="utf-8")
        run_install(self.home, "opencode")
        self.assertEqual(first, self.agents().read_text(encoding="utf-8"),
                         "вторая установка не должна менять файл")

    def test_unrelated_mcp_servers_survive(self) -> None:
        self.write_jsonc(json.dumps({
            "mcp": {"context7": {"type": "http", "url": "https://example.com"}},
        }, indent=2))
        run_install(self.home, "opencode")
        self.assertIn("context7", self.read_jsonc()["mcp"],
                      "чужой MCP-сервер не должен исчезнуть")

    def test_broken_config_is_left_alone_and_says_so(self) -> None:
        """Нечитаемый конфиг должен остаться нетронутым и быть назван, а не
        молча перезаписанным половиной настройки."""
        original = '{ this is not json at all\n'
        self.write_jsonc(original)
        result = run_install(self.home, "opencode")
        self.assertIn("не читается", result.stdout)
        self.assertIn("не тронут", result.stdout)
        self.assertEqual(self.jsonc.read_text(encoding="utf-8"), original,
                         "нечитаемый конфиг не переписывается")


class TestNativePermissions(InstallCase):
    """Установщик пишет нативный permission-слой: allow-list вместо кликов.

    Боль раунда 54: 5 из 6 профилей поднимались с голым launch, deny писался
    только для opencode. Проверка — по файлам в изолированном HOME и по
    проектному .claude/settings.json: слой есть, deny содержит §12-категории.
    """

    def test_agy_settings_carry_deny(self) -> None:
        result = run_install(self.home, "agy")
        self.assertIn("права:", result.stdout)
        path = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertTrue(path.exists(), "настройки Antigravity не записаны")
        perms = json.loads(path.read_text(encoding="utf-8"))["permissions"]
        self.assertIn("command(sudo)", perms["deny"])
        self.assertIn("command(git status)", perms["allow"])

    def test_codex_is_left_to_the_pilot(self) -> None:
        """Нативного allow/deny у codex нет — установщик его не трогает.

        Профиль остаётся на пилоте (§13b): это решение раунда 54, а не
        недоделка, и молча писать approval-«never» в config.toml нельзя.
        """
        result = run_install(self.home, "codex")
        self.assertNotIn("права:", result.stdout)

    def test_claude_project_settings_keep_deny(self) -> None:
        run_install(self.home, "claude")
        path = ROOT / ".claude" / "settings.json"
        self.assertTrue(path.exists(), "проектный .claude/settings.json потерян")
        perms = json.loads(path.read_text(encoding="utf-8"))["permission"]
        self.assertIn("Bash(sudo:*)", perms["deny"])
        self.assertIn("Bash(git status:*)", perms["allow"])


class TestMcpRegistration(InstallCase):
    def test_pair_server_is_registered_and_verified(self) -> None:
        result = run_install(self.home, "opencode")
        self.assertIn("pair-mailbox", result.stdout)
        argv = self.read_jsonc()["mcp"]["pair"]["command"]
        self.assertEqual(argv[0], "sh")
        self.assertTrue(argv[1].endswith("serve.sh"))
        self.assertEqual(argv[2], "opencode", "роль зашивается в конфиг, а не в память процесса")

    def test_the_suite_runs_as_part_of_the_install(self) -> None:
        if os.environ.get("PAIR_INSTALL_FAST"):
            self.skipTest("быстрый режим: живую сюиту гоняет integration-слой")
        env = dict(os.environ, HOME=str(self.home))
        env.pop("PAIR_INSTALL_SKIP_SUITE", None)
        result = subprocess.run([sys.executable, str(INSTALL), "opencode"],
                                capture_output=True, text=True, env=env,
                                cwd=str(ROOT), timeout=600)
        self.assertNotIn("ТЕСТЫ НЕ ПРОШЛИ", result.stdout)

    def test_test_installs_skip_the_nested_suite(self) -> None:
        """run_install ставит PAIR_INSTALL_SKIP_SUITE: вложенные ~3 мин
        на каждый тестовый install не повторяются (было 698с на файл)."""
        result = run_install(self.home, "opencode")
        self.assertNotIn("тесты сервера", result.stdout)


class TestVerifyDoesNotTouchTheRoster(InstallCase):
    """Проверка транспорта не имеет права менять состояние живой пары.

    Найдено вживую, а не в чтении кода: `verify()` поднимал `serve.sh <role>` с
    унаследованным окружением и без явного состояния, то есть писал в НАСТОЯЩИЙ
    `.pair/state.json` того проекта, из которого запущен install.sh, и наследовал
    `TMUX_PANE`. Прогон шёл из окна agy, и панель `opencode` в ростере
    перезаписалась адресом панели `agy`.

    До перехода на адреса сессий это была порча одного запуска. После — порча
    прочная: роль получала ЧУЖУЮ панель каноническим адресом, то есть адрес,
    который не перенумеровывается, и следующий же `pair_deliver` печатал бы в
    чужой композер. Плюс в ростер попадал pid проверочного процесса, который
    сразу умирает, и роль выглядела «мёртвой при живом окне».

    Тест ставит те же условия: запускает install из под панели и смотрит, что
    настоящий `.pair/state.json` не тронут. Панель берётся НАСТОЯЩАЯ, а не
    выдуманная, по двум причинам. Баг проявился именно с живой панелью в
    окружении, и выдуманная панель воспроизвела бы не его. И, что важнее,
    установщик прогоняет ещё и набор тестов с тем же окружением, а часть тестов
    работает с живой панелью набора: подставь несуществующую — упадут они, и
    красный результат сказал бы про подмену, а не про баг.

    Мок здесь не годится: нужен настоящий запуск того же кода.
    """

    def live_pane(self) -> str:
        """Настоящая панель, из которой запущен набор, или пропуск."""
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            self.skipTest("набор запущен вне tmux")
        tmux = shutil.which("tmux")
        if not tmux:
            self.skipTest("tmux не установлен")
        probe = subprocess.run([tmux, "display-message", "-p", "-t", pane, "#{pane_id}"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or not probe.stdout.strip():
            self.skipTest(f"панель набора {pane} не отвечает")
        return pane

    def test_install_from_a_pane_does_not_touch_the_real_roster(self) -> None:
        pane = self.live_pane()
        state = ROOT / ".pair" / "state.json"
        before = state.read_text(encoding="utf-8") if state.exists() else None
        env = dict(os.environ, HOME=str(self.home), TMUX_PANE=pane)
        subprocess.run([sys.executable, str(INSTALL), "opencode"],
                       capture_output=True, text=True, env=env,
                       cwd=str(ROOT), timeout=300)
        after = state.read_text(encoding="utf-8") if state.exists() else None
        # Негерметичность, найденная критиком opencode после раунда 47:
        # сравнение ПОЛНОГО живого state.json до/после краснело при любой
        # живой паре — процессы пары пишут в файл во время установки
        # (сообщения, кольца пилота, evidence), то есть тест детерминированно
        # краснел от СВИДЕТЕЛЯ, а не от ошибки установщика. Проверяем то,
        # что установщик действительно может сломать: владение панелью
        # запуска и число ролей.
        if before is not None and after is not None:
            self.assertEqual(
                _roles_claiming(before, pane), _roles_claiming(after, pane),
                "панель запускающего процесса не должна перейти к другой "
                "роли: установщик добавил или отнял её в живом ростере")
            # Не «opencode» в белый список: сравнение идёт по роли, которую
            # никто живой не держит. Живая роль появляется сама (её агент
            # пишет в состояние во время установки — пара-набор жива), мёртвая
            # не появляется ничьей рукой, кроме установщика.
            self.assertEqual(
                _dead_roles(before), _dead_roles(after),
                "установщик не должен добавлять и убирать роли живого ростера: "
                "мёртвая роль появляется или исчезает только его рукой "
                "(verify, писавший в настоящий state.json, — регрессия 31-го)")
        return

    def test_a_pane_may_belong_to_a_role_without_being_rejected(self) -> None:
        """Панель набора законно может стоять в ростере.

        Набор запускают из панели самой пары, и тогда `%0` — это панель писателя.
        Старая проверка требовала, чтобы такой панели в ростере не было, то есть
        требовала от установщика снести чужую запись, и падала на правильном
        поведении. Этот тест падает, если кто-то вернёт ту формулировку в виде
        «панели набора не должно быть в ростере» — то есть сторожит сторожа.
        """
        pane = self.live_pane()
        state = ROOT / ".pair" / "state.json"
        if not state.exists():
            self.skipTest("нет состояния пары")
        owners = _roles_claiming(state.read_text(encoding="utf-8"), pane)
        if not owners:
            self.skipTest("панель набора в ростере не значится: нечего проверять")
        for role in owners:
            self.assertIn(role, json.loads(state.read_text(encoding="utf-8"))["roles"])

    def test_the_role_in_the_install_still_gets_registered(self) -> None:
        """Изоляция проверки не превратилась в «сервер не запускается».

        Проверка транспорта существует ради одного ответа: сервер отвечает как
        pair-mailbox. Если изоляция состояния её сломала, установщик тихо
        перестал бы что-либо проверять, и это выглядело бы как успех.
        """
        result = run_install(self.home, "opencode")
        self.assertIn("проверка: ОК, pair-mailbox", result.stdout)


class TestPaneRoleLookup(InstallCase):
    """`pane_role_pid` ищет сервер роли, а не процесс панели.

    Эта поломка прошла мой собственный пункт 2 незамеченной: тесты мокали
    `pane_pid` живым значением, то есть проверяли не то. На живых панелях
    `#{pane_pid}` отдаёт bash, а агент живёт его ребёнком, поэтому «в панели
    кто-то есть» всегда истинно — и падавшая роль объявлялась вечно живой, то
    есть перезапуск отменялся целиком.

    Здесь настоящий `/proc` и настоящие процессы этого набора, потому что подмена
    тут и была причиной, по которой баг не видели.
    """

    def test_a_role_server_is_found_among_the_descendants(self) -> None:
        # «Панель» — это процесс набора, а его потомки — то, что в ней живёт.
        # Проверка без decoy была бы пустой: при единственном потомке «первый
        # потомок» и «сервер роли» совпадают, и замена одного другим проходила
        # незаметно. Поэтому сначала запускается посторонний процесс, и ответ
        # обязан быть НЕ им.
        decoy = subprocess.Popen(["sleep", "30"])
        self.addCleanup(decoy.kill)
        # Второй посторонний процесс, и он содержит имя роли в своих аргументах —
        # так выглядит запуск агента. Искать просто подстроку нельзя: такой процесс
        # удовлетворяет подстрокке и не является сервером роли, а ошибка стоила бы
        # чужого pid в чужой записи.
        namesake = subprocess.Popen(["sleep", "30", "probe-role"])
        self.addCleanup(namesake.kill)
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp/pair_mailbox/server.py"),
             "--role", "probe-role", "--state", str(Path(tempfile.mkdtemp()) / ".pair")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        # Поиск получает фиксированный порядок потомков. Порядок обхода настоящего
        # `/proc` произволен, а на произвольном порядке эти проверки моргали: то
        # ловили замену «найди сервер роли» на «верни первого потомка», то нет.
        # Тест, результат которого зависит от порядка каталога, проверяет не код,
        # а удачу. Порядок здесь задан явно, поэтому и падение детерминировано.
        real_descendants = composer_module()._descendants
        composer_module()._descendants = lambda root: [decoy.pid, namesake.pid, child.pid]
        try:
            found = 0
            for _ in range(50):
                found = composer_module().find_role_pid(os.getpid(), "probe-role")
                if found:
                    break
                time.sleep(0.05)
        finally:
            composer_module()._descendants = real_descendants
        self.assertEqual(child.pid, found,
                         "найден должен быть сервер роли, а не первый попавшийся "
                         "процесс и не тот, у кого имя роли есть в аргументах")

    def test_a_process_with_the_role_name_in_its_args_is_not_the_role(self) -> None:
        """Сверка роли строгая: имя файла и соседние аргументы, не подстрока.

        Детерминированная проверка вместо «среди потомков нашёлся не тот»: порядок
        `/proc` произволен, и на нём замена точного совпадения на подстроку
        ловилась через раз. Здесь всё наоборот — один конкретный процесс, и
        вопрос к нему один.
        """
        namesake = subprocess.Popen(["sleep", "30", "probe-role"])
        self.addCleanup(namesake.kill)
        time.sleep(0.1)
        composer = composer_module()
        self.assertFalse(composer._is_role_server(namesake.pid, "probe-role"),
                         "процесс с именем роли в аргументах — не сервер роли")

    def test_a_foreign_role_is_not_claimed(self) -> None:
        """Роль в панели одна, и чужая роль не должна находиться как своя.

        Ошибка в одну сторону — всегда. Найти чужой процесс и записать его в
        чужую же запись значит стереть различие между двумя ролями.
        """
        found = _find_in_tree(os.getpid(), "definitely-not-a-real-role")
        self.assertEqual(0, found, "несуществующая роль не должна находиться")

    def test_an_empty_role_never_matches(self) -> None:
        self.assertEqual(0, _find_in_tree(os.getpid(), ""),
                         "пустая роль не должна совпасть ни с чем")

    def test_the_lookup_needs_no_tmux_at_all(self) -> None:
        """Поиск идёт по `/proc`, а не по tmux.

        Замечание критика было про второй вызов к tmux ради pid. После этой
        правки его не осталось: сведения о панели берутся из `pane_identity`
        одним запросом, а сам поиск процесса идёт по `/proc`. Проверяется не
        числом вызовов, а тем, что подмена tmux не может помешать найти роль —
        иначе утверждение «запрос не нужен» осталось бы словами.
        """
        real_run = subprocess.run

        def refuse_tmux(argv, **kwargs):
            if isinstance(argv, (list, tuple)) and "tmux" in " ".join(map(str, argv)):
                raise AssertionError("поиск роли не должен ходить в tmux")
            return real_run(argv, **kwargs)

        subprocess.run = refuse_tmux
        try:
            self.assertEqual(0, _find_in_tree(os.getpid(), "definitely-not-a-real-role"))
        finally:
            subprocess.run = real_run


def composer_module():
    """Настоящий модуль composer, а не его копия в тесте.

    Раньше поиск был продублирован прямо здесь, и тест проверял копию: правка
    прода на «вернуть первого потомка» оставляла набор зелёным. Тест, который не
    может увидеть правку прода, ничего не проверяет.
    """
    sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
    import composer  # noqa: PLC0415
    return composer


def _find_in_tree(root: int, role: str) -> int:
    return composer_module().find_role_pid(root, role)


class TestPilotLaunchMap(InstallCase):
    """`launch` нужен перезапуску упавшего агента: без него сверхвизор знает, что
    роль мертва, но не знает, какой командой её поднять."""

    def test_every_profile_carries_a_launch_command(self) -> None:
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            with self.subTest(profile=name):
                self.assertTrue(profile.get("launch"),
                                f"у профиля {name} нет команды перезапуска")

    def test_opencode_launch_is_standalone(self) -> None:
        """opencode по умолчанию делит фоновый демон между окнами, и два
        экземпляра получили бы один конфиг — то есть одну роль на двоих."""
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("--standalone", profiles["opencode"]["launch"])

    def test_opencode_launch_paths_agree_on_auto(self) -> None:
        """Оба пути запуска несут --auto: иначе они расходятся и один из них
        встаёт на промпт при каждом рутинном прогоне (раунд 33, P0).

        cli-profiles.json — рестарт пилота, bin/pair-duo — живой запуск.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("--auto", profiles["opencode"]["launch"])
        duo = (ROOT / "bin" / "pair-duo").read_text(encoding="utf-8")
        self.assertIn("opencode --standalone --auto", duo)


    def test_launch_is_json_parseable(self) -> None:
        """Файл читается в том числе с --check, где любая ошибка JSON ломает
        установку целиком."""
        data = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("_readme", data)

    def test_profiles_sharing_one_file_use_different_server_keys(self) -> None:
        """Два CLI на одном mcp.json обязаны писать в разные ключи (#omp).

        Измерено 2026-10-06: `install.sh omp` перезаписал ключ `pair` в
        `~/.pi/agent/mcp.json` ролью `omp` и молча сломал MCP у `pi`.
        Общий файл без развода ключей — это одна роль на двоих.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        by_path: dict[str, dict[str, str]] = {}
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            spec = (profile or {}).get("mcp") or {}
            by_path.setdefault(str(spec.get("path")), {})[name] = str(
                spec.get("server_key", "pair"))
        for path, keys in by_path.items():
            with self.subTest(path=path):
                self.assertEqual(len(keys), len(set(keys.values())),
                                 f"один файл {path} — разные ключи: {keys}")

    def test_no_two_profiles_share_one_config_home(self) -> None:
        """Два РАЗНЫХ движка на одном доме конфигов — это общая роль (ROLE-BUG 2026-10-07).

        Измерено 2026-10-07: бинарь `omp` — симлинк на pi-coding-agent, но его дом —
        `~/.omp/agent/`, а профиль `omp` писал в `~/.pi/agent/mcp.json`. Хуже того:
        `omp`/`pi` грузят чужие MCP через discovery (OpenCode/Claude/Codex/Cursor/
        Gemini/VSCode/Windsurf), и ключ `pair` коллизирует между источниками —
        процесс `omp` поднял `server.py --role opencode` из проектного
        `.opencode/opencode.jsonc` и потерял write authority. Профиль обязан
        указывать на собственный дом движка, а не на чужой.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        home_of = {"pi": ".pi", "omp": ".omp", "opencode": ".config/opencode",
                   "agy": ".gemini", "codex": ".codex", "claude": ".claude"}
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            spec = (profile or {}).get("mcp") or {}
            path = str(spec.get("path"))
            with self.subTest(profile=name):
                if name in home_of:
                    self.assertIn(home_of[name], path,
                                 f"профиль {name} пишет в чужой дом: {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)