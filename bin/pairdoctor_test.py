"""Тесты диагностики (`bin/pairdoctor`).

Проверяется то, ради чего чинился парсинг: итог unittest лежит в stderr,
и чтение только stdout давало пустой tail навсегда.

Функция грузится через AST без исполнения файла: на верхнем уровне
pairdoctor сразу гоняет проверки (print + subprocess), и exec всего
файла виснет на прогоне test_protocol внутри импорта.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_tail_fn():
    tree = ast.parse((ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "unittest_tail":
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {}
            exec(compile(mod, "pairdoctor_fn", "exec"), ns)
            return ns["unittest_tail"]
    raise AssertionError("unittest_tail не найден в bin/pairdoctor")


unittest_tail = _load_tail_fn()


class TestUnittestTail(unittest.TestCase):
    def test_stderr_carries_the_count(self) -> None:
        """unittest пишет Ran/OK в stderr: tail находится в объединённом."""
        self.assertEqual(["Ran 3 tests in 0.011s", "OK"],
                         unittest_tail("", "Ran 3 tests in 0.011s\nOK\n"))

    def test_stdout_alone_is_not_enough(self) -> None:
        """Чтение только stdout — пустой tail: исходный дефект карточки."""
        self.assertEqual([], unittest_tail("", ""))

    def test_failure_tail_is_found(self) -> None:
        self.assertIn("FAILED (failures=1)",
                      unittest_tail("", "Ran 3 tests in 0.011s\nFAILED (failures=1)\n"))


class TestDeliverySection(unittest.TestCase):
    def test_section_present(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[5] delivery", text)
        self.assertIn("composer_line", text)
        self.assertIn("is_composer_empty", text)

    def test_sections_renumbered(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[7] git", text)


class TestRound86Fixes(unittest.TestCase):
    def test_server_key_from_profile(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn('spec.get("server_key", "pair")', text)

    def test_none_is_warn_not_bad(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("composer busy (not visible) — delivery deferred", text)
        # рядом с None не должно быть bad()
        idx = text.find("if _line is None:")
        block = text[idx:idx + 300]
        self.assertNotIn("bad(", block)

    def test_targets_deduped(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("if _proj not in targets:", text)

    def test_freshness_section_present(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[6] freshness", text)
        self.assertIn("list_stale_files", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
