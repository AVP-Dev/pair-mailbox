"""Одно правило — три грамматики. Тест на каждое представление и на границу deny.

Один тест-файл на один исходник: `autoperms.py` -> `test_autoperms.py`.
Живая проверка идёт через install_test.py: установщик обязан написать файлы,
которые парсятся.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import autoperms


class TestRenderers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.home = Path(self.tmp.name) / "home"
        self.root.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_profile_refuses(self) -> None:
        with self.assertRaises(ValueError):
            autoperms.render("codex", self.root, self.home)

    def test_codex_and_pi_stay_on_pilot(self) -> None:
        """У codex и pi нет нативных allow/deny — это решение, а не дыра."""
        self.assertIn("codex", autoperms.PROFILES_ON_PILOT_ONLY)
        self.assertIn("pi", autoperms.PROFILES_ON_PILOT_ONLY)
        for profile in autoperms.PROFILES_ON_PILOT_ONLY:
            self.assertNotIn(profile, autoperms.RENDERERS)

    def test_every_renderer_outputs_valid_json(self) -> None:
        for profile in autoperms.RENDERERS:
            with self.subTest(profile=profile):
                payload = autoperms.render(profile, self.root, self.home)
                round_trip = json.loads(json.dumps(payload))
                self.assertIsInstance(round_trip, dict)

    def test_opencode_shape(self) -> None:
        payload = autoperms.render("opencode", self.root, self.home)
        bash = payload["permission"]["bash"]
        self.assertEqual("allow", bash["*"])
        self.assertIn("edit", payload["permission"])
        self.assertIn("webfetch", payload["permission"])
        deny_keys = [k for k, v in bash.items() if v == "deny"]
        joined = " ".join(deny_keys)
        for must in ("sudo", "curl", ".env", "--force"):
            self.assertIn(must, joined, f"§12-категория {must!r} обязана быть в deny")

    def test_agy_shape(self) -> None:
        payload = autoperms.render("agy", self.root, self.home)
        perms = payload["permissions"]
        self.assertIn("command(git status)", perms["allow"])
        self.assertIn("command(sudo)", perms["deny"])
        self.assertIn("write_file(.git/)", perms["deny"])
        self.assertIn("command(regex:curl .*)", perms["deny"])

    def test_claude_shape(self) -> None:
        payload = autoperms.render("claude", self.root, self.home)
        perms = payload["permission"]
        self.assertIn("Bash(git status:*)", perms["allow"])
        # P0 (раунд 75): раннеры тестов — deny, а не allow.
        self.assertIn("Bash(bun test:*)", perms["deny"])
        self.assertIn("Bash(sudo:*)", perms["deny"])
        self.assertIn("Read(./.env)", perms["deny"])

    def test_allow_covers_routine(self) -> None:
        """Без этого разрешение бесполезно: рутина всё равно спрашивала бы.

        Канон хранит подкоманды и команды по отдельности (`status` + `git`),
        склеивает их рендерер — проверяем и канон, и склейку в каждом виде.
        P0 (раунд 75): раннеры тестов из allow убраны — только человек или
        изоляция; здесь проверяем, что они переехали в deny.
        """
        covered = autoperms._allow_items()
        for item in ("status", "commit", "ruff", "bash scripts/",
                     "bin/pairup", "gh pr"):
            self.assertIn(item, covered)
        denied = " ".join(autoperms._deny_items())
        for item in ("pytest", "bun test", "cargo test", "npm test"):
            self.assertIn(item, denied)
        agy = autoperms.render("agy", self.root, self.home)["permissions"]["allow"]
        claude = autoperms.render("claude", self.root, self.home)["permission"]["allow"]
        self.assertIn("command(git status)", agy)
        self.assertIn("Bash(git status:*)", claude)

    def test_deny_covers_twelve(self) -> None:
        denied = " ".join(autoperms._deny_items())
        for stop in ("sudo", "curl", "rm -rf /", ".ssh", "--force", "reset --hard"):
            self.assertIn(stop, denied)


class TestWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.home = Path(self.tmp.name) / "home"
        self.root.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_keeps_foreign_keys(self) -> None:
        """Настройки человека не затираются: дописывается только permissions."""
        root = self.home / ".config" / "opencode"
        root.mkdir(parents=True)
        target = root / "opencode.jsonc"
        target.write_text(json.dumps({
            "model": "keep-me",
            "permission": {"bash": {"x": "allow", "sudo *": "deny"}},
        }), encoding="utf-8")
        autoperms.write_native_perms("opencode", self.root, self.home)
        merged = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual("keep-me", merged["model"])
        bash = merged["permission"]["bash"]
        self.assertEqual("allow", bash["*"], "канон добавлен")
        self.assertEqual("deny", bash["sudo *"], "ручное правило человека выжило")
        self.assertEqual("allow", bash["x"], "не наше правило не тронуто")

    def test_merge_deny_wins_over_human_allow(self) -> None:
        """Конфликт решается в пользу deny: человеческий allow на опасное
        ключ перебивается каноном, иначе §12 зависел бы от порядка правок."""
        root = self.home / ".config" / "opencode"
        root.mkdir(parents=True)
        target = root / "opencode.jsonc"
        target.write_text(json.dumps({
            "permission": {"bash": {"x": "allow", "*sudo*": "allow"}},
        }), encoding="utf-8")
        autoperms.write_native_perms("opencode", self.root, self.home)
        bash = json.loads(target.read_text(encoding="utf-8"))["permission"]["bash"]
        self.assertEqual("deny", bash["*sudo*"])

    def test_broken_file_raises_not_silently_overwrites(self) -> None:
        target = self.root / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_text("{ not json at all", encoding="utf-8")
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            autoperms.write_native_perms("claude", self.root, self.home)

    def test_created_target_is_readable(self) -> None:
        path = autoperms.write_native_perms("claude", self.root, self.home)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("permission", payload)
        self.assertIn("Bash(git status:*)", payload["permission"]["allow"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
