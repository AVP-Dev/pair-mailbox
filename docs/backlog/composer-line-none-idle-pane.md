---
title: composer_line() returns None on an idle pane whose prompt has no classic marker
found: 2026-10-17T19:00Z
severity: low
round: 104
status: open
---

Measured on the peer's live idle pane: `is_composer_empty` -> False while
`composer_line` -> None. The pane is idle (nowhere to type is not the truth), so
callers refuse to deliver to a CLI that is in fact waiting.

Cause: the composer's prompt in this CLI is a bare line between horizontal rules,
with the status rows beneath it. `composer_line()` scans the last 8 lines for a
PROMPT_MARKERS prefix and finds none; `looks_like_status_line()` then rejects the
bare line as a status row. Red-proven on a captured frame during round 104.

Real but separate from every round's subject (it is a geometry defect, not a
protocol one), so it was parked rather than fixed in-line.

Cure direction: a line directly under a horizontal rule, with no prompt above it
and the pane not busy, IS a prompt row; only treat a genuinely absent composer
field as None. The busy field must keep its own detection so a typing pane still
reads busy.
