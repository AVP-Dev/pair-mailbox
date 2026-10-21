---
title: The pair skills cite NOTE_READ_DEFAULT, NOTE_READ_MAX and NOTE_LINE_CAP at lines that moved
found: 2026-10-21
severity: low
round: 134
status: open
---

## Where

- `skills/pair-work/SKILL.md:256-257` — `NOTE_READ_DEFAULT` / `NOTE_READ_MAX` cited as
  `server.py:183,198`
- `skills/pair-work/SKILL.md:260` — `NOTE_LINE_CAP` cited as `server.py:182`
- `skills/pair-work/SKILL.ru.md:259` and `262` — the same three citations
- the constants themselves: `mcp/pair_mailbox/server.py:158` (`NOTE_READ_DEFAULT = 120`),
  `mcp/pair_mailbox/server.py:173` (`NOTE_READ_MAX = 400`),
  `mcp/pair_mailbox/server.py:157` (`NOTE_LINE_CAP = 600`)

## What breaks

Nothing at runtime. The cited lines hold different constants today, so an agent
following the citation lands on the wrong line and reads the wrong name — the
guidance is the only consumer of these citations, and it is the guidance that is
wrong.

Three of the three cited line numbers are off, in both language versions of the same
skill.

## What to do

- fix the three citations in both `SKILL.md` and `SKILL.ru.md`;
- decide whether citations like this should be checked by a gate. `scripts/staleness-detector.py`
  exists, but it checks a different family (staleness-keyed fields written as
  `snapshot.get(K, measurement)`) and does not look at documentation citations.

## Red-proof

- a check that resolves every `file:line` citation in `skills/**` and asserts the
  cited line still contains the named symbol — the property that is missing today;
  the fix is that check's own report, red on the three current citations and green
  after the fix.

## Severity

low — a stale pointer in guidance, not a defect in behaviour. It was found while
measuring the token cost of `pair_note_read`, whose defaults the same paragraph
cites.
