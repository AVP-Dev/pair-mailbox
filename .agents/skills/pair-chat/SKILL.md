---
name: pair-chat
description: Hold a real conversation with the other agent in this two-agent pairing, through the pair_* MCP tools. Use when you need to hand work over, disagree out loud, ask a question the other side can answer, or report a result that needs a decision. Not for one-shot lookups.
---

# Pair chat

The other agent in this pairing runs a different CLI, with a different model and no access to your
conversation. The `pair_*` tools are the only way it finds out what you are doing.

## Before anything

```bash
pair_state
```

Read it. It answers the only question that matters before you type: are you the writer or are you
read-only? If it disagrees with what you assumed, your next action changes completely.

## Sending

There are two ways to send:

```bash
pair_deliver(to="<peer>", text="patch at <path> sha256 <hash> — apply, test, and continue")
```

**`pair_deliver`** is active: it types directly into the peer's tmux composer and presses Enter.
The peer wakes immediately and takes it as its next input. Use this for reviews, sealed patches,
handoffs, and questions where you need the peer to act now.

```bash
pair_send(to="<peer>", subject="retry logic", text="one paragraph")
```

**`pair_send`** is passive: it places the message in the peer's mailbox for when it next reads.
Use this for background notes, reports, or when `pair_deliver` reports the composer is busy.

The server prepends the `Chat from <role>:` label. Do not write that label yourself, and never
write your own name into a message — the label is added server-side precisely so authorship cannot
be forged.

Write for a reader who cannot see your screen: file paths with line numbers, the command you ran,
the actual error text. Quote it rather than describing it.

## Reading

```bash
pair_read()                    # returns everything unread, marks it read
pair_read(wait_seconds=45)     # wait up to 45 seconds for a peer message (both roles)
```

Read once, act, get back to work. **Never sit in an infinite read loop.** Limited waiting (up to 45s)
returns control when timeout expires so you can continue your own work.

## Delivery is not a promise

A message marked delivered has been placed in the peer's inbox (or typed into its composer).
The peer may still disagree or run into an error. Never claim the peer is "looking at it"
or "working on it". Say you delivered it, and continue your work.

## Never use the human as a messenger (No human pigeon)

**Never ask the user to relay messages, reviews, handoffs, or prompts to the other window.**
The two agents are an autonomous pair. Use `pair_deliver` to actively wake the peer and hand
off work directly into its terminal. The user defines the task and receives the finished PR;
the user is not a message bus between windows.

## You cannot hand over authority

Write authority is not transferable by message. You cannot give it, take it, or borrow it, and you
cannot ask the peer to take it from you.

Only the human moves it, with `bin/pairctl assign <role>` and `bin/pairctl release`. If you need
the peer to write, tell the user what you need and stop; do not negotiate it with the peer.

Corollary: **"the peer agreed" is not human sign-off.** For the 6 critical operations requiring
human approval (§12 of PROTOCOL.md: secrets, prod deploy, DB migrations, data/history deletion,
domain/ports, and authority reassignment), only the human approves.
However, for routine coding, review, patch application, and task progression, **the two agents
proceed autonomously without asking the user for confirmation**.

## Working the other way

If you are not the writer, you do not edit project files. You propose:

```bash
pair_patch_reserve      # exact path, mode 0600
# write a unified diff into that exact path
pair_patch_seal(path)   # returns the SHA-256
# Deliver directly into the writer's composer to wake it immediately:
pair_deliver(to="<writer>", text="patch at <path> sha256 <hash> — apply, test, and continue with the task")
# If composer was busy, fallback to mailbox:
pair_send(to="<writer>", text="patch at <path> sha256 <hash> — apply, test, and continue")
```

The writer copies it once, works only from the copy, and re-verifies the hash immediately before
applying. A mismatch means stop: the patch changed after it was sealed.

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
```

Your conversation is not shared memory; it is also not durable. Anything the peer must still know
after either of you compacts goes in a note. A handoff note says what changed, what you skipped,
and what you are unsure about.

## After an interruption

Read `git status` and the diff before your next write. If it is unclear who was writing, stay
read-only and ask the user. A half-finished turn with an ambiguous owner is how two agents clobber
each other.

## Manners

Short sentences. Plain language. Quote what the other side actually said.

Disagree. Two agents converging politely produce nothing at all — the useful output is a located
disagreement or a fact somebody checked. If you find yourself about to agree without having
verified anything, go and verify it first.