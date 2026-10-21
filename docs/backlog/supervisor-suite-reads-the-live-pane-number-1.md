---
title: The supervisor suite hardcodes pane %1 and reads the live tmux server — the required gate turns red on a machine with more than one project
found: 2026-10-21
severity: medium
round: 134
status: open
---

## Where

- `bin/pairpilot_test.py:2933` — `class TestPushGates`
- `bin/pairpilot_test.py:2947` — `_pilot_with_tmux`: it stubs `pairpilot.pane_role_pid`
  and sets `pilot.tmux = "/usr/bin/tmux"`, but leaves `pane_alive`,
  `pane_belongs_to_project` and the real pane itself alone
- `bin/pairpilot_test.py:2971`, `3140`, `3158`, `3187` — the four cases, each hardcoding
  `pane: "%1"`
- `bin/pairpilot:1247` — the ownership check the pilot runs before it types
- `scripts/verification-profile.tsv` — the `supervisor` row
  (`python3 bin/pairpilot_test.py`, required layer)

## What breaks

Most of the suite never touches a live window — `PilotCase.pilot()` sets
`pilot.tmux = None`. `TestPushGates` deliberately hands the pilot a real tmux binary
to exercise the push gates, and the pane it names is `%1`: a hardcoded, machine-global
id.

On this machine `%1` is a live pane of a **different project**:

```
$ tmux list-panes -a -F "#{pane_id} #{pane_current_path}"
%0   /mnt/storage/work/nyx-system
%1   /mnt/storage/work/nyx-system
%100 /mnt/storage/work/nyx-system
%2   /mnt/storage/work/umputum
%3   /mnt/storage/work/umputum
```

The pilot then does the right thing: it will not type into a pane that belongs to
another project. It records `pilot_pane_foreign` ("pane belongs to another project:
/mnt/storage/work/nyx-system"), drops the queued push, and the four cases that assert
the push *was* queued fail:

```
FAIL: test_live_cli_in_the_pane_gets_the_text
FAIL: test_unread_mail_wakes_a_live_peer_without_an_ask_file
FAIL: test_a_delivered_mail_wake_does_not_spend_the_human_trigger
FAIL: test_a_delivered_report_does_spend_the_human_trigger
Ran 169 tests in 7.3s
FAILED (failures=4)
```

The required gate turns red, and the red says nothing about the change under test:

```
$ ./scripts/verify-project.sh
[verify] supervisor: failed (exit 1, required)
[verify] failed: 1 required check(s), 0 warning(s)
```

Measured on round 134, same tree, same code, one hour apart: the full gate was green
at 07:56 and red at 08:0x. The only thing that changed in the world was that the
other project opened a window and took the id `%1`.

The suite is green when the live server has no `%1` at all — the pane is then
"unproven", the pilot keeps the queued push, and the assertions hold:

```
$ env -u TMUX -u TMUX_PANE TMUX_TMPDIR=/tmp/tmuxempty python3 bin/pairpilot_test.py
Ran 169 tests in 4.5s
OK
```

Same class, one level over: `.pair/notes/finding-flake.md` records `TestRestart`
reading the live pane `%7`, and the fix there was to add the missing stub.

## What to do

- stub the facts the pilot still reads live in `TestPushGates` —
  `pairpilot.pane_alive` and `pairpilot.pane_belongs_to_project` — exactly as the
  neighbouring cases already do (`bin/pairpilot_test.py:3175-3176` and `3202-3203`),
  and give the pilot a pane id that cannot exist so the real server is never asked;
- the class docstring already promises this: "The stub answers the two facts the
  pilot needs and nothing else, so the test cannot pass by touching a real window"
  (`bin/pairpilot_test.py:2951-2953`). It answers one of the two;
- the suite should also be runnable with the live server out of reach (the CI runner
  has no tmux session at all, which is why CI is green and a developer's machine is
  not). A gate that is green only on CI is a gate that lies on one of the two.

## Red-proof

- with a live foreign `%1` present, the four `TestPushGates` cases pass;
- the suite never resolves a real pane: a test that points the pilot at a real pane id
  of another project and asserts the suite still passes;
- `./scripts/verify-project.sh` is green with a foreign `%1` on the machine — the
  property the gate has never had.

## Severity

medium — a required check turns red for a reason unrelated to the change, and a red
gate that is not about the change is how people learn to ignore red.
