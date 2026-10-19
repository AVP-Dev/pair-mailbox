---
title: Two instances of ONE CLI in one project — the launcher exists, the role never arrives
found: 2026-10-19
severity: medium
round: 124
status: fixed
fixed: 2026-10-19
fixed_by: round 124
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

## DECIDED (2026-10-20, round 124): the prefix guard, not a clone

The human's call: "если это так быстро делается, то можно просто сделать и все".
Re-reading the measurement changed the answer: **the reversal of 242e9a7 never
closed the omp case** — the omp defect was the DISCOVERY loading a foreign
project config (fixed by the per-CLI homes and by the project config carrying no
mcp block), while the env override was removed as an UNRESTRICTED channel and as
the source of 30 live refusals (round 103 S5). So a channel that may only
DERIVE the configured role, and may never replace it, keeps the direction the
reversal was defending and gives the duo what it needs.

**Decided: option 1 with a prefix guard.** `PAIR_ROLE` is accepted only when it
EXTENDS the configured role ("agy" -> "agy-writer", "agy_2"), and only with
characters that cannot climb out of `.pair/` (the role names a mailbox directory
and a wake file). A foreign role is still refused, exactly as before.

This also retires options 2 and 4: no clone, no second config, no per-CLI home
matrix — the instance is named by the launcher, and the config stays the one the
human installed.

### What was implemented (round 124)

- `mcp/pair_mailbox/serve.sh` — the guard: a foreign role is refused (exit 3, the
  message names both roles), a traversal-shaped suffix is refused, an instance of
  the configured role is served UNDER THAT NAME.
- `bin/pair-duo` — each pane exports its own `PAIR_ROLE` ("<cli>-writer",
  "<cli>-critic"); the swap path (a DIFFERENT CLI as critic) unsets the export,
  because the swapped pane inherits the writer's and the swapped CLI's own role
  does not extend it.
- `bin/pair-duo_test.py` — the measurement: a stub CLI reports the role it was
  launched with, and the two panes must report two DIFFERENT roles that extend
  the configured one. Red before the change (no role at all), green after.
- `mcp/pair_mailbox/test_origin_guard.py` — the guard's three cases (foreign
  role refused, traversal refused, instance served).

### What this does NOT cover

- The socket transport (`PAIR_TRANSPORT=socket`): the thin client takes its role
  from argv, and the socket path is not role-scoped in the same way. The duo
  works on the default stdio path; the socket mode is a separate item.
- `pairup --duo` is still refused: the same-CLI launcher is `bin/pair-duo`,
  and that flag stays gone. The refusal message now points there instead of
  claiming the mode is impossible (round 125).

## Acceptance (what "done" means for this card)

- `pair-duo agy` gives the two panes TWO DISTINCT ROLES, both live, both with
  working pair tools — measured, not by reading the code.
- A foreign `PAIR_ROLE` is still refused, and a role that is not an instance of
  the configured one is refused by name.
- A restart of either instance does NOT change its role.
- The trio still works: two different CLIs in one project, unchanged.

## Where it is not going

- No change to the role-collision rule: one live role means one live process
  (`server.py:960`). The duo gives the second instance its own role; it does not
  teach the server to tolerate a double.
- No env-set role, in any form, in any profile: the environment may only DERIVE
  an instance of the configured role, and a future design that wants more must
  argue with `242e9a7` and the omp/opencode measurement by name.
