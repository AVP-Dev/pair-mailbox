#!/usr/bin/env python3
"""The pointers this system types, and the leftovers it may clean up.

A pointer the neighbour never accepted stays in its composer: the composer is
then not empty, so every later delivery into that pane is refused and the pane
stays wedged until a human clears it by hand. Measured live: 7
`deliver_pointer_busy` and 10 `doorbell_refused` rows, every one of them
naming the leftover pointer in its reason.

The cleanup must therefore erase OUR OWN text and nothing else. The human's
typing shares that composer, and eating it is worse than the wedge.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pointers  # noqa: E402


class TestPointerRecognition(unittest.TestCase):
    def test_the_ring_prompt_of_this_role_is_ours(self) -> None:
        prompt = pointers.ring_prompt("agy")
        self.assertEqual(len(prompt),
                         pointers.own_pointer_length("╰─ " + prompt, "agy"))

    def test_the_detail_of_a_ring_is_part_of_our_pointer(self) -> None:
        prompt = pointers.ring_prompt("agy", "message", "3 unread")
        self.assertEqual(len(prompt),
                         pointers.own_pointer_length("╰─ " + prompt, "agy"))

    def test_the_mailbox_pointer_of_this_role_is_ours(self) -> None:
        pointer = pointers.mailbox_pointer("agy", 1559)
        self.assertEqual(len(pointer),
                         pointers.own_pointer_length("╰─ " + pointer, "agy"))

    def test_a_pointer_of_another_role_is_not_ours(self) -> None:
        self.assertEqual(0, pointers.own_pointer_length(
            "╰─ " + pointers.ring_prompt("omp"), "agy"),
            "another agent's pointer is not ours to erase")
        self.assertEqual(0, pointers.own_pointer_length(
            "╰─ " + pointers.mailbox_pointer("omp", 1559), "agy"))

    def test_the_human_typing_is_never_ours(self) -> None:
        self.assertEqual(0, pointers.own_pointer_length("╰─ hello there", "agy"))
        self.assertEqual(0, pointers.own_pointer_length("╰─ ", "agy"))
        self.assertEqual(0, pointers.own_pointer_length("", "agy"))
        self.assertEqual(0, pointers.own_pointer_length(None, "agy"))

    def test_a_composer_holding_more_than_our_pointer_is_not_ours(self) -> None:
        line = "╰─ the human wrote this " + pointers.ring_prompt("agy")
        self.assertEqual(0, pointers.own_pointer_length(line, "agy"),
                         "ours is not the only text there; erasing by count "
                         "would eat the human's words")

    def test_a_prompt_marker_before_our_pointer_is_still_ours(self) -> None:
        prompt = pointers.ring_prompt("agy")
        self.assertEqual(len(prompt),
                         pointers.own_pointer_length("> " + prompt, "agy"))

    def test_a_cursor_block_after_our_pointer_is_chrome_not_text(self) -> None:
        prompt = pointers.ring_prompt("agy")
        self.assertEqual(len(prompt),
                         pointers.own_pointer_length("╰─ " + prompt + "█", "agy"))


class TestClearOwnPointer(unittest.TestCase):
    def _clear(self, line: str, role: str = "agy") -> tuple[int, list[int]]:
        erased: list[int] = []

        def fake_erase(_tmux, _pane, count):
            erased.append(count)

        with mock.patch.object(pointers, "capture", return_value=[line]), \
             mock.patch.object(pointers, "composer_line",
                               side_effect=lambda lines: lines[0]), \
             mock.patch.object(pointers, "erase", side_effect=fake_erase):
            count = pointers.clear_own_pointer("tmux", "%1", role)
        return count, erased

    def test_only_our_own_pointer_is_erased(self) -> None:
        prompt = pointers.ring_prompt("agy")
        count, erased = self._clear("╰─ " + prompt)
        self.assertEqual(len(prompt), count)
        self.assertEqual([len(prompt)], erased,
                         "exactly our own text goes, by its own length")

    def test_a_composer_with_the_human_text_is_left_alone(self) -> None:
        count, erased = self._clear("╰─ the human wrote this")
        self.assertEqual(0, count)
        self.assertEqual([], erased, "the human's typing is never erased")

    def test_a_pane_we_cannot_read_is_left_alone(self) -> None:
        with mock.patch.object(pointers, "capture", side_effect=OSError("no tmux")), \
             mock.patch.object(pointers, "erase") as erase:
            count = pointers.clear_own_pointer("tmux", "%1", "agy")
        self.assertEqual(0, count)
        erase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
