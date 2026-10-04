#!/usr/bin/env python3
"""Тесты установщика.

Проверяют не «файл существует», а что установка НЕ ЛОМАЕТ чужие файлы: это
единственное, где ошибка стоит пользователю данных. Каждый тест гоняет
настоящий install.sh в отдельном HOME и смотрит на результат.

Запуск:  python3 install_test.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INSTALL = ROOT / "install.sh"


def run_install(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(INSTALL), *args], capture_output=True,
                          text=True, env=env, cwd=str(ROOT), timeout=300)


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
        self.assertEqual(self.read_jsonc().get("model"), "test-model")

    def test_urls_inside_strings_are_not_mangled(self) -> None:
        """Комментарии снимаются конечным автоматом, а не регуляркой: иначе
        "https://..." внутри строки разрезается пополам."""
        self.write_jsonc(json.dumps({"server": "https://example.com/a//b"}, indent=2))
        run_install(self.home, "opencode")
        self.assertEqual(self.read_jsonc()["server"], "https://example.com/a//b")

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


class TestMcpRegistration(InstallCase):
    def test_pair_server_is_registered_and_verified(self) -> None:
        result = run_install(self.home, "opencode")
        self.assertIn("pair-mailbox", result.stdout)
        argv = self.read_jsonc()["mcp"]["pair"]["command"]
        self.assertEqual(argv[0], "sh")
        self.assertTrue(argv[1].endswith("serve.sh"))
        self.assertEqual(argv[2], "opencode", "роль зашивается в конфиг, а не в память процесса")

    def test_the_suite_runs_as_part_of_the_install(self) -> None:
        result = run_install(self.home, "opencode")
        self.assertNotIn("ТЕСТЫ НЕ ПРОШЛИ", result.stdout)


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

    def test_launch_is_json_parseable(self) -> None:
        """Файл читается в том числе с --check, где любая ошибка JSON ломает
        установку целиком."""
        data = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("_readme", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)