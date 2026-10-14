"""Digest tests: a pair-state slice under a hard token ceiling.

One test file per source: `digest.py` owns one duty — fitting state into budget —
and checks here, never mixed with protocol.

Three properties check, never declare:
- the ceiling holds on any state, including a deliberately bloated one;
- section order decides all: a permutation costs more at the same content;
- the verdict quotes, never retells, because retelling a foreign decision in own
  words is a way to lie about what was never read.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import main  # noqa: E402

import digest  # noqa: E402


class TestDigest(PairTestCase):
    """The digest: a state slice in a hard token budget (item 5).

    Why it stands, measured before writing it: orienting in the pair costs
    `pair_state` 939 + `pair_task list` 1051 + `pair_transcript` 1734 = 3724 tokens.
    The digest must run notably cheaper, or it is unneeded.

    Three properties checking here, never declared:
    - **the ceiling holds** on any state, including a deliberately bloated one;
    - **section order decides all**: a permutation costs more at the same content;
    - **the verdict quotes, never retells**, because retelling a foreign decision in
      own words is a way to lie about what was never read.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def digest(self, budget: int = 500, role: str = "") -> str:
        return self.server.build_digest(self.store, role, budget)

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def with_state(self, **fields: object) -> None:
        self.store.mutate(lambda st: st.update(fields))

    def many_tasks(self, count: int, status: str = "open") -> None:
        self.store.mutate(lambda st: st.setdefault("tasks", []).extend(
            {"id": 100 + i, "text": f"task number {i} " + "tail " * 12,
             "status": status, "added_at": "then", "added_by": "human", "round": 14}
            for i in range(count)))

    # ------------------------------------------------------------- ceiling

    def test_the_digest_fits_the_budget(self) -> None:
        """The ceiling is a property, not a wish.

        A digest that stopped fitting reads worse than a missing one: the agent
        will plan context on a ceiling that no longer holds.
        """
        self.with_state(task="automation of pair work: the pilot supervisor, roles, modes",
                        writer="opencode", mode="fix", phase="implement", round=14)
        self.many_tasks(6)
        text = self.digest()
        self.assertLessEqual(self.server.estimate_tokens(text), 500,
                             "the digest must fit the ceiling")

    def test_the_budget_holds_on_a_deliberately_bloated_state(self) -> None:
        """The ceiling holds on a deliberately bloated state.

        Checking a cheap digest on a normal state proves nothing: it fits anyway.
        It breaks on the unexpected — twenty open tasks with long texts and no git.
        """
        self.with_state(task="task " * 400, writer="agy", mode="fix",
                        phase="verify", round=99)
        self.many_tasks(20, status="open")
        text = self.digest()
        self.assertLessEqual(self.server.estimate_tokens(text), 500)

    def test_a_tiny_budget_is_still_honoured(self) -> None:
        """A small ceiling is a ceiling too, not a landmark.

        Had trimming failed, an agent on a bounded context would take the full text
        learning it by fact, hitting the limit.
        """
        self.with_state(task="task " * 200, writer="agy")
        self.many_tasks(8)
        text = self.digest(budget=80)
        self.assertLessEqual(self.server.estimate_tokens(text), 80)

    def test_what_did_not_fit_is_said(self) -> None:
        """Trimming must declare itself.

        A silently shortened digest reads worse than a long one: the agent decides no
        verdict stood, starting to search it where it never was.
        """
        self.with_state(task="task " * 400, writer="agy")
        self.many_tasks(20)
        text = self.digest(budget=120)
        self.assertIn("truncated", text)

    # ----------------------------------------------------------- content

    def test_the_reserve_covers_the_marker(self) -> None:
        """The reserve must at least cover the mark itself.

        Found on self: the reserve stood at a round 20, the mark ran 22, and on text
        fit exactly to the ceiling the digest gave 501 tokens at ceiling 500 — meaning
        it either broke the promise or cleaned the line it gathered for. Both read
        bad, both read as "nearly works".
        """
        self.assertGreaterEqual(self.server._digest_marker_reserve(),
                                self.server.estimate_tokens(self.server.DIGEST_MARKER),
                                "a reserve below the mark: the digest pays the mark at the price of "
                                "a cleaned useful line")

    def test_an_oversized_line_is_cut_rather_than_dropped(self) -> None:
        """A cut first line beats its absence.

        Here the two ways of holding the ceiling differ, and not cosmetically.
        Dropping the line whole keeps budget, but the agent never sees the task,
        forced to call `pair_state`. Cutting it, the agent sees the task start,
        understanding what the round is about.

        Mutation-checked: dropping only the line cut, the ceiling still holds (take
        and the final cleanup hold it), the suite staying green. So with no this
        check the cut would stand dead code.
        """
        self.with_state(task="TASK-VERY-LONG " + "details " * 400)
        text = self.digest(budget=200)
        self.assertLessEqual(self.server.estimate_tokens(text), 200)
        self.assertIn("TASK-VERY-LONG", text,
                      "the task start must show, or the agent never knows what the round is about")
        self.assertIn("…", text, "trimming must mark itself")

    def test_the_digest_states_what_cannot_be_guessed(self) -> None:
        """Task, mode, phase and writer — what work goes wrong without.

        A missing task is a fact too, not blank: an empty line reads as "a task
        stands, but empty", and those differ.
        """
        text = self.digest()
        self.assertIn("ROUND", text)
        self.assertIn("task:", text)
        self.assertIn("mode:", text)
        self.assertIn("phase:", text)
        self.assertIn("writes:", text)

    def test_closed_tasks_do_not_take_space(self) -> None:
        """Closed tasks carry no meaning for the next step.

        Eight closed tasks cost near 800 tokens in `pair_task list`, saying nothing
        the agent never knows.
        """
        self.many_tasks(6, status="done")
        text = self.digest()
        self.assertIn("OPEN TASKS: 0", text)
        self.assertNotIn("task number", text)

    def test_missing_obligations_are_stated(self) -> None:
        """No obligations is a fact, not a blank in the text."""
        text = self.digest()
        self.assertIn("OBLIGATIONS: 0", text)
        self.assertIn("nobody waits on anyone", text)

    def test_an_open_obligation_is_named(self) -> None:
        """Who awaits whom is what the agent reads the digest for."""
        self.store.mutate(lambda st: None)
        path = self.store.obligations_root
        path.mkdir(parents=True, exist_ok=True)
        (path / "o1.json").write_text(json.dumps(
            {"from": "opencode", "to": "agy", "subject": "item 5"}, ensure_ascii=False),
            encoding="utf-8")
        text = self.digest()
        self.assertIn("opencode waits agy", text)

    def test_the_last_commit_is_read_from_git_not_from_the_log(self) -> None:
        """The live tree state, not a journal retelling.

        The journal never knows what stands committed. An agent reading the digest
        must see the tree truth, or it plans work on a stale view.
        """
        text = self.digest()
        self.assertIn("LAST COMMIT", text)

    def test_no_git_is_said_out_loud(self) -> None:
        """Missing git declares itself, never reads as "no commits".

        Different facts with different consequences: the first means "watch it
        yourself", the second "the tree is clean, nothing to do".
        """
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            text = self.digest()
        self.assertIn("git unreachable", text)

    # -------------------------------------------------------------- verdict

    def _note_in_journal(self, slug: str, role: str = "agy") -> None:
        """A note registered in the journal the same way `pair_note` writes it."""
        self.store.append_transcript("note", role, note_kind=slug, chars=10)
        path = self.store.notes_root / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A note longer than the tail: had the digest taken the head, the "PAST ROUND
        # SETUP" would land in it instead of the decision.
        body = [f"line {i} of the setup" for i in range(12)]
        path.write_text("\n".join(body + ["VERDICT: accepted."]) + "\n", encoding="utf-8")

    def test_the_last_verdict_is_quoted_not_summarised(self) -> None:
        """A quote, not a retelling.

        The digest retelling a foreign decision in own words can lie about what it never
        read. So the note literal tail lands in it, showing whose verdict it is.
        """
        self._note_in_journal("verdict-past")
        self._note_in_journal("verdict-fresh")
        text = self.digest()
        self.assertIn("VERDICT: accepted.", text)
        self.assertIn("verdict-fresh", text)
        self.assertNotIn("line 0 of the setup", text,
                         "the digest takes the note tail, not its head")

    def test_the_newest_verdict_wins_regardless_of_name(self) -> None:
        """Verdict order comes from the journal, not the file name.

        Names never order by time: `verdict.md`, `verdict-agy.md`,
        `verdict-pairctl-pane.md` — which runs last never shows by name. Ninety-two
        notes, and reading their content to find the last is exactly the cost the
        digest promises never to pay.
        """
        # First one in the journal, then the other: the journal sets the order.
        self._note_in_journal("verdict-aaa")
        self._note_in_journal("verdict-zzz")
        text = self.digest()
        self.assertIn("verdict-zzz", text)

    def test_no_verdict_is_stated_as_no_verdict(self) -> None:
        """A missing verdict says so directly.

        Or a blank section reads as "a verdict stands, but never fit".
        """
        text = self.digest()
        self.assertIn("LAST VERDICT", text)
        self.assertIn("no verdicts yet", text)

    # ------------------------------------------------------------- surfaces

    def test_the_tool_and_the_command_return_the_same_thing(self) -> None:
        """One implementation for the human and the agent.

        Two digest implementations would drift, and the human would see what the
        agent never sees — a drift in such a thing surfacing only at an incident autopsy.
        incident.
        """
        self.with_state(task="one and the same task", writer="opencode")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, self.ctl("digest", "--budget", "400"))
        self.assertEqual(self.server.build_digest(self.store, "", 400), out.getvalue())

    def test_the_command_fails_loudly_when_the_budget_is_broken(self) -> None:
        """A ceiling never held is breakage, not a cause to pretend.

        A silent exit with code 0 would hand the human text taken for a digest,
        leaning on it.
        """
        # Stubbing the consumer module: `cmd_digest` lives in `pairctl_read`, taking
        # `build_digest` from there, not from `server`. Stubbing `server` past parsing
        # would change nothing, testing the wrong code.
        with mock.patch("pairctl_read.build_digest", return_value="x" * 4000):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = self.ctl("digest", "--budget", "100")
        self.assertEqual(1, code)
        self.assertIn("ceiling", err.getvalue())

    def test_the_digest_names_the_role_that_writes(self) -> None:
        """With a passed role the digest says "you write".

        It spares the agent a second call, and the meaning is no beauty but that the
        whole digest meaning is dropping the extra state round-trip.
        """
        self.with_state(writer="opencode")
        self.assertIn("(you)", self.digest(role="opencode"))
        self.assertNotIn("(you)", self.digest(role="agy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
