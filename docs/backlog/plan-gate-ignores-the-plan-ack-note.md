---
title: The plan gate looks for the critic's visa only in notes/verdict*.md — a visa written to notes/plan-ack.md is invisible to it
found: 2026-10-19T23:25Z
severity: medium
round: 126
status: open
---

## Where

`mcp/pair_mailbox/plan_gate.py:111`:

```python
for path in sorted(notes_dir.glob("verdict*.md")):
```

The visa is then matched inside those files by `NOTE_ENTRY` (the `## <stamp> —
<role> (note <sha> attempt <n>)` header) and `ACK_LINE` (`plan-ack round N`).

## What breaks

The gate finds a visa only if the peer happened to put it in a note whose FILE
NAME starts with `verdict`. The note's *kind* is irrelevant to the glob; only
the name matters.

Isolated reproduction, identical content in both files, only the name differs:

```
critic_ack on notes/plan-ack.md  ->  {'ack': False, 'why': 'no critic note with this round plan-ack'}
critic_ack on notes/verdict.md    ->  {'ack': True, 'role': 'agy', 'at': '2026-10-19T22:49:27+00:00'}
```

This is not hypothetical in this repository. The live room has a visa for round
125 written to `notes/plan-ack.md`:

```
$ grep -rln "plan-ack round 12" .pair/notes/*.md
.pair/notes/plan-ack.md
.pair/notes/verdict.md

$ sed -n '4p' .pair/notes/plan-ack.md
plan-ack round 125
```

`plan-ack.md` is invisible to the gate. The only entries the gate can see are
`plan-ack round 123` in `.pair/notes/verdict.md:492` and `:504` (a state
file, not a tracked one - the line numbers are of the room at the moment of
the measurement, 2026-10-19).

The consequence is a **silent 24-hour delay** on the `plan → implement`
transition. When the visa is not found, `plan_gate.gate` falls through to
`timeout_ack` (`plan_gate.py:161`), which allows the move only after
`DEFAULT_ACK_TIMEOUT_HOURS = 24` (`plan_gate.py:26`) have passed since the
mtime of `.pair/plan.md` — and only if the writer has no open obligations
towards the neighbour. The peer has done its part; the writer waits a day.

`docs/PROTOCOL.md:337` states the rule as "a `kind=verdict` note containing
`plan-ack round N`" — formally the gate is right and the peer is wrong. But
`plan-ack` is the obvious kind for a plan acknowledgement, the name is already
taken in this very project, and the protocol says nothing about the FILE NAME
being the part that matters. A contract whose failure mode is a silent day-long
wait, and whose only enforcement is a filename glob nobody is told about, is a
defect in the contract, not only in the peer's choice.

## What to do

- Search all `notes/*.md` for the visa, not just `verdict*.md`. The `kind` is
  metadata the server already records; the content is what the gate needs.
- Failing that, state the filename requirement in `docs/PROTOCOL.md` AND in the
  skills that tell the peer how to give a visa (`skills/pair-work/SKILL.md`
  currently never mentions `plan-ack` at all), and make the refusal message
  name the files that were searched.

The second option is weaker: it keeps a day-long silent failure for a peer who
guessed a different name, and the note kind that reads most naturally is
exactly the one that fails.

## Red-proof

A test that writes the visa into `notes/plan-ack.md` (with a correct header)
and asserts `plan_gate.gate` accepts it. Before the fix, the gate reports
"no critic note with this round plan-ack" for a note that says exactly that.

## Severity

medium — a machine gate that rejects a correct visa because of the file's name,
with a silent 24-hour fallback instead of an error.
