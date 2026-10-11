# How it works

The short version, without detail. For "why is it like this" see
[`PROTOCOL.md`](./PROTOCOL.md); this file is "what to do".

> Language: this is the English original. The Russian mirror is
> [`HOW-IT-WORKS.ru.md`](./HOW-IT-WORKS.ru.md).

## What this is

Two agents on one project, running different programs, with a single server between
them. The server does not just pass messages along: it also watches the rules and
refuses to let them break.

Three rules matter, and they are real rather than aspirational:

1. **Exactly one agent edits code.** Not "agreed between ourselves" — the second one
   gets a refusal from the server.
2. **Edits arrive sealed.** The second cannot edit files; it proposes a patch as a
   file, and the writer checks the hash and applies it.
3. **Every refusal is logged.** Including an attempt to work around a rule.

## Why two agents when one would do

One agent accepts its own decisions as correct. It has no reason to look in its code
for what it did not think to check. The second agent is a different context and often
a different model. It reads the same thing and thinks independently.

Hence the rule: **two agents politely agreeing is worth nothing.** What is worth
something is a specific bug found or a fact verified. Silent agreement is not a
result.

## Three words

| word | meaning |
|---|---|
| **writer** | the one who edits code. Exactly one. |
| **critic** | the one who does not edit. Reads, breaks, proposes. |
| **human** | the one who gives the task and decides. Agents do not decide for them. |

A role is the name of a program, not a choice an agent makes. You have two programs,
so one writes and one breaks. Which is which is decided by the standing arrangement
(`bin/pairctl roles`) or, failing that, by a claim — and the agent must give a reason
("I already found where to fix it"), not "because I was first".

```sh
bin/pairctl           # the human's authority surface: task, mode, autonomy, ok, status
pairup                 # fast start from the project folder: supervisor-if-dead + pair
bin/pair-duo --swap-critic <cli>  # replace the critic pane, writer untouched
bin/pairpilot         # the supervisor: restarts a dead agent, confirms allowlisted
                      # prompts, nudges a role with unread mail, writes the report
bin/pairchat          # the conversation of this pair, live
bin/pairboard         # a self-contained HTML snapshot of the state, no server
bin/pairctl all       # every project at once: who writes, who is silent
bin/pairwatch         # catches a stuck agent
bin/pairdoctor        # checks that everything is in place
```

The standing arrangement pins only the writer (`bin/pairctl roles --writer X`);
the critic is any second live role, so a CLI whose limits ran out is swapped
with `pairup --swap <cli>` and no paperwork. `finish.sh` is the heavy first-time
path (install + full tests); `pairup` is the daily one (seconds).

Launch commands live in `cli-profiles.json`: six profiles (`opencode`, `agy`,
`codex`, `claude`, `pi`, `omp`). The
server exposes 22 `pair_*` MCP tools. The pair's state lives in `.pair`:
`state.json`, `transcript.jsonl`, and the `mailbox/`, `notes/`, `patches/`,
`obligations/`, `notifications/` stores.

`bin/pairpilot` cannot move phases, grant or revoke write authority, commit, or touch
project files. It watches, and it reports.

`.pair/tasks.md` is the session journal, generated from state so it cannot drift from
the facts: what was asked, what was done, what was proven, what is left.

## Two axes of work

`mode` says what the round is for. `autonomy` says how far it goes without you. They
are separate, and merging them would be wrong.

```sh
bin/pairctl mode fix          # default: code changes
bin/pairctl mode audit        # code does not change, only findings

bin/pairctl autonomy auto     # default: works alone, closes the round given proof
bin/pairctl autonomy semi     # works alone, but closes only on your word
bin/pairctl autonomy observe  # only reports; the human is at the wheel
```

You are asked **at most once**, and by default not at all: the level is chosen when the
pair starts, not renegotiated mid-round.

```sh
pairup --task "autonomy: semi do the thing"   # or pairctl autonomy semi
```

With no flag the launcher resolves in this order, strongest first: the flag, then
`autonomy: semi` on the first line of the task, then `.pair/autonomy`, then `auto`. The
source is written to the transcript as `via=flag|task|file|default`, so a level nobody
can account for never becomes a mystery later.

Agents read the mode themselves on the first `pair_state`. They do **not** know you
changed it, so after the command **say one line in their panes**.

Note that `observe` is not "gates off": proof is required to close a round in every
mode. `observe` only means it will not restart agents and does not need your word.

### fix — we repair

```sh
bin/pairctl task "fix the retries on 502"
```

They work as two: **one writes code, the other breaks it.** Not both at once. The
second cannot edit files — it has the patch mechanism for that, which proposes rather
than applies.

The task list is append-only. A second task does not overwrite the first:

```sh
bin/pairctl tasks                        # what is open, what is closed
bin/pairctl task --done 2                # finished
bin/pairctl task --drop 3 --reason "covered by PR #5"
```

A round cannot close while a task is open, and a dropped task needs a reason. That is
deliberate: an agent that switches to a new task and forgets the old one is the normal
failure mode, and here it cannot pass silently.

Who writes:

```sh
bin/pairctl why
```

To the writer's pane:

```
mode fix, you are writing. Start with the task I recorded.
```

To the other's pane:

```
mode fix, you are the critic. You do not edit — propose patches.
```

### audit — we look

```sh
bin/pairctl task "find the races in the store"
bin/pairctl mode audit
```

They work as two: **both hunt for defects, both try to break the other's finding.**
Nobody changes code. The server refuses patches — in a round where nothing changes,
there is nothing to propose.

A finding counts as found only once both have confirmed it. Could not break it —
`confirmed`; could — `refuted`, with the reason.

Confirmed findings go into `docs/backlog/`. Those are the only files such a round
creates, and they are the ready-made input for the next repair round.

### If you switched mode, tell the peers

```sh
bin/pairctl mode fix
```

The state changed, but the agents will not find out until you say so. One line in each
pane. What exactly to say depends on who is writing:

```sh
bin/pairctl why
```

## Typical sessions

**Just repair.** `pairctl task "..."` → one line in both panes → they work → watch
`pairchat`.

**Look first, then repair.** `pairctl mode audit` → work → findings in
`docs/backlog/` → `pairctl mode fix` → a line in the panes "we are taking from
docs/backlog/" → work.

**The repair grew, we need a step back.** `pairctl mode audit`. What is already done
stays done.

**A new round.** The task appears in both panes (the banner, round 53) and the
critic gets a push if the writer is silent. Moving from `scoping → plan` requires
`.pair/intent.md` (Problem, Scope, Outcome). The writer writes `.pair/plan.md` —
`round: N`, Goal, Invariants, Acceptance — and sends it to the neighbour for the
visа. `plan → implement` is refused by the server until the visа (or the waiting
window expires); stepping back and reopening are never blocked (docs/PROTOCOL.md §6).
During execution, `budget ceiling` (§10d) protects against infinite round loops by
measuring machine metrics (messages, phase advances, wall-clock, restarts); moving
forward is refused on breach, while `done` and backward steps always remain open.
Upon closing into `done`, a machine `Task Receipt` (`.pair/RECEIPT-<round>.md`) is
automatically emitted with tasks, evidences, and final budget metrics.
If regressions or failing evidence (`ok=false`) are reported in `done`, `pair-heal` (§10f)
automatically reopens the round into `implement` (capped at 3 attempts).
Reset limits: `bin/pairctl budget --reset`.

## What you see

```sh
bin/pairchat                 # the live conversation
bin/pairchat --once          # once, then exit
bin/pairctl all              # every project, who is silent, what the task is
bin/pairctl log --limit 50   # what they did, including every refusal
bin/pairctl status           # this project, and what blocks closing the round
bin/pairctl roster           # who is registered
```

The human steps in for six things only: secrets and tokens, deploy targets and ports,
deploys, database migrations, deleting data, deleting git history. Everything else the
agents settle between themselves.

Agents are forbidden from asking you to relay messages between windows: they wake
each other through `pair_deliver` directly.

## What will not work

**An agent asks you to pass text into the neighbouring window ("pigeon post").**
That is an agent behaviour bug: `pair_deliver` exists for active messaging and
handing over. The human gave a task and expects a result (a PR), not to work as a
manual router.

**A confirmation prompt appears in the CLI.**
If an agent is blocked on an interactive question, the peer may call `pair_unblock`,
or the background `bin/pairwatch --unblock` presses it — but only for what the
allowlist permits (§13b of the protocol). Not every prompt is answered: a `curl` or an
`rm -rf /` stays refused on purpose, and that is not a bug to be worked around. Both
forms are handled: a `[y/N]` question and a TUI menu, which takes `Enter` only.

**An agent is silent.** It means it is gone. Check `bin/pairctl all` — it shows who
has not answered for a long time. `bin/pairwatch` catches this before you notice.

**One agent is stuck.** Usually it waits for an answer that will not come.
`bin/pairdoctor` helps, as does restarting the pane.

**The round was closed and then a defect was found.** The phase can be stepped back
one step, by anyone, without write authority: an agent will return the round itself
if it found something after closing.

**One agent has been working for a second hour.** Nobody stops it from finishing.
`pairwatch` shows the silence; the decision is yours.