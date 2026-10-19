#!/usr/bin/env python3
"""The guards in serve.sh: WHERE the code runs, and WHICH role it may claim.

Two guards, one source file, one real fixture.

**The origin guard (round 108 slice 1).** The measured defect: my own MCP config
pointed at /tmp/wire_test, a scratch clone, and on restart that OLD code would
have taken my role silently — the role lock would have been free, so the foreign
tree would have served while everything looked correct. Round 104's
inventory-honesty class, our own instance.

**The role derivation (round 124).** The role comes from the MCP config, and the
environment may only DERIVE an instance of it ("agy" -> "agy-writer"): duo mode
needs two instances of one CLI, and the config is per CLI. A foreign role stays
a refusal — the 2026-10-07 case, where a process picked up a role it was not
given.

These tests run the REAL serve.sh from real trees, because the claims under test
are about WHERE code runs and WHICH role it may claim, and a mocked path proves
nothing about either.

Run: python3 mcp/pair_mailbox/test_origin_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVE = ROOT / "mcp" / "pair_mailbox" / "serve.sh"


def _git_repo(path: Path) -> Path:
    """A real git tree — the guard's identity is `git rev-parse --show-toplevel`,
    so a plain directory cannot carry one."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


def _lock_holder(state_dir: Path, role: str) -> str:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.endswith(f".server-{role}.lock") and str(state_dir) in target:
                    return pid
        except OSError:
            continue
    return ""


class ServeFixture:
    """The ground the serve.sh guards are measured on.

    A real tree, a real state, a real serve.sh: the claims under test are about
    WHERE code runs and WHICH role a process may claim, and a mocked path proves
    nothing about either. A mixin, not a TestCase, so the classes below do not
    inherit each other's test cases.
    """

    def setUp(self) -> None:
        # NOT under TMPDIR: serve.sh refuses a code root in /tmp outright (round
        # 108's scar — a config left pointing at /tmp/wire_test served a role
        # from stale code). The pre-round-120 suite built its whole fixture in
        # tempfile's default space, so every legitimate tree looked like scratch;
        # the trees now live beside the repo, and only the scratch test uses
        # /tmp on purpose. Verified the repo's own tree is not under /tmp.
        repo = ROOT.resolve()
        if str(repo).startswith("/tmp") or str(repo).startswith("/var/tmp"):
            self.skipTest(f"the repo itself lives in scratch space ({repo}); "
                          "the guard would refuse to run here")
        base = repo.parent / ".pair-guard-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        # the state must sit in a git repo: the guard's identity is
        # `git rev-parse --show-toplevel`, so a plain /tmp dir cannot carry one
        self.state = _git_repo(Path(self._tmp.name) / "proj") / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.role = "guardrole"

    def tearDown(self) -> None:
        holder = _lock_holder(self.state, self.role)
        if holder:
            subprocess.run(["kill", holder], capture_output=True, timeout=10)
            time.sleep(0.3)
        self._tmp.cleanup()
        # Leave the tree clean (house rule): the hidden base beside the repo
        # survives when the last TemporaryDirectory goes. rmtree ignores a
        # keeper another concurrent run may have created.
        base = ROOT.resolve().parent / ".pair-guard-tests"
        try:
            if base.is_dir():
                shutil.rmtree(base, ignore_errors=True)
        except OSError:
            pass

    def _raise(self, cwd: Path, serve: Path | None = None,
               pair_role: str | None = None) -> subprocess.Popen:
        """Raise the server the way the MCP entry does: a chosen CWD and a chosen
        serve.sh. Both matter — the guard compares the repo the CODE lives in
        against the repo the STATE lives in, so a legitimate twin must run its
        OWN serve.sh, not the test repo's."""
        # A cloned tree carries the WHOLE package, not just serve.sh: serve.sh
        # execs server.py relative to itself, and a partial copy would fail with
        # "can't open server.py" before the guard ever ran (measured).
        pkg = Path(cwd) / "mcp" / "pair_mailbox"
        if pkg.is_dir():
            shutil.rmtree(pkg)
        shutil.copytree(ROOT / "mcp" / "pair_mailbox", pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        # The role comes from argv, never from env (round 103 S5's guard): the
        # running pair exports PAIR_ROLE=omp, and a test raising another role
        # would be refused by that guard before it ever reached the origin check.
        env.pop("PAIR_ROLE", None)
        if pair_role is not None:
            env["PAIR_ROLE"] = pair_role
        proc = subprocess.Popen(
            ["sh", str(Path(cwd) / "mcp" / "pair_mailbox" / "serve.sh"), self.role,
             "--state", str(self.state)],
            cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        return proc

    def _sanction(self, engine_root: str | None) -> None:
        """Write (or clear) .pair/engine: the record by which a target project
        names the pair-mailbox tree its engine code legitimately comes from.
        Cleared by engine_root=None, which is the pre-round-120 world."""
        path = Path(self.state) / "engine"
        if engine_root is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(engine_root, encoding="utf-8")


class OriginGuardCase(ServeFixture, unittest.TestCase):

    def test_foreign_tree_refuses_and_names_both_roots(self) -> None:
        """A server raised from a FOREIGN REPO must refuse, not serve. The
        refusal names both repos, because a bare 'refused' leaves the operator
        guessing which of their clones is wrong."""
        foreign = _git_repo(Path(self._tmp.name) / "scratch-clone")
        proc = self._raise(foreign)   # the full package copied INTO the foreign tree
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("a server from a foreign tree must refuse, not run")
        self.assertNotEqual(proc.returncode, 0)
        combined = (err or "") + (out or "")
        self.assertIn("foreign", combined.lower())
        self.assertIn("scratch-clone", combined,
                      "the refusal must name the repo the code came from")

    def _converse(self, cwd: Path) -> tuple[int, str, bool]:
        """Raise the server AND speak one frame to it: an MCP `initialize`.
        Returns (returncode, combined_output, answered).

        A stdio server exits when stdin closes, so a liveness claim needs a
        client. One frame is the honest probe: `served` means the protocol
        answered; `refused` means the process died before answering, and the
        refusal text is its stderr."""
        proc = self._raise(cwd)
        served = False
        try:
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "guard-test", "version": "1"}}}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            served = bool(line.strip())
        except (OSError, ValueError):
            served = False
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return proc.returncode, (err or "") + (out or ""), served

    def test_correct_tree_serves(self) -> None:
        """The guard must not block the legitimate case: the code and the state
        belong to the SAME repo, wherever it is reachable from. Proven by the
        protocol answering, not by a pid surviving."""
        twin = _git_repo(Path(self._tmp.name) / "twin")
        twin_state = twin / ".pair"
        twin_state.mkdir(parents=True, exist_ok=True)
        twin_state.joinpath("state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        old_state = self.state
        self.state = twin_state
        try:
            rc, out, served = self._converse(twin)
            self.assertTrue(served, f"the correct tree must answer the protocol: {out[:400]}")
            self.assertEqual(rc, 0, out[:200])
        finally:
            self.state = old_state

    def test_same_repo_two_paths_is_one_identity(self) -> None:
        """The guard's identity must survive bind mounts and symlinks: the live
        pair runs the server through /home/alexios/work/umputum while state lives
        at /mnt/storage/work/umputum — one repo, two paths, and a path comparison
        would refuse the live pair. Simulated with a symlink into the same repo."""
        twin = _git_repo(Path(self._tmp.name) / "twin2")
        (twin / ".pair").mkdir(parents=True, exist_ok=True)
        (twin / ".pair" / "state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "roles": {}, "phase": "implement",
             "seq": 0}), encoding="utf-8")
        alias = Path(self._tmp.name) / "alias-to-twin2"
        alias.symlink_to(twin, target_is_directory=True)
        old_state = self.state
        self.state = twin / ".pair"
        try:
            rc, out, served = self._converse(alias)   # reached through the alias
            self.assertTrue(served, f"one repo reached two ways must serve: {out[:400]}")
        finally:
            self.state = old_state


    def test_non_git_state_dir_fails_open_and_serves(self) -> None:
        """The regression the peer caught (seq 1625): under `set -e` an unguarded
        command substitution in an assignment EXITS the shell, and `2>/dev/null`
        hides the message but not the status — so a server raised with a state
        dir outside any git repo died rc=128 BEFORE the handshake, silently.
        The installer's verify path uses exactly that shape (temp state), which
        is how it surfaced. Fail-open must mean 'no identity -> serve'."""
        outside = Path(self._tmp.name) / "plain-dir"
        outside.mkdir(parents=True, exist_ok=True)
        plain_state = outside / ".pair"
        plain_state.mkdir(parents=True, exist_ok=True)
        plain_state.joinpath("state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "roles": {}, "phase": "implement",
             "seq": 0}), encoding="utf-8")
        old_state = self.state
        self.state = plain_state
        try:
            rc, out, served = self._converse(outside)
            self.assertEqual(rc, 0, f"a non-git state must fail open, not die: {out[:300]}")
            self.assertTrue(served, f"and it must answer the protocol: {out[:300]}")
        finally:
            self.state = old_state

    def test_no_tree_state_refuses_without_creating_a_stray(self) -> None:
        """serve.sh already refuses a stray state; the guard must not turn that
        into a serve-from-anywhere hole. A cwd outside any project with the state
        pinned explicitly still refuses (the tree check passes on cwd)."""
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir(parents=True, exist_ok=True)
        proc = self._raise(outside)
        try:
            proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        # whichever way it refuses, no role lock may be held by a foreign cwd
        self.assertEqual(_lock_holder(self.state, self.role), "",
                         "a foreign cwd must never hold the role")

    def test_sanctioned_engine_serves_a_foreign_state(self) -> None:
        """The multi-project case, which round 108's guard broke: a target
        project with NO package of its own runs the engine from the autonomy
        repo, and pairup sanctions that root in .pair/engine. Proven by the
        protocol answering, not by a pid surviving."""
        engine = _git_repo(Path(self._tmp.name) / "engine-tree")
        self._sanction(str(engine))
        try:
            rc, out, served = self._converse(engine)
            self.assertTrue(
                served,
                "a sanctioned engine must answer the protocol for a foreign "
                f"state repo: {out[:400]}")
            self.assertEqual(rc, 0, out[:200])
        finally:
            self._sanction(None)

    def test_sanction_identity_is_the_toplevel_not_the_path(self) -> None:
        """The same engine, reached through an alias, must serve: one repo, two
        paths (bind mount, symlink) — the live pair runs the code under
        /home/alexios/work/umputum while state sits at /mnt/storage/work/umputum.
        The sanction names the git toplevel, and a path compare would refuse."""
        engine = _git_repo(Path(self._tmp.name) / "engine-real")
        self._sanction(str(engine))
        alias = Path(self._tmp.name) / "engine-alias"
        alias.symlink_to(engine, target_is_directory=True)
        try:
            rc, out, served = self._converse(alias)
            self.assertTrue(served, f"an aliased sanctioned engine must serve: {out[:400]}")
        finally:
            self._sanction(None)

    def test_scratch_code_root_refuses_even_with_a_sanction(self) -> None:
        """Round 108's scar: a config left pointing at a scratch clone served a
        role from /tmp/wire_test. A sanction must never open that hole, so code
        under /tmp refuses no matter what the state says, and names the reason."""
        scratch = Path("/tmp/pair-guard-scratch-" + os.urandom(4).hex())
        try:
            engine = _git_repo(scratch)
            self._sanction(str(engine))
            try:
                proc = self._raise(engine)
                try:
                    out, err = proc.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    self.fail("a scratch-clone server must refuse, not run")
                combined = (err or "") + (out or "")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("scratch", combined.lower(),
                              "a scratch code root must be named as the reason")
                self.assertEqual(_lock_holder(self.state, self.role), "",
                                 "a scratch clone must never hold the role")
            finally:
                self._sanction(None)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_mismatched_sanction_refuses(self) -> None:
        """A state that sanctions one engine while the entry points at another
        must refuse: that is a stale sanction or a wrong entry, and either way
        the role must not be served from an unsanctioned tree."""
        engine = _git_repo(Path(self._tmp.name) / "engine-right")
        other = _git_repo(Path(self._tmp.name) / "engine-wrong")
        self._sanction(str(engine))          # sanctioned: engine
        try:
            proc = self._raise(other)        # entry actually points here
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail("an unsanctioned engine must refuse, not run")
            combined = (err or "") + (out or "")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("sanction", combined.lower(),
                          "the refusal must name the sanction as the failing check")
            self.assertIn(str(engine), combined,
                          "the refusal must name the engine this state sanctions")
            self.assertEqual(_lock_holder(self.state, self.role), "",
                             "an unsanctioned engine must never hold the role")
        finally:
            self._sanction(None)

    def test_project_with_own_package_refuses_a_foreign_engine(self) -> None:
        """Round 108's live case, now narrowed: a project that HAS its own
        mcp/pair_mailbox/ must run it, so a foreign code root there is exactly
        the scratch-clone shape that guard was written for."""
        project = Path(self.state).parent
        pkg = project / "mcp" / "pair_mailbox"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "serve.sh").write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        engine = _git_repo(Path(self._tmp.name) / "engine-not-needed")
        self._sanction(str(engine))
        try:
            proc = self._raise(engine)
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail("a project with its own package must refuse a foreign engine")
            combined = (err or "") + (out or "")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(str(project.resolve()), combined,
                          "the refusal must name this project's own engine path")
            self.assertEqual(_lock_holder(self.state, self.role), "",
                             "the foreign engine must never hold the role here")
        finally:
            self._sanction(None)
            shutil.rmtree(project / "mcp", ignore_errors=True)

    def test_no_sanction_still_refuses(self) -> None:
        """Round 108's contract, unchanged: a foreign code root with NO sanction
        on record refuses. Removing the guard's presumption must not delete the
        refusal — the pre-round-120 world (a project that never ran pairup)
        installs nothing by itself."""
        engine = _git_repo(Path(self._tmp.name) / "engine-unsanctioned")
        self._sanction(None)
        proc = self._raise(engine)
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("an unsanctioned foreign engine must refuse, not run")
        self.assertNotEqual(proc.returncode, 0)
        combined = (err or "") + (out or "")
        self.assertIn("engine", combined.lower())
        self.assertIn("foreign", combined.lower())

    def _raise_with_env(self, cwd: Path, env_state: str, env_pin: str = "PAIR_STATE") \
            -> subprocess.Popen:
        """Raise the server the way a PANE does: no argv --state, only the
        inherited environment. That is the shape the nyx defect ran in (round
        121): both panes of a hand-made session served a foreign room because
        nothing checked what the shell remembered."""
        pkg = Path(cwd) / "mcp" / "pair_mailbox"
        if pkg.is_dir():
            shutil.rmtree(pkg)
        shutil.copytree(ROOT / "mcp" / "pair_mailbox", pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        env.pop("PAIR_ROLE", None)
        env.pop("PAIR_STATE", None)
        env.pop("PAIR_TRUST_ENV", None)
        env[env_pin] = env_state
        return subprocess.Popen(
            ["sh", str(pkg / "serve.sh"), self.role],
            cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)

    def test_inherited_pairstate_of_a_foreign_project_is_ignored(self) -> None:
        """Round 121's live defect, end to end.

        Measured: the operator opened `tmux new -s nyx` inside another project's
        session, ran pairup, and BOTH nyx panes served the foreign galatour room
        (`ps`: --state .../galatour/.pair), while .../nyx-system/.pair was served
        by no live server at all. The old serve.sh took PAIR_STATE from the
        environment on its face, so a stale shell repointed the whole launch —
        and the NOTE it printed named the mismatch without ever refusing it.

        Contract now: an env PAIR_STATE naming another project's room is ignored,
        the server serves the room of the tree it was launched in, and it says so
        on stderr.
        """
        other = _git_repo(Path(self._tmp.name) / "other-project")
        self._sanction(str(ROOT.resolve()))
        proc = self._raise_with_env(Path(self._tmp.name) / "proj",
                                    str(other / ".pair"))
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("the server must serve this project's own room, not hang")
        combined = (err or "") + (out or "")
        self.assertIn("ignoring PAIR_STATE", combined,
                      "a foreign env room must be named and dropped, not silently used")
        drop_line = [ln for ln in combined.splitlines() if "launched from" in ln]
        self.assertTrue(drop_line,
                        "the drop must say which project the server is serving instead")
        self.assertIn(str(Path(self._tmp.name) / "proj"), drop_line[0])
        self.assertIn("own", combined.lower(),
                      "the answer must promise this project's own room")

    def test_argv_state_is_still_explicit_and_trusted(self) -> None:
        """The escape hatch and the MCP config path stay: argv `--state` names
        exactly which room to serve, and it is honoured even from a foreign cwd.
        The guard drops the ENVIRONMENT, never the operator's explicit word."""
        other = _git_repo(Path(self._tmp.name) / "explicit-other")
        (other / ".pair").mkdir(parents=True, exist_ok=True)
        (other / ".pair" / "state.json").write_text(json.dumps(
            {"round": 904, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        (other / ".pair" / "engine").write_text(str(ROOT.resolve()), encoding="utf-8")
        pkg = Path(self._tmp.name) / "proj" / "mcp" / "pair_mailbox"
        shutil.copytree(ROOT / "mcp" / "pair_mailbox", pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        env.pop("PAIR_ROLE", None)
        env.pop("PAIR_STATE", None)
        proc = subprocess.Popen(
            ["sh", str(pkg / "serve.sh"), self.role, "--state", str(other / ".pair")],
            cwd=str(Path(self._tmp.name) / "proj"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("an explicit --state must serve, not hang")
        combined = (err or "") + (out or "")
        self.assertNotIn("ignoring PAIR_STATE", combined,
                         "argv --state is the operator's word; it is never overridden")

    def test_paired_pane_init_never_inherits_a_foreign_room(self) -> None:
        """pairup's pane init strings, EXECUTED rather than grepped.

        The first cut of this test only grepped for "env -u PAIR_STATE" in the
        source, and it stayed green on a broken fix: the critic measured in
        review that `env -u A -u B VAR=; cd ...` is a program invocation, not a
        shell directive — the `;` ends the command, no executable follows the
        assignment, so `env` dumps the whole environment (79 lines) and the
        shell keeps its old values (probe printed state=old role=foo). A source
        grep cannot tell those apart; running the init string can.

        So: take the real init string out of pairup, run it under a hostile
        environment, and demand the pane's environment ends up right.
        """
        source = (ROOT / "bin" / "pairup").read_text(encoding="utf-8")
        for marker in ("writer_init=", "critic_init="):
            line = [ln for ln in source.splitlines() if ln.startswith(marker)]
            self.assertTrue(line, f"{marker} is gone — the pane would start with no room")
            init = line[0].split('="', 1)[1].rsplit('"', 1)[0]
            # pairup writes these INIT strings as source lines: $STATE_DIR and
            # $PROJECT_DIR are pairup's own variables, expanded at LAUNCH time.
            # The probe must substitute them itself — a literal `$STATE_DIR` in
            # the string would just expand to empty and the assert would pass
            # for the wrong reason (measured in this round's first cut).
            head = init.split("; echo '=== ", 1)[0]
            for name, value in (("STATE_DIR", str(self.state)),
                                ("PROJECT_DIR", str(Path(self._tmp.name)))):
                head = head.replace(f"${name}", value)
            probe = head + "; printf '%s|%s|%s|%s' \"${PAIR_STATE:-}\" \"${PAIR_ROLE:-}\" \"${PAIR_TRUST_ENV:-}\" \"$(pwd -P)\""
            env = dict(__import__("os").environ)
            env["PAIR_STATE"] = "/foreign/project/.pair"
            env["PAIR_ROLE"] = "stale-role"
            env["PAIR_TRUST_ENV"] = "1"
            proc = subprocess.run(["sh", "-c", probe], capture_output=True,
                                  text=True, env=env, timeout=60,
                                  cwd=str(Path(self._tmp.name)))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            got_state, got_role, got_trust, got_cwd = proc.stdout.split("|", 3)
            self.assertEqual(got_state, str(self.state),
                             f"{marker}: the pane must serve THIS project's room")
            self.assertEqual(got_role, "",
                             f"{marker}: a stale role must be unset, not inherited")
            self.assertEqual(got_trust, "",
                             f"{marker}: PAIR_TRUST_ENV must not leak into the pane")
            self.assertEqual(got_cwd, str(Path(self._tmp.name)),
                             f"{marker}: the shell must change into its cwd")
            self.assertLess(len(proc.stdout), 200,
                            f"{marker}: the init must not dump the environment")


class RoleDerivationCase(ServeFixture, unittest.TestCase):
    """The role may be DERIVED from the configured one, never replaced by it.

    Duo mode needs two instances of ONE CLI in a project, and the MCP config is
    per CLI: both instances read the same file, so both would claim the same
    role and the second would be refused by the "one live role, one live
    process" rule (server.py). The instance suffix says WHICH instance.

    The direction that must stay closed is the one the 2026-10-07 measurement
    opened: a process that picked up a role it was not given (PAIR_ROLE=opencode
    served by an omp server). So a FOREIGN role is still a refusal, and the
    derived name may only extend the configured one - and only with characters
    that cannot climb out of .pair/, because the role names a mailbox directory
    and a wake file.
    """

    def _role_of(self, pair_role: str) -> tuple[int, str]:
        """Raise the server with this PAIR_ROLE and return (rc, output)."""
        proc = self._raise(Path(self._tmp.name) / "twin", pair_role=pair_role)
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return proc.returncode, (err or "") + (out or "")

    def test_a_foreign_role_is_refused(self) -> None:
        rc, out = self._role_of("opencode")
        self.assertNotEqual(rc, 0, "a role the config never gave must be refused")
        self.assertIn("refus", out.lower())
        self.assertIn("opencode", out, "the refusal must name the role it refused")
        self.assertIn(self.role, out, "and the role that WAS configured")

    def test_a_traversal_shaped_role_is_refused(self) -> None:
        """The role names a mailbox directory and a wake file."""
        rc, out = self._role_of(f"{self.role}-../..")
        self.assertNotEqual(rc, 0, "a role that climbs out of .pair/ must be refused")
        self.assertIn("refus", out.lower())

    def test_an_instance_of_the_configured_role_is_served(self) -> None:
        """The legitimate case: "agy" -> "agy-writer" answers the protocol."""
        twin = _git_repo(Path(self._tmp.name) / "twin")
        twin_state = twin / ".pair"
        twin_state.mkdir(parents=True, exist_ok=True)
        twin_state.joinpath("state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        old_state = self.state
        self.state = twin_state
        try:
            proc = self._raise(twin, pair_role=f"{self.role}-writer")
            served = False
            try:
                proc.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "role-test", "version": "1"}}}) + "\n")
                proc.stdin.flush()
                served = bool(proc.stdout.readline().strip())
            except (OSError, ValueError):
                served = False
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            self.assertTrue(served, "an instance role must be served, not refused")
        finally:
            self.state = old_state


if __name__ == "__main__":
    unittest.main(verbosity=2)
