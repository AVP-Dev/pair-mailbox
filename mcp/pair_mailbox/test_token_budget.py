#!/usr/bin/env python3
"""The token budget: what a read is allowed to cost.

Three properties, all bought by round 135:

- the estimate over-counts, never shaves: a budget that under-counts is a budget the
  agent cannot trust, and the room writes in two scripts;
- a cut lands on a record edge, never inside a record: a half record reads as a whole
  one, and the reader acts on it;
- a prefix cut fits the budget it was given.

The ratios below are the measured ones (cl100k_base, 2026-10-21): about 4 characters
per token in English, about 2.6 in Russian. The old bytes/4 estimate under-counted the
second by a third, and every budget built on it was a budget in one language only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import token_budget  # noqa: E402


class TestTokenEstimate(unittest.TestCase):
    def test_an_empty_text_costs_nothing(self) -> None:
        self.assertEqual(0, token_budget.estimate_tokens(""))

    def test_the_estimate_over_counts_english(self) -> None:
        """English runs about 4 characters per token; the estimate must sit above it."""
        text = "the quick brown fox jumps over the lazy dog, and does it twice over"
        self.assertGreaterEqual(token_budget.estimate_tokens(text), len(text) / 4.0)

    def test_the_estimate_over_counts_russian(self) -> None:
        """Russian runs about 2.6 characters per token — the case bytes/4 missed."""
        text = "быстрая коричневая лиса перепрыгивает через ленивую собаку дважды"
        self.assertGreaterEqual(token_budget.estimate_tokens(text), len(text) / 2.6)

    def test_a_mixed_text_is_counted_by_both_scripts(self) -> None:
        text = "the fox привет the лиса jumped"
        ascii_chars = sum(1 for ch in text if ch.isascii())
        other = len(text) - ascii_chars
        self.assertGreaterEqual(token_budget.estimate_tokens(text),
                                ascii_chars / 4.0 + other / 2.6)

    def test_the_estimate_grows_with_the_text(self) -> None:
        short = token_budget.estimate_tokens("one fact")
        long = token_budget.estimate_tokens("one fact " * 100)
        self.assertLess(short, long)


class TestPrefixFit(unittest.TestCase):
    def test_the_prefix_fits_the_budget(self) -> None:
        text = "слово " * 500
        for budget in (10, 50, 200, 800):
            cut = token_budget.fit_prefix(text, budget)
            self.assertLessEqual(token_budget.estimate_tokens(cut), budget)
            self.assertTrue(text.startswith(cut), "the cut is not a prefix")

    def test_a_text_that_fits_comes_back_whole(self) -> None:
        text = "short enough"
        self.assertEqual(text, token_budget.fit_prefix(text, 100))


class TestRecordFitting(unittest.TestCase):
    def _records(self, count: int, size: int = 10) -> list[list[str]]:
        return [["## 2026-10-21T10:00:00+00:00 — agy", ""] + ["x" * size] * 3
                for _ in range(count)]

    def test_the_body_splits_on_record_edges(self) -> None:
        records = token_budget.split_records(["## one", "a", "## two", "b"])
        self.assertEqual(2, len(records))
        self.assertEqual(["## one", "a"], records[0])
        self.assertEqual(["## two", "b"], records[1])

    def test_the_newest_records_are_the_ones_kept(self) -> None:
        records = self._records(20)
        kept, _ = token_budget.fit_records_to_budget(records, 100)
        self.assertEqual(records[-len(kept):], kept)

    def test_the_budget_holds_across_many_records(self) -> None:
        records = self._records(50)
        kept, tokens = token_budget.fit_records_to_budget(records, 200)
        self.assertLessEqual(tokens, 200)
        self.assertTrue(kept)

    def test_a_record_bigger_than_the_budget_comes_whole(self) -> None:
        """The fact, not a silent trim: the caller is told the budget did not hold."""
        records = [["## one", "y" * 5000]]
        kept, tokens = token_budget.fit_records_to_budget(records, 100)
        self.assertEqual(1, len(kept))
        self.assertGreater(tokens, 100)

    def test_no_records_means_no_records(self) -> None:
        kept, tokens = token_budget.fit_records_to_budget([], 100)
        self.assertEqual([], kept)
        self.assertEqual(0, tokens)


if __name__ == "__main__":
    unittest.main()
