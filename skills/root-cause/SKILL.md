---
name: root-cause
description: Systematic root-cause analysis of a bug or unexpected behaviour using 5-Why, before touching any code. Use on errors, test failures, flaky behaviour, "это не работает", "падает", "непонятно почему", or when the same fix has already failed twice.
---

# Root cause

> Language: this is the English original. The Russian mirror is [SKILL.ru.md](./SKILL.ru.md).

A symptom is almost never the cause. Fix the symptom and you get a bug that comes back.
Cause first, code after.

## Rule

**Not a line of code until the cause is named.** Two failed approaches to one
symptom are not two solutions but a new guess. Do not try a third approach:
go back to analysis.

## Steps

1. **Symptom.** What we observe, how to reproduce, what was expected.
2. **5 whys.** Ask "why?" five times, verifying each answer with its own tool. Stop when
   the cause stops being about code and becomes about a decision or a process.
3. **Boundary.** Runs along data, environment, ordering, parallelism.
4. **Name the cause.** One sentence. Cannot do it — you found another symptom.
5. **Record it.** `pair_note(kind="finding")` with the cause and the check that confirmed it.

## Stop

- Cause named and confirmed — fix.
- Three rounds with no progress — stop, `pair_note(kind="verdict")`, the task is harder than it looked.
