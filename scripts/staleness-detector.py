#!/usr/bin/env python3
"""Write-once staleness detector (round 103 S4) — NARROW, and it says so.

The syntactic family this checks: a staleness-keyed field whose value is
`snapshot.get(K, <measurement>)` — the copy-paste shape that froze
asked_at/seen_at_ask/unread_at_ask in wake.py and bin/pairpilot (round 102:
opencode's wake record carried a 17:22:42Z asked_at while its own reasons
listed the notes written minutes ago; unread_at_ask frozen at 0 made the
pilot escalation gate unsatisfiable forever).

It is NOT a class-cleanness claim. зелёный output names exactly what is
checked, never "class clean": the semantic instances of the same
anti-pattern (manifest.py is_open branch, wrong-path recompute, the
read-side gate at pairpilot escalation) are covered by behavioural tests
(test_manifest.py TestMergedPredicate103, test_liveness.py
TestPidSlotAlive, pairpilot wake tests) — no regex can catch those.

The matcher keys on the ACCESS shape (`.get(K, ...)` with two+ args), not
names, so a rename of `previous` does not defeat it. Exit 1 on a hit.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

KEYS = {"asked_at", "seen_at_ask", "unread_at_ask"}
# The three writers where the family lived. New files teaching the pattern
# must add themselves here — an unregistered site is a new copy of the defect.
FILE_PATHS = ("mcp/pair_mailbox/wake.py", "bin/pairpilot")


def scan_file(path: str, keys: set[str]) -> list[tuple[int, str]]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        key = target.slice.value if isinstance(target.slice, ast.Constant) else None
        if key not in keys:
            continue
        value = node.value
        # Both defects read `snapshot.get(K)` with a measurement fallback,
        # optionally wrapped:  payload[K] = snapshot.get(K, meas)
        # and                 payload[K] = str(snapshot.get(K) or meas)
        def _or_left(expr: ast.expr):
            if isinstance(expr, ast.Call) and expr.args:
                if isinstance(expr.func, ast.Name) and expr.func.id in ("str", "int",
                                                                        "float", "bool"):
                    return _or_left(expr.args[0])
            if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
                return expr.values[0] if expr.values else None
            if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Or):
                return expr.values[0] if expr.values else None
        left = _or_left(value)
        two_arg_get = (isinstance(value, ast.Call)
                       and isinstance(value.func, ast.Attribute)
                       and value.func.attr == "get"
                       and len(value.args) >= 2 and not value.keywords)
        or_get = (isinstance(left, ast.Call)
                  and isinstance(left.func, ast.Attribute)
                  and left.func.attr == "get"
                  and len(left.args) == 1 and not left.keywords)
        if two_arg_get or or_get:
            hits.append((node.lineno, key))
    return hits


def main() -> int:
    offenders: list[str] = []
    for path in FILE_PATHS:
        for lineno, key in scan_file(path, KEYS):
            offenders.append(f"{path}:{lineno}: staleness key {key!r} written "
                             "as snapshot.get(K, measurement) — recompute at read "
                             "or keep the anchor on purpose (round 103 S2)")
    if offenders:
        print("staleness-detector: copy-paste family VIOLATED:")
        for line in offenders:
            print("  " + line)
        return 1
    print(f"staleness-detector: clean for its narrow family "
          f"(keys={sorted(KEYS)}, files={list(FILE_PATHS)}); "
          "semantic instances live in behavioural tests, not here")
    return 0


if __name__ == "__main__":
    sys.exit(main())
