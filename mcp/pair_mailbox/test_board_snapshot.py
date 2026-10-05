"""Тесты снимка состояния для дашборда (`board_snapshot.py`).

Что здесь проверяется и почему именно это. Снимок — единственное место, где
животные данные превращаются в то, что увидит человек, поэтому проверяется не
«структура словаря», а решения, которые человек принять не может:

- битое состояние даёт снимок с `read_error`, а не исключение (дашборд открывают
  именно тогда, когда что-то сломалось, и пустой экран без причины — худший ответ);
- «не знаю» не превращается в «нет» ни для панели, ни для pid;
- непризнанное событие сохраняет своё имя, а не выдуманный смысл;
- длинный текст обрезается, и страница остаётся читаемой.

Про `/proc` и tmux. Здесь нет настоящей панели и сверхвизора, поэтому проверки
живости подменяют их и убеждаются, что подмена вообще возможна: тест, который
прошёл бы и с настоящей панелью, ничего не проверял бы.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from pair_test_base import PairTestCase

import board_snapshot
from board_snapshot import (DEFAULT_STALE_AFTER, _age, _clip, build_snapshot,
                            snapshot_json, summarise_event)


def _iso(delta_seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat()


class SnapshotFactsTest(PairTestCase):
    def test_round_facts_come_from_state(self):
        self.store.mutate(lambda state: state.update(
            task="дашборд", phase="implement", mode="audit", autonomy="semi",
            writer="writer-agent", writer_reason="назначено человеком", round=16))
        snap = build_snapshot(self.root, limit=5)
        self.assertEqual(snap["round"]["number"], 16)
        self.assertEqual(snap["round"]["phase"], "implement")
        self.assertEqual(snap["round"]["phase_index"], 3)
        self.assertEqual(snap["round"]["mode"], "audit")
        self.assertEqual(snap["round"]["autonomy"], "semi")
        self.assertEqual(snap["round"]["writer"], "writer-agent")
        self.assertEqual(snap["round"]["writer_reason"], "назначено человеком")
        self.assertFalse(snap["round"]["closed"])

    def test_done_phase_is_closed(self):
        self.store.mutate(lambda state: state.__setitem__("phase", "done"))
        self.assertTrue(build_snapshot(self.root)["round"]["closed"])

    def test_roles_carry_writer_flag_and_writer_comes_first(self):
        self.store.mutate(lambda state: state.update(writer="writer-agent", roles={
            "zeta": {"pid": 0, "last_seen": _iso(5)},
            "writer-agent": {"pid": os.getpid(), "last_seen": _iso(1), "sends": 7},
        }))
        roles = build_snapshot(self.root)["roles"]
        self.assertEqual([row["role"] for row in roles], ["writer-agent", "zeta"])
        self.assertTrue(roles[0]["is_writer"])
        self.assertFalse(roles[1]["is_writer"])
        self.assertEqual(roles[0]["sends"], 7)

    def test_role_with_a_recorded_pane_carries_its_human_name(self):
        """Роль с непустой панелью обязана собираться, а не падать.

        Пробел в покрытии, который пропустил сломанный импорт: до этой правки ни
        один тест не ставил роли поле `pane`, а ветка `resolve_pane_title(...) if
        pane_str else ""` вызывается только при непустой панели. То есть набор из
        92 тестов был зелёным на снимке, который падал с NameError на любой
        паре, у которой панель записана, — то есть на почти любой.
        """
        self.store.mutate(lambda state: state.__setitem__("roles", {
            "agy": {"pid": 1, "pane": "%999999", "last_seen": _iso(5)},
        }))
        row = build_snapshot(self.root)["roles"][0]
        self.assertEqual(row["pane"], "%999999")
        # Фантомной панели имя не выдумывается: об этом test_phantom_pane_*
        # в test_board_hosts, здесь важен сам факт, что строка собралась.
        self.assertIsInstance(row["pane_human"], str)

    def test_tasks_open_first_then_by_descending_id(self):
        self.store.mutate(lambda state: state.__setitem__("tasks", [
            {"id": 1, "text": "старая", "status": "done", "round": 2},
            {"id": 3, "text": "новая", "status": "open", "round": 3, "reason": "важно"},
            {"id": 2, "text": "средняя", "status": "dropped", "round": 2, "reason": "перенесено"},
        ]))
        tasks = build_snapshot(self.root)["tasks"]
        self.assertEqual([task["id"] for task in tasks], [3, 2, 1])
        self.assertEqual(tasks[0]["reason"], "важно")
        self.assertEqual(build_snapshot(self.root)["counts"]["tasks_open"], 1)

    def test_events_carry_raw_kind_and_one_line_summary(self):
        for entry in ({"kind": "send", "role": "a", "to": "b", "subject": "привет", "seq": 1},
                      {"kind": "advance", "role": "a", "phase": "verify"}):
            self.store.append_transcript(entry["kind"], entry["role"], **{k: v for k, v in entry.items()
                                                                        if k not in ("kind", "role")})
        events = build_snapshot(self.root, limit=10)["events"]
        kinds = [event["kind"] for event in events]
        self.assertIn("send", kinds)
        self.assertIn("advance", kinds)
        by_kind = {event["kind"]: event for event in events}
        self.assertEqual(by_kind["send"]["summary"], "привет")
        self.assertEqual(by_kind["send"]["to"], "b")
        self.assertEqual(by_kind["advance"]["summary"], "verify")


class SnapshotRefusesToLieTest(PairTestCase):
    def test_broken_state_json_yields_read_error_not_exception(self):
        (self.root / "state.json").write_text("{ это не json", encoding="utf-8")
        snap = build_snapshot(self.root)
        self.assertTrue(snap["read_error"])
        self.assertEqual(snap["roles"], [])
        self.assertIsInstance(snap["tasks"], list)

    def test_missing_state_file_is_not_an_error(self):
        # `Store` создаёт state.json при первой записи, а не при создании
        # каталога, поэтому в свежем хранилище файла может не быть вовсе — и
        # это тоже не ошибка.
        (self.root / "state.json").unlink(missing_ok=True)
        snap = build_snapshot(self.root)
        self.assertEqual(snap["read_error"], "")
        self.assertEqual(snap["round"]["phase"], "scoping")

    def test_absent_pid_is_not_a_dead_pid(self):
        self.store.mutate(lambda state: state.__setitem__("roles", {"agy": {"pid": 0}}))
        role = build_snapshot(self.root)["roles"][0]
        self.assertIsNone(role["pid"])
        self.assertFalse(role["pid_alive"])

    def test_broken_transcript_line_is_shown_not_silently_dropped(self):
        path = self.root / "transcript.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "send", "role": "a", "subject": "до мусора"}\n')
            handle.write("не json вовсе\n")
        events = build_snapshot(self.root, limit=5)["events"]
        kinds = [event["kind"] for event in events]
        # Обрыв на середине строки — обычное дело при убитом процессе, и `Store`
        # отдаёт такую строку помеченной, а не выкидывает. Проверка требует именно
        # этого: фантома с именем «?» быть не должно, а сама строка — видна.
        self.assertIn("send", kinds)
        self.assertIn("damaged", kinds)
        self.assertNotIn("?", kinds)
        damaged = [event for event in events if event["kind"] == "damaged"][0]
        self.assertEqual(damaged["summary"], "не json вовсе")


class SummariseEventTest(unittest.TestCase):
    def test_prefers_the_first_field_that_says_something(self):
        self.assertEqual(summarise_event({"kind": "send", "subject": "тема", "why": "нет"}),
                         "тема")
        self.assertEqual(summarise_event({"kind": "evidence", "value": "289 тестов"}),
                         "289 тестов")

    def test_unknown_kind_keeps_its_name(self):
        self.assertEqual(summarise_event({"kind": "выдуманное"}), "выдуманное")

    def test_empty_fields_do_not_win_over_later_ones(self):
        self.assertEqual(summarise_event({"kind": "note", "subject": "", "text": "тело"}),
                         "тело")

    def test_newlines_never_reach_the_feed_as_lines(self):
        self.assertEqual(summarise_event({"kind": "note", "text": "раз\nдва"}), "раз два")

    def test_long_values_are_clipped(self):
        summary = summarise_event({"kind": "note", "text": "x" * 500})
        self.assertEqual(len(summary), board_snapshot.MAX_VALUE_CHARS)
        self.assertTrue(summary.endswith("…"))


class ClipAndAgeTest(unittest.TestCase):
    def test_clip_leaves_short_text_alone(self):
        self.assertEqual(_clip("коротко"), "коротко")

    def test_clip_handles_none_as_empty(self):
        self.assertEqual(_clip(None), "")

    def test_age_of_absent_or_broken_stamp_is_none(self):
        self.assertIsNone(_age(None))
        self.assertIsNone(_age("не дата"))
        self.assertIsNone(_age(""))

    def test_age_of_fresh_stamp_is_small_and_never_negative(self):
        self.assertLess(_age(_iso(2)), 10)
        self.assertGreaterEqual(_age(_iso(-50)), 0.0)


class SnapshotJsonTest(PairTestCase):
    def test_closing_tag_in_data_cannot_close_the_page(self):
        snap = build_snapshot(self.root)
        snap["round"]["task"] = "</script><img src=x onerror=alert(1)>"
        text = snapshot_json(snap)
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertNotIn("</script", text)
        restored = json.loads(text)
        self.assertEqual(restored["round"]["task"], "</script><img src=x onerror=alert(1)>")

    def test_ampersand_survives_as_itself_not_as_an_entity(self):
        snap = build_snapshot(self.root)
        snap["round"]["task"] = "a & b"
        restored = json.loads(snapshot_json(snap))
        self.assertEqual(restored["round"]["task"], "a & b")

    def test_backslashes_are_not_double_escaped(self):
        snap = build_snapshot(self.root)
        snap["round"]["task"] = r"путь C:\каталог\файл"
        restored = json.loads(snapshot_json(snap))
        self.assertEqual(restored["round"]["task"], r"путь C:\каталог\файл")

    def test_json_line_separators_are_escaped_for_js(self):
        snap = build_snapshot(self.root)
        snap["round"]["task"] = "до\u2028после\u2029"
        text = snapshot_json(snap)
        self.assertNotIn("\u2028", text)
        self.assertNotIn("\u2029", text)
        self.assertEqual(json.loads(text)["round"]["task"], "до\u2028после\u2029")

    def test_snapshot_is_json_serialisable_as_is(self):
        json.dumps(build_snapshot(self.root), ensure_ascii=False)


class DefaultsTest(PairTestCase):
    def test_stale_after_default_is_used_when_not_given(self):
        self.assertEqual(build_snapshot(self.root)["stale_after"], DEFAULT_STALE_AFTER)

    def test_snapshot_records_where_it_read_from(self):
        snap = build_snapshot(self.root)
        self.assertEqual(snap["state_dir"], str(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)