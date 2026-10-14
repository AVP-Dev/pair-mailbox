"""Pane-ownership tests: whether it is ours and on what proof.

One test file per source file: `panes.py` owns one duty — telling whether a
pane belongs to our project — and it is checked here.

The key split the module exists for: "foreign-project pane" and "dead pane"
are different facts. `pane_belongs_to_project` never asks about liveness, and
exactly that is checked here: mixing them would brand a role with a dead pane
as "foreign" too, and stale-entry cleanup would stop firing where it alone works.

Membership checks run on live tmux and a real pane created by the suite inside
a temp project: stubbing `pane_identity` would prove the string comparison is
spelled right, not that the pane is recognized as ours.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import composer  # noqa: E402
import panes  # noqa: E402

TMUX = "tmux"


def have_tmux() -> bool:
    from shutil import which

    return bool(which(TMUX))


class PaneOwnershipBase(unittest.TestCase):
    """A temp project and a live pane inside it."""

    def setUp(self) -> None:
        if not have_tmux():
            self.skipTest("tmux not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".pair").mkdir(parents=True)
        self.pane = self._split(self.project)
        if not self.pane:
            self.skipTest("tmux gave no pane")
        # Let tmux set the cwd: the first poll returns the previous pane’s path.
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)

    def _split(self, where: Path) -> str:
        made = subprocess.run(
            [TMUX, "split-window", "-d", "-P", "-F", "#{pane_id}", "-c", str(where)],
            capture_output=True, text=True, timeout=10)
        pane = made.stdout.strip()
        if pane:
            self.addCleanup(subprocess.run, [TMUX, "kill-pane", "-t", pane],
                            capture_output=True, timeout=10)
        return pane

    def other_project(self) -> Path:
        """A second temp project with a live pane — the "foreign" side of the check."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = Path(tmp.name) / "other"
        (other / ".pair").mkdir(parents=True)
        self.other_pane = self._split(other)
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)
        return other


class TestPaneBelongsToProject(PaneOwnershipBase):
    def test_a_pane_of_this_project_is_ours(self) -> None:
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertTrue(ours, why)

    def test_a_pane_of_another_project_is_not_ours(self) -> None:
        other = self.other_project()
        ours, why = panes.pane_belongs_to_project(TMUX, self.other_pane, self.project)
        self.assertFalse(ours)
        self.assertIn(str(other), why)

    def test_a_panel_inside_the_project_counts_as_ours(self) -> None:
        """A pane may launch from a project subdir and still be ours.

        Rejecting that case is forbidden: an agent opening a second terminal
        inside the repo would lose its addressing.
        """
        nested = self.project / "sub" / "deep"
        nested.mkdir(parents=True)
        pane = self._split(nested)
        if not pane:
            self.skipTest("tmux gave no pane")
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)
        ours, why = panes.pane_belongs_to_project(TMUX, pane, self.project)
        self.assertTrue(ours, why)

    def test_a_panel_in_the_shared_parent_is_accepted_only_when_it_is_really_ours(
        self) -> None:
        """A shared parent like ~/work never makes another pane ours.

        Checked on the SECOND project’s pane, not ours: our pane sits at the
        project root, and "pane accepted at root as ours" proves equality, not
        parenthood. The parent is proven by the neighbour project’s pane being
        rejected at a root-parent: otherwise ~/work would declare the pane its
        own for several projects on this machine at once.
        """
        self.other_project()
        ours, why = panes.pane_belongs_to_project(
            TMUX, self.other_pane, self.project.parent)
        self.assertFalse(ours, why)

    def test_a_phantom_pane_is_not_proven(self) -> None:
        """tmux answers a missing pane with code 0 and empty output.

        Hence "pane missing", not "ours": the nonexistent cannot be proven.
        """
        ours, why = panes.pane_belongs_to_project(TMUX, "%999999", self.project)
        self.assertFalse(ours)
        self.assertIn("pane missing", why)

    def test_an_unreported_cwd_is_not_proven(self) -> None:
        """No cwd — no proof, and that is not "ours"."""
        with mock.patch.object(panes.composer, "pane_identity",
                               return_value={"exists": True, "alive": True,
                                             "path": "", "address": "x:0.0"}):
            ours, why = panes.pane_belongs_to_project(TMUX, "x:0.0", self.project)
        self.assertFalse(ours)
        self.assertIn("cwd", why)

    def test_a_pane_that_died_is_absent_not_foreign(self) -> None:
        """A killed pane is ABSENCE, not "foreign project".

        The distinction carries weight: stale roster-entry cleanup leans on
        "no pane", and swapping it for "foreign pane" would send the cleanup
        the wrong way. Checked on a real `kill-pane`, because a stubbed snapshot
        would prove nothing.
        """
        subprocess.run([TMUX, "kill-pane", "-t", self.pane],
                       capture_output=True, timeout=10)
        time.sleep(0.3)
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertFalse(ours)
        self.assertIn("pane missing", why)

    def test_a_live_pane_is_not_mistaken_for_a_dead_one(self) -> None:
        """The reverse side: a live pane proves membership, not "no".

        Exactly this miss would cost addressing refusal for the just-born agent.
        """
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertTrue(ours, why)


class TestPaneIsForeignStillRefusesToPrint(PaneOwnershipBase):
    def test_a_phantom_cannot_be_printed_to(self) -> None:
        foreign, why = panes.pane_is_foreign(TMUX, "%999999", self.project)
        self.assertTrue(foreign)
        self.assertIn("pane missing", why)

    def test_a_pane_that_died_cannot_be_printed_to(self) -> None:
        """The opposite side: printing is forbidden even when the project matches.

        Membership and printability are different questions, and both are closed.
        """
        subprocess.run([TMUX, "kill-pane", "-t", self.pane],
                       capture_output=True, timeout=10)
        time.sleep(0.3)
        foreign, why = panes.pane_is_foreign(TMUX, self.pane, self.project)
        self.assertTrue(foreign)
        self.assertIn("pane missing", why)

    def test_another_projects_pane_is_refused(self) -> None:
        self.other_project()
        foreign, why = panes.pane_is_foreign(TMUX, self.other_pane, self.project)
        self.assertTrue(foreign)
        self.assertIn("another project", why)

    def test_our_own_pane_is_printable(self) -> None:
        foreign, why = panes.pane_is_foreign(TMUX, self.pane, self.project)
        self.assertFalse(foreign, why)


class TestComposerDelegatesWithoutForkingBehaviour(PaneOwnershipBase):
    def test_composer_pane_is_foreign_matches(self) -> None:
        """The wrapper in `composer` must behave as `panes`.

        Otherwise delivery would print by one check while the roster drops the
        pane by another, and the drift would surface as "delivery broken at a
        live neighbour".
        """
        for pane, root in ((self.pane, self.project),
                           ("%999999", self.project)):
            self.assertEqual(panes.pane_is_foreign(TMUX, pane, root),
                             composer.pane_is_foreign(TMUX, pane, root),
                             f"{pane}: composer and panes must answer alike")

    def test_composer_belongs_matches(self) -> None:
        for pane, root in ((self.pane, self.project),
                           ("%999999", self.project)):
            self.assertEqual(panes.pane_belongs_to_project(TMUX, pane, root),
                             composer.pane_belongs_to_project(TMUX, pane, root))


class TestFindOwnPane(PaneOwnershipBase):
    def test_chain_meets_pane_pid_in_stub_tmux(self) -> None:
        """Hermetic: the ancestor chain meets the stub pane_pid — the pane is found
        with no live tmux. A skip here would mask the breakage."""
        import os
        real_run = composer.subprocess.run

        def stub(cmd, **kw):
            class R:
                stdout = f"%99 {os.getpid()}\n%100 1\n"
            return R()
        composer.subprocess.run = stub
        try:
            self.assertEqual(composer.find_pane_of_pid("tmux", os.getpid()), "%99")
        finally:
            composer.subprocess.run = real_run

    def test_empty_pane_list_gives_empty(self) -> None:
        """A stub with no panes is an honest "", not an invented pane."""
        real_run = composer.subprocess.run

        def stub(cmd, **kw):
            class R:
                stdout = ""
            return R()
        composer.subprocess.run = stub
        try:
            self.assertEqual(composer.find_own_pane("tmux"), "")
        finally:
            composer.subprocess.run = real_run

    def test_unknown_tmux_binary_gives_empty(self) -> None:
        """No tmux is an honest "", not an invented pane."""
        self.assertEqual(composer.find_own_pane("tmux-no-such-binary-xyz"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)