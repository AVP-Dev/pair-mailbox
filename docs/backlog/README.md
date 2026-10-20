# The backlog

This directory holds **open debt only**: real defects, confirmed, not yet fixed.

A card that is fixed is **deleted**, and the commit that closed it says what
closed it — the git history keeps the record, this directory keeps the debt.

- open cards: `python3 scripts/backlog-check.py --index`
- closed cards: `python3 scripts/backlog-check.py --closed`

The rule is in `docs/PROTOCOL.md` (and its Russian mirror) and in
`skills/pair-backlog/SKILL.md`. `scripts/backlog-check.py` enforces it in the
static layer of the gate.
