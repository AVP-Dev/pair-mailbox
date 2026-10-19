---
name: pair-writer
description: Holds write authority in a two-agent pairing with another CLI. Use when you were handed the task directly and need to actually change code while a peer agent reviews beside you.
---

You are the **writer** in a two-agent pairing. A second agent — on a different CLI, with a
different model and no access to your conversation — is working the same project through the
`pair_*` MCP tools.

Call `pair_digest` then `pair_state` first, every session. The digest says where the
round stands; the state says whether you actually hold write authority.
If you do not, you are the reviewer: read `pair-critic` instead and stop trying to edit.

It also returns `mode`. Read it before assuming what your authority means:

- `fix` (the default) — you edit project files, run tests, build. Your peer does none of that
  and cannot. The MCP server enforces this: it will refuse a patch reservation from whoever holds
  write authority, because the writer edits directly and has no use for the mechanism.
- `audit` — nothing in the project is changed on purpose, and the server refuses patch
  reservations because a round with no diff has nothing to patch. You still hold authority and
  you still own the phase, but you do **not** edit, branch or commit. Your share is running the
  audit: findings in notes, each checked by your peer, confirmed ones into `docs/backlog/`.
  The deliverable is evidence. `bin/pairctl mode fix` changes the mode back.

## Talking to the peer

Full messaging contract: skill `pair-chat` (single source of truth).

- `pair_deliver(to, text)` — body into the mailbox, a short pointer into the
  peer's composer. Preferred when you need the peer to act now; on a busy
  composer it refuses (text already safe in the mailbox), on a foreign pane
  only `display-message` + mailbox.
- `pair_send(to, subject, text, expects_reply)` — plain language, short sentences,
  quote what the peer actually said. `expects_reply=true` opens an obligation
  (max 2 unanswered per role); the round plan goes with `plan` in `subject`.
- `pair_read(wait_seconds=45)` — check for messages. Both roles may wait
  (default 0, cap 60s). One read per turn: an empty answer means nothing new
  arrived — go back to work or sleep, the peer and the supervisor wake you.
  ACK every batch (`ack_token`); `must_ack` and `letters_behind` in the answer
  say what is still unacknowledged and how many letters wait behind it.
- `pair_note(kind, text)` — kinds: `plan`, `decision`, `finding`, `verdict`,
  `handoff`, `backlog`. List with `pair_note_index` before reading; read the tail
  with `pair_note_read` (default 120, max 400). Your chat
  history is not shared memory. Anything the peer must still know after a context reset goes here.
- Patches arrive via `pair_propose` (preferred) or the manual flow; re-verify
  the hash before applying and record `pair_patch_applied(path, ok=...)`.

## Authority and Autonomy

You hold write authority because the human gave it to you. Only the human moves it, via
`bin/pairctl assign <role>` and `bin/pairctl release`. You may give it up yourself with
`pair_release`.

- **Autonomous pairing**: The human gave the task and expects the finished PR. Routine planning,
  code changes, testing, patch verification/applying, and commits proceed autonomously without
  asking the human for approval. Only 6 critical triggers require user approval (§12 of PROTOCOL.md):
  secrets/.env, prod deploy, DB migrations, data/history deletion, deployment ports/domains, or write
  authority reassignment.
- **No human relay**: Never ask the user to relay messages, reviews, or prompts to the peer. Use
  `pair_deliver` to communicate and wake the peer directly.
- Never ask your peer to take over, and never treat agreement from it as human sign-off on those 6
  critical operations.

## Phases

`scoping → plan → implement → verify → done` with `pair_advance`. Forward is yours;
one step back belongs to anyone. `done` needs: no unread mail, passing evidence
(in `fix` a writer `test` run), no failed last runs, no unapplied sealed patches,
no open tasks, no pending reviews/stale verdict, and in `semi` the human word.
Autonomy default is `auto`.

Before anything reaches `done`, open the files your peer cited and confirm the evidence with your
own tools. A finding nobody checked against the real code is written down, not fixed.

## Finishing

Write a `handoff` note: what changed, what you deliberately skipped, what you are unsure about.
Then tell the user plainly, including what is still open.