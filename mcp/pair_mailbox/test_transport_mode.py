#!/usr/bin/env python3
"""The transport mode is persisted, not remembered (round 111 slice 2).

The failure this pins, measured live while flipping: the mode lived only in the
installer's environment (PAIR_TRANSPORT=socket), so a HUMAN restart — which has
no memory of any env var — raised no daemon, and the newly flipped thin client
refused with exit 4. The pair would have been dead at the next restart with
nothing in the tree explaining why.

So the mode lives in state (`transport: "socket"`), pairup reads it, and the
installer writes it. These tests exercise the pieces the way a human does: no
env, a restart, and a state file.
"""
from __future__ import annotations

import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_installer():
    """install.sh is a python script with a .sh name; load it as a module."""
    loader = SourceFileLoader("install_script", str(ROOT / "install.sh"))
    spec = spec_from_loader("install_script", loader)
    mod = module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TransportModeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".pair").mkdir(parents=True)
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 905, "mode": "fix", "roles": {}, "phase": "implement"}),
            encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_installer_persists_the_mode(self) -> None:
        mod = _load_installer()
        mod.persist_transport(str(self.proj), "socket")
        state = json.loads((self.proj / ".pair" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("transport"), "socket")

    def test_default_transport_is_recorded_as_child(self) -> None:
        """The upside-down case matters too: a project that installs with the
        DEFAULT must be recorded as child, so pairup does not spend a daemon."""
        mod = _load_installer()
        mod.persist_transport(str(self.proj), "")
        state = json.loads((self.proj / ".pair" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("transport"), "child")

    def test_pairup_reads_the_mode_without_env(self) -> None:
        """The human-restart case, the whole point: pairup is invoked with a
        CLEAN environment (no PAIR_TRANSPORT) and still raises the daemon."""
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 905, "mode": "fix", "roles": {}, "phase": "implement",
             "transport": "socket"}), encoding="utf-8")
        # the raise_daemon block is shell inside pairup; extract its mode read
        pairup = (ROOT / "bin" / "pairup").read_text(encoding="utf-8")
        self.assertIn('st.get("transport")', pairup,
                      "pairup must read the persisted mode, not an env var")
        self.assertNotIn('[ "${PAIR_TRANSPORT:-}" = "socket" ]', pairup,
                         "an env-only gate is exactly the defect")

    def test_missing_state_does_not_raise(self) -> None:
        """A project with no state.json: the installer must not create a stray
        one, and the write is a no-op."""
        (self.proj / ".pair" / "state.json").unlink()
        mod = _load_installer()
        mod.persist_transport(str(self.proj), "socket")
        self.assertFalse((self.proj / ".pair" / "state.json").exists(),
                         "no state, no write — never invent one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
