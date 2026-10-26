---
title: bin/pairmcpdoctor ships with no tests, no gate row, and a second convention for the same blind sleep
found: 2026-10-26T23:20Z
severity: medium
round: 152
status: open
---

## Where

- `bin/pairmcpdoctor` — the doctor over the room
- `bin/pairmcpdoctor:153,161,169` — the three blind waits inside `reconnect()`:
  `time.sleep(6)`, `time.sleep(2)` and `time.sleep(3)` in a loop
- `bin/pairmcprestart:45-47` — the three named ceilings the sibling got
- `bin/pairmcprestart:152,165,178` — its three `wait_for_condition` calls
- `mcp/pair_mailbox/wait_until.py` — the helper round 151 introduced
- `scripts/verification-profile.tsv` — no row for `pairmcpdoctor`
- `scripts/verify-map.tsv` — no mapping for `bin/pairmcpdoctor`

## What breaks

**Two things, and they compound.**

### 1. The doctor is a shipped operator of the transport layer with no safety net

`bin/pairmcpdoctor` reaches into a live CLI's `/mcp` picker and toggles a
server. `bin/pairmcprestart` does the same thing and, since round 151, has
`bin/pairmcprestart_test.py`, a profile row (`mcpprestart`) and a map row. The
doctor has neither.

```
$ git ls-files bin/ | grep -E "mcp.*test"
bin/pairmcprestart_test.py
```

The convention in `bin/` is otherwise unbroken: `pairboard`, `pairdoctor`,
`pairpilot`, `pair-duo`, `pairup`, `pairrestart` all have a `*_test.py`
sibling. The doctor is the exception, and it is the one with the most reach —
it runs over EVERY role, not one.

### 2. It still sleeps blind — and it is now the SECOND convention for the same duty

Round 151 introduced `mcp/pair_mailbox/wait_until.py` and rewrote
`bin/pairmcprestart` onto it, with three named ceilings:

```python
PICKER_TIMEOUT = 15.0     # the /mcp picker must draw
FILTER_TIMEOUT = 10.0     # typing "pair" must narrow the list
HANDSHAKE_TIMEOUT = 30.0  # the server process must appear
```

The doctor's `reconnect()` still does what the sibling used to do:

```python
send(pane, MENU, enter=True)
time.sleep(6)      # the picker — blind
send(pane, "pair")
time.sleep(2)      # the filter — blind
if pair_row_state(pane) == "connected":
    return "already-connected"
send(pane, "space")
for _ in range(10):
    time.sleep(3)  # the handshake — blind, up to 30s
    if server_pid(role):
        return "reconnected"
```

The same defect, the same cure, one file over — and the cure is already
written and tested. The doctor's blind waits are worse than the sibling's were:
it runs over EVERY role in the room, so every role pays 8 seconds of dead
waiting even when its picker is up in 300ms, and 38 seconds when the handshake
is slow.

### 3. The doctor's own detection logic is weaker than the sibling's

`pairmcpdoctor.pair_row_state()` classifies the picker row from a plain
`capture-pane -p` read — the ROWS, not the logical lines. Its own docstring
lists `connected`, `failed`, `disabled`, `present`, `absent`, but the actual
classifier is:

```python
if "connected" in joined and "not connected" not in joined and "not " not in joined:
    return "connected"
```

Note the third clause: `"not " not in joined`. A row reading
`pair — Connected` is fine, but a row reading `pair — Not connected` is caught
by the second clause, and ANY other row containing "not " (a path, a
description, a server name) falls through to `failed`/`present`. The logic is
a pile of negations rather than a classifier. The sibling round 151 left has
the same shape but a narrower job — it only asks "is it connected", not four
ways.

And the doctor does NOT use the round-151 `-J` capture at all, so on a narrow
pane it reads the same fragmented rows the round-151 defect was about: a
`Connected` marker that wrapped onto the second row reads as `absent`, and the
doctor then types into a pane it thinks is not showing a picker.

## What to do

- **Reuse the sibling's waits, not a new one.** `bin/pairmcpdoctor` must call
  `mcp/pair_mailbox/wait_until.wait_for_condition` with the SAME three named
  ceilings. The doctor's `reconnect()` becomes: wait for the picker's chrome
  (the sibling's `_picker_open`), wait for the filtered pair row
  (`_pair_row_visible`), then wait for the process to appear. One convention,
  one helper, two callers.
- **Share the detection, do not fork it.** The picker predicates and the
  connected/failed/disabled classifier are the SAME knowledge the sibling
  already has and round 151 already sharpened. Either move them into a shared
  module under `mcp/pair_mailbox/` (with one test file beside it, per the
  repo's rule) or make the doctor import the sibling's. Two copies of a
  picker classifier is how the two drifted apart in the first place.
- **Use the `-J` capture.** The doctor must read the LOGICAL line, or it keeps
  the round-151 fragmentation defect on the one tool that acts on what it
  reads.
- **Write `bin/pairmcpdoctor_test.py`**, following `bin/pairmcprestart_test.py`:
  a stub CLI in a real tmux pane, a temp state dir, assertions on the
  observable outcome. The permanent coverage is the test file; the live stub
  proves it once.
- **Register both in the gate**: a `mcpdoctor` row in
  `scripts/verification-profile.tsv` and rows for `bin/pairmcpdoctor` and
  `bin/pairmcpdoctor_test.py` in `scripts/verify-map.tsv`.

## Red-proof

- A test that asserts `bin/pairmcpdoctor` contains no fixed `time.sleep` in
  its wait path — the same assertion `bin/pairmcprestart_test.py` makes, and
  it fails against the current file.
- A test that the doctor does not type into a pane whose picker has not drawn:
  with a stub pane that never opens the picker, the doctor must fail with a
  clear message after the ceiling, not type into the CLI's prompt.
- A test that a `Connected` marker which WRAPS past the pane width is still
  read as connected (the `-J` path), and that the doctor leaves a live server
  strictly alone (the read-before-write rule — the mistake that cost a
  working context7 its state).
- A test that every executable in `bin/` has either a `*_test.py` sibling or
  an explicit exemption recorded in the verification profile. Before the fix,
  `pairmcpdoctor` fails it.

## Severity

medium — the doctor is the tool that runs unattended over every role, it holds
a real per-role lock and can toggle a live server, and it has neither a test
nor the cure its sibling received in the very same round. The failure mode is
not a false pass: it is a doctor that waits too long, types into a pane that
is not ready, and misreads a wrapped row.
