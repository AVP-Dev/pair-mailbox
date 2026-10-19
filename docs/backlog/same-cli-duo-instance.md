---
title: Two instances of ONE CLI in one project — the launcher exists, the role never arrives
found: 2026-10-19
severity: medium
round: 124
status: open
---

## The ask (from the human, relayed by the critic)

Support launching two instances of the SAME CLI in one project — `pairup agy agy`,
`pairup claude claude` — so a pair does not need a zoo of different tools. The
role is currently hard-wired to the CLI's program name, which is what blocks it.

## What is already there (measured, not assumed)

**The launcher exists.** `bin/pair-duo` launches two instances of one CLI in tmux,
with worktree isolation for the critic, and names two roles:

```
WRITER_ROLE="${CLI}-writer"     # e.g. agy-writer
CRITIC_ROLE="${CLI}-critic"     # e.g. agy-critic
```

**The roles never reach the servers.** The role is baked into the MCP config by
`install.sh` (`write_mcp`: `serve.sh <role>`), and the config path is per CLI home
(`~/.gemini/config/mcp_config.json`, `~/.codex/config.toml`, `~/.claude.json`,
`~/.pi/agent/mcp.json`, `~/.omp/agent/mcp.json`, `~/.config/opencode/opencode.jsonc`).
Two instances of one CLI read the SAME file, so both servers start with the SAME
role.

**The second one is then refused, loudly.** `server.py:960` — "One live role means
one live process": a second process under a held role, with a live pid and a
different live pane, raises `ProtocolError` ("the second process never registers
on the same role"). So the second pane gets no pair tools at all. The duo
launcher's two role names are decoration today.

## Why the env cannot be the channel (measured, and deliberately reversed)

`bin/pair-duo` does not export a role, and must not. The env override WAS
implemented — commit `70800ae` ("Support PAIR_ROLE and PAIR_STATE overrides in
serve.sh") — and was removed in `242e9a7` after a live defect:

> The `omp` binary is the same engine as pi-coding-agent but with a different home
> (`~/.omp/` vs `~/.pi/`). The engine loads OTHER CLIs' MCP servers by discovery
> (OpenCode/Claude/Codex/Cursor/Gemini/VSCode/Windsurf) and the server key is one:
> `pair`. The omp process, started from the repo root, raised `server.py --role
> opencode` from the project's `.opencode/opencode.jsonc`, and `pair_state` answered
> `you_are_writer=false`: the agent refused to write.

Today `serve.sh` refuses a disagreeing env instead of honouring it:

```sh
if [ -n "${PAIR_ROLE:-}" ] && [ "${PAIR_ROLE}" != "$role" ]; then
  echo "pair-mailbox: refusing: PAIR_ROLE=${PAIR_ROLE} != argv role $role (role comes from config, never from env)" >&2
  exit 3
fi
```

and the header states the rule: *"The role is baked into configuration rather than
read from the environment, because an agent must not be able to claim a role it was
not given: the whole write-authority boundary depends on the server knowing which
process is which, and a value the process can set for itself is not that."*

**So "PAIR_ROLE with priority over --role" reinstates the mechanism that a measured
defect forced to be removed.** It is not a fresh option; it is a revert.

## Does the env reach the server? (the critic's question — measured)

**Yes, for all six profiles.** Every profile's MCP entry is a local/stdio command
(`opencode`: `"type": "local"`; the rest: `command` + `args`), and **no profile sets
an `env` block in its entry** — so nothing overrides the inherited environment: the
server, being the CLI's child, inherits the CLI's environment. An env var CAN reach
it. The question was never inheritance; it is AUTHORITY: the role env is refused on
purpose, because a role is identity, and identity must not be self-assignable.

## The options, with what each costs

1. **PAIR_ROLE overrides argv.** Reverts `242e9a7`; the measured omp/opencode
   defect returns (a process picks up a role it was not given). Cheapest, and the
   only one that makes a stale shell export dangerous again.
2. **Profile clones in `cli-profiles.json` (`agy`, `agy2`).** The role stays in the
   config — the channel the architecture already trusts — and the clone IS the
   "instance" concept. What it still needs: a per-INSTANCE config location, because
   the config path is derived from the CLI home and two instances of one CLI share
   one home. Note what already keeps two CLIs apart today: the per-CLI HOME
   (`~/.omp/` vs `~/.pi/`, the 2026-10-07 case). A clone needs the same separation.
3. **Decouple the role from the CLI name (roles become `writer`/`critic`).**
   Orthogonal, not a solution: two instances of one CLI still need two distinct
   identities, and only one of them may be the writer. The naming convention is
   already broken by duo mode (`agy-writer`), so the change is smaller than it
   looks — but it touches the state's vocabulary, the transcript, the mailbox paths
   and the shipped skills.
4. **Per-instance config written by the launcher (not in the critic's list).**
   The launcher writes each instance's MCP entry — with THAT instance's role —
   into a per-instance config, and points the CLI at it. Keeps the role in config
   (authority intact) and gives each instance its own entry. The hazard is the
   shared file: if the launcher rewrites the ONE config before the second launch,
   the first instance's role flips on its next CLI restart. A per-instance file
   (or a per-instance home) avoids it.

## Recommendation (the writer's, for the critic to argue with)

**Option 2 + option 4, and not option 1.** The role must stay where the
architecture put it: in the config the human installed. What the duo needs is not a
new role channel but a per-INSTANCE config location — the same separation that
keeps `~/.omp/` and `~/.pi/` apart. Concretely:

- `cli-profiles.json` gains an optional per-profile `instance` (a clone with its
  own home), and the config path is derived from the clone's home;
- `install.sh` writes the clone's config with the clone's role;
- `bin/pair-duo` launches each instance with the env that points the CLI at ITS
  home (the per-CLI fact to measure — see below).

The open question that decides how much work this is: **which env var moves a
given CLI's config home?** The repo records none of them today — a search of
`docs/`, `install.sh`, `cli-profiles.json` and the server for `XDG_CONFIG_HOME`,
`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `GEMINI_*`, `PI_HOME`, `OMP_HOME` finds
nothing — so the matrix has to be MEASURED per profile, not guessed. A CLI that
honours no home env cannot be cloned by moving its home, and then the
per-instance FILE (option 4) is the only door left for it.

## Acceptance (what "done" means for this card)

- `pair-duo agy` (and `pairup agy agy`) gives the two panes TWO DISTINCT ROLES, both
  live, both with working pair tools — measured, not by reading the code.
- The role of each instance comes from a config the launcher wrote. No env var
  overrides the role; `serve.sh`'s refusal stays.
- A restart of either instance does NOT change its role (the per-instance config
  is not shared).
- The per-CLI home/env matrix is measured and recorded (six profiles).
- The trio still works: two different CLIs in one project, unchanged.

## Where it is not going

- No change to the role-collision rule: one live role means one live process
  (`server.py:960`). The duo must give the second instance its own role, not teach
  the server to tolerate a double.
- No env-set role, in any form, in any profile. If a future design needs it, it
  must argue with `242e9a7` and the omp/opencode measurement by name.
