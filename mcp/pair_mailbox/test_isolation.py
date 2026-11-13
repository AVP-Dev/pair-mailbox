"""Tests for isolation.py — the declared strategy and whether it fits the change.

The contract under test is a refusal contract: the module exists to say "no"
in the cases where a writer's declaration does not match the work. So the
tests below assert the refusals first and the permissions second — a matrix
that only ever says "yes" is not a matrix.
"""
from __future__ import annotations

import unittest

from pair_test_base import PairTestCase
import isolation


PLAN_WITH = """round: 147

# Goal

Something.

# Isolation Strategy

mode: {mode}
worktrees: {worktrees}
slices: {slices}
rationale: because

# Acceptance

Something.
"""


class TestParseStrategy(PairTestCase):
    def test_a_plan_without_the_section_refuses(self) -> None:
        """No declaration is NOT a declaration of `in-place`."""
        res = isolation.parse_strategy("round: 147\n\n# Goal\n\nSomething.\n")
        self.assertFalse(res["ok"])
        self.assertIn("no `# Isolation Strategy`", res["why"])

    def test_a_section_without_a_mode_refuses(self) -> None:
        res = isolation.parse_strategy(
            "round: 147\n\n# Isolation Strategy\n\nrationale: because\n")
        self.assertFalse(res["ok"])
        self.assertIn("names no mode", res["why"])

    def test_in_place_is_parsed(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="in-place", worktrees=0, slices=1))
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["mode"], "in-place")
        self.assertEqual(res["worktrees"], 0)
        self.assertEqual(res["slices"], 1)
        self.assertEqual(res["rationale"], "because")

    def test_worktree_is_parsed(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="worktree", worktrees=1, slices=1))
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["mode"], "worktree")
        self.assertEqual(res["worktrees"], 1)

    def test_decomposed_is_parsed(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="decomposed", worktrees=0, slices=3))
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["mode"], "decomposed")
        self.assertEqual(res["slices"], 3)
        self.assertEqual(res["worktrees"], 0)

    def test_the_russian_spelling_is_accepted(self) -> None:
        """The protocol is bilingual, so a Russian plan must satisfy the gate."""
        res = isolation.parse_strategy(
            "round: 147\n\n# Стратегия изоляции\n\nmode: worktree\nworktrees: 1\nslices: 1\n")
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["mode"], "worktree")

    def test_prose_is_not_a_declaration(self) -> None:
        """A sentence is not a decision.

        "No worktree is needed here" would otherwise DECLARE a worktree, and
        "the branch is round-147" would declare `in-place` — both from a
        sentence that said the opposite. The declaration is the `mode:` field.
        """
        res = isolation.parse_strategy(
            "round: 147\n\n# Isolation Strategy\n\nNo worktree is needed here.\n")
        self.assertFalse(res["ok"])
        self.assertIn("names no mode", res["why"])

    def test_a_fenced_example_is_not_a_declaration(self) -> None:
        """A plan that DOCUMENTS the format inside a fence has not chosen one.

        The round-147 plan itself carries such a fence, showing the shape of
        the section. Reading the example as the declaration would invert the
        strategy of every plan that documents one.
        """
        plan = ("round: 147\n\n# Isolation Strategy\n\n"
                "The format is:\n\n```\nmode: worktree\nworktrees: 1\n```\n")
        res = isolation.parse_strategy(plan)
        self.assertFalse(res["ok"])
        self.assertIn("names no mode", res["why"])

    def test_in_place_with_worktrees_is_a_contradiction(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="in-place", worktrees=2, slices=1))
        self.assertFalse(res["ok"])
        self.assertIn("disagree", res["why"])

    def test_decomposed_below_two_slices_refuses(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="decomposed", worktrees=0, slices=1))
        self.assertFalse(res["ok"])
        self.assertIn("at least 2 slices", res["why"])

    def test_a_worktree_count_that_differs_from_the_slice_count_refuses(self) -> None:
        """One worktree per slice, or none for sequential slices — not two of three."""
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="decomposed", worktrees=2, slices=3))
        self.assertFalse(res["ok"])
        self.assertIn("must equal the slice count", res["why"])

    def test_worktree_mode_with_several_slices_refuses(self) -> None:
        res = isolation.parse_strategy(
            PLAN_WITH.format(mode="worktree", worktrees=1, slices=3))
        self.assertFalse(res["ok"])
        self.assertIn("use `decomposed`", res["why"])

    def test_the_section_stops_at_the_next_heading(self) -> None:
        """A `mode:` line under a LATER heading is not this section's mode."""
        plan = ("round: 147\n\n# Isolation Strategy\n\nmode: worktree\n"
                "worktrees: 1\nslices: 1\n\n# Acceptance\n\nmode: in-place\n"
                "worktrees: 0\nslices: 1\n")
        res = isolation.parse_strategy(plan)
        self.assertTrue(res["ok"], res["why"])
        self.assertEqual(res["mode"], "worktree")


class TestSizeMatrix(PairTestCase):
    def test_the_class_boundaries(self) -> None:
        self.assertEqual(isolation.size_class(0), "small")
        self.assertEqual(isolation.size_class(50), "small")
        self.assertEqual(isolation.size_class(51), "medium")
        self.assertEqual(isolation.size_class(300), "medium")
        self.assertEqual(isolation.size_class(301), "large")

    def test_decide_follows_the_matrix(self) -> None:
        self.assertEqual(isolation.decide(20)["mode"], "in-place")
        self.assertEqual(isolation.decide(20)["worktrees"], 0)
        self.assertEqual(isolation.decide(120)["mode"], "worktree")
        self.assertEqual(isolation.decide(120)["worktrees"], 1)
        self.assertEqual(isolation.decide(900)["mode"], "decomposed")
        self.assertEqual(isolation.decide(900)["slices"], 2)


class TestValidate(PairTestCase):
    def _strategy(self, mode: str, worktrees: int, slices: int) -> dict:
        return isolation.parse_strategy(
            PLAN_WITH.format(mode=mode, worktrees=worktrees, slices=slices))

    def test_a_small_in_place_change_passes(self) -> None:
        ok, why = isolation.validate(self._strategy("in-place", 0, 1), 20)
        self.assertTrue(ok, why)

    def test_an_in_place_change_that_grew_is_refused(self) -> None:
        """The rule is checked against the FACTUAL diff, not the promise."""
        ok, why = isolation.validate(self._strategy("in-place", 0, 1), 200)
        self.assertFalse(ok)
        self.assertIn("in-place", why)
        self.assertIn("200", why)

    def test_a_medium_single_worktree_passes(self) -> None:
        ok, why = isolation.validate(self._strategy("worktree", 1, 1), 120)
        self.assertTrue(ok, why)

    def test_a_single_worktree_that_overshot_is_refused(self) -> None:
        ok, why = isolation.validate(self._strategy("worktree", 1, 1), 800)
        self.assertFalse(ok)
        self.assertIn("decomposed", why)

    def test_a_decomposed_large_change_passes(self) -> None:
        ok, why = isolation.validate(self._strategy("decomposed", 0, 3), 800)
        self.assertTrue(ok, why)

    def test_a_decomposition_that_overshot_its_slices_is_refused(self) -> None:
        """Three slices of 300 lines cannot hold 1000 changed lines."""
        ok, why = isolation.validate(self._strategy("decomposed", 0, 3), 1000)
        self.assertFalse(ok)
        self.assertIn("cannot hold it", why)
        self.assertIn("900", why)

    def test_an_unparsed_strategy_refuses(self) -> None:
        ok, why = isolation.validate({"ok": False, "why": "nothing parsed"}, 10)
        self.assertFalse(ok)
        self.assertIn("nothing parsed", why)

    def test_the_round_147_own_declaration_is_consistent(self) -> None:
        """The round's own strategy must satisfy the rule it introduces.

        A rule that the round that writes it violates is a rule with a hole in
        the one place it is most likely to be applied.
        """
        strategy = self._strategy("decomposed", 0, 3)
        ok, why = isolation.validate(strategy, 800)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
