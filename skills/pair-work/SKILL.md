---
name: pair-work
description: Full autonomous collaboration with the peer agent. The two of you plan, implement, review each other, test, fix, and open a pull request without asking the user anything. Use when the user says "работаем вместе над X", "работаем вдвоём", "позови соседа на X", "нужна пара", "work with the peer on X", "let's do this together", or "автономно над X".
---

# Autonomous pair work

> Language: this is the English original. The Russian mirror is [SKILL.ru.md](./SKILL.ru.md).

You work together with another agent: a different CLI, a different model, it cannot see your
conversation. The only channel is the `pair_*` tools.

The user gave a task and left. **Your job is to carry it to a PR without their involvement.**

## 0. Digest — cheap start, state right after

**Always start with `pair_digest`, then `pair_state`.** The digest (~1000 tokens)
answers "where does the round stand" (task, mode, phase, open tasks,
obligations, last commit and verdict) and replaces the `pair_state` +
`pair_task` + `pair_transcript` bundle (3724 tokens measured). But the digest does not say
who writes and what blocks closing — for that, read `pair_state` right away
(authority, roster, unread, done-blockers).

Read history only when the digest says you need it. Answer "how is it going?"
with what is **actually true right now** from `pair_state`/`pair_transcript`,
not with a promise to check.

## 1. Split: read first, then claim

**Read the task and the code first. Claim after.** Not the other way around.

```
pair_digest                                       # what the task is, who is here, what is open
pair_state                                        # who writes, what blocks closing
# study the code the task will touch
pair_claim(reason="one sentence: why you")
```

`reason` is not a formality. It lands in the transcript and in the refusal to the peer, so the peer
sees your argument and may disagree with it. "Because I was first" is a bad
answer: whoever read the code is fitter to write than whoever was merely faster.

A standing arrangement beats a race: `pair_claim` answers `by_policy=true`
when the writer was assigned by a human (`pairctl roles`), and there is nothing to argue about.
Only a human changes the arrangement: `pairctl roles --swap`.

If the peer already grabbed it and you think you are the better fit:

1. Name the reason in `pair_send` — concretely, with `file:line`.
2. It may give up the right itself via `pair_release(why="...")`. That is its decision, not your
   transfer: you ask, it decides.

If it is not ready to give it up — work as the critic and wait. A critic who is right is more useful than a writer
who is wrong.

## 2. Live channel: wait for messages, don't fall asleep

This is the main thing that makes the pair work. An agent that silently waits is a dead agent.

**One read per turn.** In your free time call `pair_read(wait_seconds=45)`. It returns
when the peer sends a message, or when the time runs out. Came back empty — **go back to
your task** (or sleep): the peer and the supervisor wake you when the mail arrives, so
reading again in a loop buys nothing and burns a full context on every poll. A read that
would replay a batch you already hold spends its whole wait before answering, so the
loop cannot spin.

The batch it returns stays unread until you ACK it: process it, then pass its
`ack_token` on a later `pair_read`. A retry without the token replays the same
letters, and the letters that arrived after it wait behind it — the answer names how
many (`letters_behind`). Reading again will not bring them; acknowledging will
(round 101 batch contract — see `pair-chat` Reading).

Waiting rules:

- One read per turn. An empty answer is no reason to read again.
- ACK every batch you get. `must_ack` in the answer says the batch is still unacknowledged.
- The `wait_seconds` default is 0 (`DEFAULT_READ_WAIT_SECONDS`, `server.py:97`),
  the cap is 60s (`MAX_READ_WAIT_SECONDS`); beyond that the client timeout cuts it.
- `waited_seconds` is the time the call really took; `wait_budget_seconds` is what it
  was allowed to take. A call that took less than its budget answered early: it
  replayed a batch you already hold.
- Time ran out — go back to your task. Do not sit in the wait forever.
- Both roles may wait. Bounded waiting creates no deadlock:
  it always expires, and you return to work. (Previously the writer was
  forced to 0 — that killed the channel: an agent without waiting went idle and missed
  mail. The winning code is `tool_pair_read`, `server.py:2341`.)

**When asked "how is it going?" — never answer "checking in a sec".** That is the most annoying
phrase in the whole system. Open `pair_state` or `pair_transcript` and answer with what is
**actually true right now**: phase, what is done, what is pending. If nothing changed —
say so.

## 2a. Two ways to deliver

The body always lands in `.pair/mailbox/`; only a short pointer goes to the composer.
The terminal is a display, not a bus between agents. The full contract is in the
`pair-chat` skill (single source of truth); here only the choice of path:

**`pair_send`** — a message into the mailbox. It sits there until the peer itself
calls `pair_read`. Take it for reports, plans, findings: they are not needed
immediately. With `expects_reply=true` it opens an obligation (queue limit —
2 unanswered per role, `MAX_OPEN_REVIEWS`, `mcp/pair_mailbox/review.py:27`);
a plan before the first commit of a `fix` round carries `plan`/`план` in `subject` (§9a).

**`pair_deliver`** — text into the mailbox + a pointer into the peer's composer in its
tmux pane. It wakes up on its own and takes it as its next turn. Take it when
you need it to do something **now**: a question whose answer you need before you
go further.

If the composer is busy — the typed input returns a refusal instead of overwriting what it prints.
The text is already in the mailbox: go back to waiting. On an unproven (foreign) pane typed
input is forbidden — only `display-message` + mailbox, otherwise it is injection into someone
else's composer.

## 2b. Round mode: digest, then `pair_state`

`pair_state` returns `mode`. It decides what the round does at all, and it is not in the task
text — otherwise the agent's instruction ("you are the writer, edit files") may end up
contradicting the task, and the one in the system prompt wins.

**`fix`** (default) — everything as described in §3: rounds, code, review, branch, PR.

**`audit`** — the project is not intentionally changed. Nobody edits files, creates a branch,
commits, or proposes patches: the server refuses patch reservation, and that refusal
lands in the audit. Write authority stays, otherwise the phase has nowhere to move, but the subject
is different: the writer leads the round, not commits code.

The cycle in `audit`:

**Round 1 — surfaces.** Split the code into surfaces and divide them so that
each is covered, not "both looked at server.py". What a surface is — you decide
together; it is not from the task.

**Round 2 — findings.** The writer hunts defects and writes each into
`pair_note(kind="finding-<ID>")`: where (`file:line` + function name), what breaks
(two concrete actions and what happens between them), how to verify, severity, and **what the check
does NOT prove**. The last field is required: without it the finding looks stronger than
it is.

**Round 3 — refutation.** This is what makes `audit` different from `fix`, and it is its point.
Each of you takes the peer's finding and hunts for why it is wrong. Find it — write
`pair_note` with `refuted` and the reason. Do not find it — `confirmed`. Only what is
confirmed by both goes into the report.

**Round 4 — report.** The writer assembles `pair_note(kind="audit-<date>")`: a table
of ID / severity / where / status, and a section **"what is NOT covered"**. Silence reads as "all
good", and that is a lie if you simply did not look. Confirmed items move to
`docs/backlog/` under the `pair-backlog` rules — those are the only files that in
`audit` get created at all.

The same cap — 4 rounds, then `handoff` with an honest account of what did not converge.

**What is forbidden in `audit`:** editing project files, `git checkout -b`, commits,
push, PR, `pair_patch_reserve`. Checks that do not change state — allowed and expected.

## 2c. Switching mode mid-work

A human changes the mode, and only a human changes it.

`audit` → `fix` after an audit is the usual next step, not a restart:

```sh
pairctl mode fix          # same round, same conversation, different work
```

Findings are not thrown away: confirmed ones sit in `docs/backlog/<slug>.md`, and the fix
round takes them from there as ready input. Tell the peer in its window that the mode changed
and which files to take — the agent reads `mode` itself, but does not know that you did this and why.

`fix` → `audit` is a rollback. Same call, and the agent stops editing files. Useful when
the fix sprawled and you need to go back to talking about what is happening at all.

Note: switching mode does not touch edits already made and does not roll back the phase.
If `fix` had commits, they stay, and that is fine: audit is about what comes
next, not about rewriting history.

## 3. Cycle (`fix`)

Repeat until green. Cap of 4 rounds, then `pair_note(kind="handoff")` with an
honest account of what did not converge.

**Round 1 — plan.** The writer studies the code, writes `pair_note(kind="plan")`, sends
the plan to the critic via `pair_send` with `expects_reply` and `plan` in `subject` — and waits
for an objection or agreement. No answer — no code (§9a). The critic hunts holes:
wrong assumptions, no error handling, unclear where it breaks. Bad
plan — writes why, the writer rewrites.

**Round 2 — code.** The writer follows the plan and runs tests. No green tests —
no round.

**Round 3 — review.** The critic reads the whole `git diff`. Findings go into
`pair_note(kind="finding")` with `file:line`. Real ones — the writer fixes, the critic
re-checks exactly those. False ones — the critic records why in `verdict`.

The critic does not invent problems for volume. If the code is good — it says so plainly.

**Round 4 — repeat.** What was found is fixed, tests are green, the critic confirmed.
The writer records `pair_evidence(kind="test", ...)` with what the machine actually
printed (count and exit code), and only then `pair_advance("verify")`, then
`pair_advance("done")` — but only after reading all the mail: the server will not let you close the round over
unread mail. The full `done` gate list is in §9 below.

## 4. Bar for a finding

Four questions, otherwise it is not a finding: what breaks, why it is vulnerable, at what cost, what
to fix. A `file:line` reference is required. One strong conclusion beats five weak ones.

**Not a finding:** what a linter or tests will catch; style and naming; "add tests".

## 5. Git

Only in `mode=fix`. In `audit` — none of this: git is not touched at all.

- Own branch: `git checkout -b <short-task-name>` at the start.
- Commit after every green step, message about **what changed**, not "fixes".
- Green tests required before commit.
- At the end: `git push` and a GitHub PR. **This is part of the work, not a separate action.**
- **Never push to `main`.** Branch and PR only.
- Do not tell the user that push and PR are "their business". That is your job.

## 6. When to stop

Only six things: `.env`, secrets and tokens, ports and deploy addresses, deploy,
DB migrations, deleting data or git history.

Decide everything else between yourselves. Uncertainty is no reason to wake the user:
pick an option, record `pair_note(kind="decision")` with the reason, move on.

## 7. Communication

The full correspondence contract is in the `pair-chat` skill (single source of truth); here
only the working rule: `pair_deliver`, `pair_send` and `pair_read`. Arguing: two
agents politely converging on an opinion gives nothing.

## 7a. Reading notes

**Always call `pair_note_index` before reading notes.** It shows each note's size
without reading the contents. Do not read a note wholesale with `read` — use
`pair_note_read` (default 120 lines, cap 400; `NOTE_READ_DEFAULT`/`NOTE_READ_MAX`,
`server.py:183,198`).

If a note is bigger than 5 KB — read only the latest entries. Rotation carries old entries
into `notes/archive/` at 600 lines (`NOTE_LINE_CAP`, `server.py:182`), leaving
a pointer — nothing is lost.

Note kinds — six, per PROTOCOL.md §9: `plan`, `decision`, `finding`, `verdict`,
`handoff`, `backlog` (in audit: `finding-<ID>` and `audit-<date>`).

**Ban on manual relay through the human:** Never ask the user to pass text or confirmation
to the peer's window (*"there is a request hanging in the opencode window for your signal..."*). To pass messages and
wake the peer use `pair_deliver`. The user gave the task and expects a finished result
(PR), not a job as a manual router between windows.

Never tell the user "the peer will look now" — you do not know that.
Sent means delivered to its mailbox or window, and that is all.

## 7b. Quiet tools (no separate section, but call them)

22 tools total (`tool_definitions`, `server.py:3241`): state, comms,
patches, tasks, notes, digest, transcript, unblock.
`pair_ping` (sign of life instead of silence), `pair_task` (a round does not close
with open tasks), `pair_evidence` (`done` gate in `fix`), `pair_transcript`
(audit trail `transcript.jsonl`: what actually happened), `pair_note_index`
(see §7a), `pair_unblock` (peer's pane on a permission prompt: confirm
only per allowlist).

Storage is `Store` in `.pair/`: `mailbox/`, `notes/`, `patches/`,
`obligations/`, `notifications/`, `state.json`, `transcript.jsonl`
(`server.py:726`). Not `.pair/` under another name and not `pair-transcript.jsonl`.

Six roles (`cli-profiles.json`: opencode, agy, codex, claude, pi, omp);
`pair-duo` raises a double session only for the first four
(`bin/pair-duo:75`). Default autonomy is `auto`
(`resolve_autonomy`, `server.py:321`); `finish.sh` takes the task only
via the `--task` flag (no positional argument, `finish.sh:131`).

## 8. Write authority

Nobody can take or pass the right. A peer's message never moves it, in either
direction.

The holder may give it up **itself**: `pair_release(why="...")`. That is its decision, not your
transfer — you may ask, but it decides.

A standing role arrangement beats a race: `pair_claim` answers `by_policy=true`
when the writer was assigned by a human, and arguing here is out of place — work as the critic.
Only a human changes the arrangement: `pairctl roles --swap`.

The final word is always the human's:

```sh
bin/pairctl assign <role>   # assign as writer (role = peer CLI name)
bin/pairctl release         # clear, nobody writes
bin/pairctl why             # who writes and why it said so
```

A round that looks closed early can be reopened from any role:
`pair_advance` one step back is available to all (forward — writer only).
This needs no write rights: reopening is not a write operation.

## 9. `done` gates — complete, no surprises

`pair_advance("done")` refuses while anything hangs (`done_blockers`,
`server.py:409`, plus queue and stale verdict in `_done_blockers`, `server.py:1760`):

1. Unread mail — `pair_read` first.
2. No passing `evidence` at all — record what the machine actually ran.
3. In `fix`, no passing `kind="test"` from the writer — someone else's `ok` and `lint`/
   `typecheck`/commit do not substitute; in `audit` this is not required.
4. Failed latest run of a kind (`ok=false`) — fix and overwrite with a pass,
   or record in a note why it does not block.
5. Unclosed sealed patches — `verify` + `applied` (or `release` with a reason);
   `ok=false` keeps the round open.
6. Open tasks — `done` on fact or `drop` with a reason.
7. Hanging review obligations and a stale verdict relative to HEAD.
8. In `semi` there is no human word (`bin/pairctl ok` on `verify`/`done`); the
   `pair_send(expects_reply)` queue — at most 2 open (`review.py:27`).
