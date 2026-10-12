---
name: pair-audit
description: Verify the evidence of a finished task is real — tests failed before the change and pass after, nothing skipped, mocked or weakened to force green, and no comment promising protection that no test exercises. Use before declaring any pair task done, and whenever a task claims tests pass.
---

# Audit

> Language: this is the English original. The Russian mirror is [SKILL.ru.md](./SKILL.ru.md).

> **Not the same as the round's `audit` mode.** This skill is about verifying the
> evidence of a change already made: did the test fail before it and go green
> after. A round in `mode=audit` (`bin/pairctl mode audit`) is something else:
> there are no changes there and nothing to roll back.
> A task phrased as "find defects, change nothing" is a round mode, and it is
> served by `pair-work`; this skill does not apply to it.

A task is not finished until "tests pass" has been verified as true. The most
expensive mistake here: a green report with no verification behind it.

## 1. Tests catch the bug, not just stay green

- Roll back the change: the test must fail. Green even before means it verifies nothing.
- Find the test that catches **exactly this** error, and show it.
- If there is none — that is a finding, not a detail.

## 2. Tests are not weakened

Look for `skip`, `only`, `xit`, `todo`, commented-out asserts, `mock`,
`stub`, increased `timeout`, `assert.ok(true)`, `.rejects` on a positive case.

## 3. Tests actually ran

Not "the command exited 0", but **the number of tests run**. Zero tests run
also exits zero.

## 4. No bypass

The fix must not disable what it repaired: a deleted test, a weakened linter,
a disabled typecheck, `continue-on-error` in CI, narrowed `permissions`.

## 5. A comment without a test is a finding

Separately and strictly. Code often carries an explanation:

```ts
// the numeric branch guards against overflow
if (typeof id === "number") { ... }
```

If no test executes this branch — the comment is **lying**. Check:

1. Roll back or alter the guard so the test should fail.
2. Run it — if everything stays green, the guard never executes.
3. Then the code is dead, and the comment promises something that is not there.

Why this matters: in six months someone will delete the branch as "duplication"
based on the comment alone. The evidence is two files: the line with the
explanation and the test file where this branch is absent.

## 6. The area is covered

Verify what was fixed. Something broken on the side is a separate finding,
not a "noticed in passing".

## How to report

A bullet list with a verdict: `confirmed` / `not verified` / `suspicious`.
State every unverified item directly.

Silence reads as "all good", and that is a lie. If you have no proof — write
`pair_note(kind="verdict")` with the text "no evidence".
