---
title: verify-map.tsv still names two test files that were split away in round 108, and bin/split_protocol_tests.py is a one-shot script left in bin/
found: 2026-10-19T23:51Z
severity: low
round: 126
status: open
---

## Where

- `scripts/verify-map.tsv:131` — `mcp/pair_mailbox/test_protocol_messaging.py	protocol`
- `scripts/verify-map.tsv:133` — `mcp/pair_mailbox/test_protocol_liveness.py	protocol`
- `bin/split_protocol_tests.py:38,50` — references to the same two files
- the round-108 split produced `test_protocol_delivery.py`,
  `test_protocol_mailbox.py`, `test_protocol_pins.py` instead

## What breaks

```
$ for f in mcp/pair_mailbox/test_protocol_messaging.py mcp/pair_mailbox/test_protocol_liveness.py; do
    test -f "$f" && echo "EXISTS $f" || echo "MISSING $f"; done
MISSING mcp/pair_mailbox/test_protocol_messaging.py
MISSING mcp/pair_mailbox/test_protocol_liveness.py
```

Both were reorganised in round 108 slice 3; the map rows were never cleaned.
`bin/split_protocol_tests.py` is the one-shot script that performed that split,
left in the executables directory, and it still refers to the old domain
files.

The blast radius today is small: `verify-changed.sh` selects suites from the
files `git diff` reports, and a file that does not exist never appears in a
diff, so the phantom rows are inert. They are a lie in a table that is supposed
to be the map from a changed file to its suite — and a reader auditing coverage
will count two suites that cannot run.

## What to do

- Delete the two rows from `scripts/verify-map.tsv`.
- Move `bin/split_protocol_tests.py` out of `bin/` (it is a one-shot migration
  script; `bin/` is for shipped executables — see the `bin/pairmcpdoctor` /
  `bin/pairmcprestart` card for the other executables with no gate coverage) or
  delete it, since round 108 is merged and the split is history.

## Red-proof

A test that asserts every path in `scripts/verify-map.tsv` exists. Before the
fix it fails on the two phantom rows.

## Severity

low — dead configuration and a dead script; nothing breaks today.
