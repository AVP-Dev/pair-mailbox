"""Тесты проверки свежести сервера: server_freshness.py.

Один тест-файл на один исходник: server_freshness.py -> test_server_freshness.py.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import server_freshness


class TestServerFreshness(unittest.TestCase):
    def test_process_start_time_is_positive(self) -> None:
        start_ts = server_freshness.get_process_start_time()
        self.assertGreater(start_ts, 0.0)

    def test_fresh_when_start_time_after_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "foo.py"
            f1.write_text("x = 1\n", encoding="utf-8")
            st = f1.stat().st_mtime
            
            # Старт процесса ПОСЛЕ создания файлов
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st + 10.0
            )
            self.assertTrue(verdict["fresh"])
            self.assertEqual("foo.py", verdict["newest_code_file"])
            self.assertIn("свеж", verdict["why"])

    def test_stale_when_files_newer_than_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "bar.py"
            f1.write_text("y = 2\n", encoding="utf-8")
            st = f1.stat().st_mtime

            # Старт процесса ЗАДОЛГО ДО изменения файлов (> 2s)
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st - 100.0
            )
            self.assertFalse(verdict["fresh"])
            self.assertEqual("bar.py", verdict["newest_code_file"])
            self.assertIn("Требуется перезапуск", verdict["why"])
            self.assertGreater(verdict["lag_seconds"], 90.0)

    def test_tolerance_window_considers_nearly_simultaneous_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "baz.py"
            f1.write_text("z = 3\n", encoding="utf-8")
            st = f1.stat().st_mtime

            # Разница 1 секунда попадает в допуск 2.0 секунды
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st - 1.0
            )
            self.assertTrue(verdict["fresh"])


if __name__ == "__main__":
    unittest.main()
