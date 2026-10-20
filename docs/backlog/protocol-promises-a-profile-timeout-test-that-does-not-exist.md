---
title: PROTOCOL §7c promises "a test fails the suite if any profile does not" exceed MAX_READ_WAIT_SECONDS — no such test exists
found: 2026-10-19T23:31Z
severity: low
round: 126
status: fixed
fixed: 2026-10-20
fixed_by: round 129
---

## Where

- `docs/PROTOCOL.md:475-476` — the promise
- `mcp/pair_mailbox/read_wait.py:9` — `MAX_READ_WAIT_SECONDS = 60`
- `mcp/pair_mailbox/read_wait.py:16-46` — `client_timeout_ms`
- `mcp/pair_mailbox/test_read_wait.py:16-19` — the only timeout assertion
- `install_test.py:620-690` — the profile tests that exist

## What breaks

The protocol states:

> The MCP `timeout` in `cli-profiles.json` must therefore exceed
> `MAX_READ_WAIT_SECONDS`, **and a test fails the suite if any profile does
> not**.

No test enforces the second half. The only file that reads
`cli-profiles.json` for a timeout is `test_read_wait.py`, and it checks exactly
one profile against a hard-coded number:

```python
# mcp/pair_mailbox/test_read_wait.py:16-19
def test_omp_profile_allows_full_protocol_wait(self) -> None:
    timeout = read_wait.client_timeout_ms("omp")
    self.assertEqual(65_000, timeout)
```

`install_test.py` reads the same file six times (`:620`, `:631`, `:640`, `:648`,
`:658`, `:683`) and checks `launch`, `--standalone`, `--auto`, `server_key` and
the config home — never `timeout`.

The state today is correct: all six profiles carry `timeout: 65000`
(`cli-profiles.json`, `mcp.entry.timeout`), and 65 s > 60 s. But the guarantee
is unguarded, and the failure mode is a silent one. `client_timeout_ms`
returns the conservative default of 20 000 ms for any role it does not find
(`read_wait.py:45`, asserted by `test_read_wait.py:28`), and
`effective_read_wait` then caps the wait at 20 s − 5 s = **15 s**
(`read_wait.py:49-54`, asserted by `test_read_wait.py:24`). So a seventh
profile added without a `timeout` — or with a smaller one — halves the pair's
maximum wait, with the suite staying green and the protocol saying it cannot
happen.

## What to do

Add the test the protocol already names, in `install_test.py` next to the other
profile assertions:

```python
def test_every_profile_timeout_exceeds_the_protocol_wait(self) -> None:
    profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
    for name, profile in profiles.items():
        if name.startswith("_"):
            continue
        timeout = ((profile.get("mcp") or {}).get("entry") or {}).get("timeout")
        with self.subTest(profile=name):
            self.assertIsInstance(timeout, int)
            self.assertGreater(timeout / 1000, MAX_READ_WAIT_SECONDS)
```

## Red-proof

The test is its own red-proof: lower one profile's `timeout` in
`cli-profiles.json` to `20000` and run `python3 install_test.py` — the new
assertion fails and names the profile. Restore `65000` and it is green.

The second half of the red-proof is the failure mode the card is about: with
a profile that has NO `timeout` key, `client_timeout_ms` returns the
conservative `20000` default (`read_wait.py:45`) and `effective_read_wait`
caps the wait at 15 s (`read_wait.py:49-54`) — so the test must also assert
that every profile carries an explicit `timeout`, not merely that the ones
present are large. A profile that omits the key is the case the protocol
promises cannot happen, and it is the case that halves the wait.

## Severity

low — nothing is broken today; the defect is an unguarded promise in the
contract document, which is the class the audit skill names explicitly
("a comment without a test is a finding").

## How it was fixed (round 129)

`TestProfileTimeoutBudget` in `install_test.py`, next to the other profile
assertions, checks both halves the card named:

- every profile carries an explicit `mcp.entry.timeout` — a missing key is the
  case that halves the wait, so it is a failure with its own message;
- that timeout exceeds `MAX_READ_WAIT_SECONDS`.

The constant is read from the real module (`read_wait.MAX_READ_WAIT_SECONDS`),
not copied into the test: a copy is a second number to keep in step, and the
protocol names the module's constant.

Red-proof, measured both ways:

- `agy`'s timeout lowered to `20000` → `AssertionError: 20.0 not greater than
  60 : profile agy allows 20.0s — the protocol waits 60s, so the client gives
  up first`;
- `agy`'s `timeout` key removed entirely → `AssertionError: unexpectedly None :
  profile agy declares no MCP timeout — the client falls back to the 20 s default
  and the wait is capped at 15 s`.

Restored, the test is green. `cli-profiles.json` carries no trace of either
mutation — all six profiles still declare `65000`, unchanged from before the
round.
