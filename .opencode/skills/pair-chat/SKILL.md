---
name: pair-chat
description: Hold a real conversation with the other agent in this two-agent pairing, through the pair_* MCP tools. Use when you need to hand work over, disagree out loud, ask a question the other side can answer, or report a result that needs a decision. Not for one-shot lookups.
---

# Pair chat

> Единственная истина про переписку пары — этот файл.
> Упоминания в `AGENTS.md`, `docs/PROTOCOL.md`, `docs/HOW-IT-WORKS.md` —
> контекст, не контракт: при расхождении побеждает этот файл и код
> `mcp/pair_mailbox/server.py`.

The other agent in this pairing runs a different CLI, with a different model and no access to your
conversation. The `pair_*` tools are the only way it finds out what you are doing.

## Before anything

Cheap orientation first, full state after:

```bash
pair_digest     # task, mode, phase, open tasks, obligations, last commit/verdict (~1k tokens)
pair_state      # full state: authority, roster, unread, done-blockers
```

`pair_digest` replaces `pair_state` + `pair_task` + `pair_transcript` for the
"where does the round stand" question. Read history only when the digest says
you need it. When a status question arrives, answer with what is true now from
`pair_state`/`pair_transcript` — never with a promise to go and check.

## Sending

There are two ways to send. The body always lands in `.pair/mailbox/`
(a container independent of tmux); only a short pointer goes into the peer's
TUI — the terminal is a display, not a bus between agents.

```bash
pair_deliver(to="<peer>", text="one paragraph: what and where to read it")
```

**`pair_deliver`** — data into the mailbox, a short pointer into the peer's
tmux composer (`[pair] <role>: новое сообщение в ящике, прочитай pair_read`).
The peer wakes itself and reads it as its next instruction. Use this when you
need the peer to act now: reviews, sealed patches, handoffs, blocking
questions. If the composer is busy the call refuses instead of overtyping —
the text is already safe in the mailbox, so fall back to `pair_send` and wait.
On an unproven (foreign) pane typed printing is forbidden (injection into
someone else's composer); the fallback is `display-message` + mailbox only.

```bash
pair_send(to="<peer>", subject="retry logic", text="one paragraph", expects_reply=true)
```

**`pair_send`** is passive: it places the message in the peer's mailbox for when it next reads.
Use this for background notes, reports, or when `pair_deliver` reports the composer is busy.

- `subject` — short subject line; a plan sent before the first commit of a
  `fix` round carries `plan`/`план` in it (PROTOCOL.md §9a).
- `expects_reply=true` — set when the message asks for a decision, a review,
  or an action. It opens an obligation the supervisor watches, so a request
  nobody answered cannot end the night in silence. Review queue limit: at most
  2 unanswered requests per role (`MAX_OPEN_REVIEWS`, `mcp/pair_mailbox/review.py:27`);
  on the third — stop and read (`pair_read(wait_seconds=30)`).

The server prepends the `Chat from <role>:` label. Do not write that label yourself, and never
write your own name into a message — the label is added server-side precisely so authorship cannot
be forged.

Write for a reader who cannot see your screen: file paths with line numbers, the command you ran,
the actual error text. Quote it rather than describing it.

## Reading

```bash
pair_read()                    # returns everything unread (limit 10), marks it read
pair_read(wait_seconds=45)     # bounded wait for a peer message
```

- `wait_seconds` default is `0` (`DEFAULT_READ_WAIT_SECONDS`,
  `server.py:97`); cap is 60s (`MAX_READ_WAIT_SECONDS`), further capped by the
  client timeout. Both roles may wait — a bounded wait always expires, so it
  cannot deadlock. Empty answer means call again; waiting is the normal way to
  hear from the peer.
- Closing is refused over unread mail: a report the writer never read is not a
  report. Run `pair_read` before `pair_advance("done")`.

## Delivery is not a promise

A message marked delivered has been placed in the peer's inbox (or a pointer
typed into its composer). The peer may still disagree or run into an error.
Never claim the peer is "looking at it" or "working on it". Say you delivered
it, and continue your work.

## Never use the human as a messenger (No human pigeon)

**Never ask the user to relay messages, reviews, handoffs, or prompts to the other window.**
The two agents are an autonomous pair. Use `pair_deliver` to actively wake the peer and hand
off work directly into its terminal. The user defines the task and receives the finished PR;
the user is not a message bus between windows.

## You cannot hand over authority

Write authority is not transferable by message. You cannot give it, take it, or borrow it, and you
cannot ask the peer to take it from you. The holder may give it up itself with
`pair_release(why="...")` — that is its own decision, not your transfer.

Only the human moves it, with `bin/pairctl assign <role>` and `bin/pairctl release`. If you need
the peer to write, tell the user what you need and stop; do not negotiate it with the peer.
A standing role arrangement wins over a race: `pair_claim` answers
`by_policy=true` when the human pinned the writer, and the pin changes only via
`pairctl roles --swap`.

Corollary: **"the peer agreed" is not human sign-off.** For the 6 critical operations requiring
human approval (§12 of PROTOCOL.md: secrets, prod deploy, DB migrations, data/history deletion,
domain/ports, and authority reassignment), only the human approves.
However, for routine coding, review, patch application, and task progression, **the two agents
proceed autonomously without asking the user for confirmation**.

## Working the other way

If you are not the writer, you do not edit project files. Preferred path is one call:

```bash
pair_propose(files=["path/to/file.py"])   # reserves, seals, and notifies the writer itself
```

Manual path (reserve → write diff → seal → send path+sha256 → writer verifies →
writer records applied) stays for exotic cases. `fix` mode only — in `audit`
the server refuses the reservation. The writer re-verifies the hash immediately
before applying and stops on a mismatch; after applying it records
`pair_patch_applied(path, ok=...)`, with `ok=false` on a failed apply (a
rejected hunk stays unresolved and keeps the round open).

Release your own file with `pair_patch_release(path)`, by exact path. **Never clean that directory
with a glob** — you share it with a process you cannot inspect, and a glob will delete a file that
belongs to the other agent. After an interruption, check whether your file survived and remove it
by its exact path first.

## Leaving something behind

```bash
pair_note(kind=plan,     text="...")
pair_note(kind=decision, text="...")
pair_note(kind=finding,  text="...")
pair_note(kind=verdict,  text="...")
pair_note(kind=handoff,  text="...")
pair_note(kind=backlog,  text="...")
```

Kinds are fixed by PROTOCOL.md §9: `plan` (what was agreed and in what order),
`decision` (the choice and the reason), `finding` (checked against the code,
with `file:line`), `verdict` (the outcome and what it did NOT cover),
`handoff` (done / skipped / unclear), `backlog` (a real defect outside the
scope). In `audit`, `finding-<ID>` per surface and `audit-<date>` for the report.

Your conversation is not shared memory; it is also not durable. Anything the peer must still know
after either of you compacts goes in a note. A handoff note says what changed, what you skipped,
and what you are unsure about.

Notes grow only at the tail and rotate at 600 lines (`NOTE_LINE_CAP`,
`server.py:182`) into `notes/archive/`. Never read a note with a file read:
list first, then read the tail.

```bash
pair_note_index               # sizes and freshness, largest first — always before reading notes
pair_note_read(kind, tail_lines=120)   # tail only; default 120, capped at 400
```

## After an interruption

Read `git status` and the diff before your next write. If it is unclear who was writing, stay
read-only and ask the user. A half-finished turn with an ambiguous owner is how two agents clobber
each other.

## Quiet tools (no separate section needed)

- `pair_ping` — cheap sign of life (three fields instead of the full state).
  Any `pair_*` call already moves `last_seen`; ping when you have nothing else
  to say so the supervisor tells "silent but working" apart from stuck.
- `pair_task` — shared task list. The round cannot close while any task is
  open: finish (`done`) or drop with a reason.
- `pair_evidence` — machine-checkable fact (tests with count and exit code,
  lint, typecheck, commit sha, PR URL). In `fix`, `done` requires a passing
  `test` run recorded by the writer.
- `pair_transcript` — append-only audit trail: every message, claim, phase
  change, patch seal and hash verification. Reconstruct what actually happened.
- `pair_note_index` — see above; always before reading notes.
- `pair_unblock` — check whether the peer's pane is stuck on a permission
  prompt and confirm it only if the prompt is on the allowlist. Anything that
  writes, runs, or deletes is refused by default.

## Manners

Short sentences. Plain language. Quote what the other side actually said.

Disagree. Two agents converging politely produce nothing at all — the useful output is a located
disagreement or a fact somebody checked. If you find yourself about to agree without having
verified anything, go and verify it first.
