# RUNBOOK: bringing up a working pair session

> Language: this is the English original. The Russian mirror is
> [`RUNBOOK.ru.md`](./RUNBOOK.ru.md).

The short path from a clean machine to a pair where both agents are silent because
there is nothing left to say, not because something broke.

## One command

```bash
./finish.sh
```

`finish.sh` does everything: installs, fixes the configs, runs the tests, prunes dead
roster entries, starts the supervisor, and starts the pair. The steps below are here
so you know what it does and what to fix by hand.

Useful flags:

| flag | effect |
|---|---|
| `--task "..."` | record the task in the shared list before starting |
| `--autonomy observe\|semi\|auto` | how far the pair may go alone |
| `--duo` | two copies of one CLI instead of the standing pair (see below) |
| `--no-pair` | install and verify only, start no agents |
| `--no-prune` | leave the roster alone |

## The standing pair, and what `--duo` is for

By default `finish.sh` reads the standing arrangement and starts **the writer and
the critic on their own CLIs**:

```bash
bin/pairctl roles --writer opencode --critic agy --global   # set it once
./finish.sh "задача"                                         # honours it
```

`--duo` is for running two instances of the *same* CLI, which is occasionally useful
and is not the default, because `bin/pair-duo` hands write authority to
`<cli>-writer`:

```bash
./finish.sh --duo "задача"        # writer becomes opencode-writer, and it says so
bin/pair-duo claude "задача"      # the same thing, directly
```

Authority moves only through the human, so `--duo` states out loud that it is
overriding the standing arrangement. Nothing else in the system may do it.

## The step-by-step path, when you need it

### 1. Install and verify

```bash
./install.sh              # every CLI it finds
./install.sh opencode     # just one
./install.sh --check      # change nothing, only verify
```

The installer writes your configs. Before it:

```bash
cp ~/.config/opencode/opencode.jsonc{,.bak}     # a backstop, though it makes its own
```

What it does and does not do:

- **writes** an `mcp.pair` block into each CLI's config, with the role baked in;
- **copies** the skills into `~/.config/<cli>/skills/`;
- **appends** the pair block to `AGENTS.md` after the `<!-- pair-mailbox -->` marker,
  keeping whatever you wrote after the marker, and lays down a `.bak` before
  rewriting;
- **leaves alone** your own config keys, your other MCP servers, and your `deny`
  rules.

Check that nothing broke:

```bash
python3 install_test.py                        # 13 tests: installing causes no harm
python3 mcp/pair_mailbox/test_protocol.py      # the protocol's invariants
python3 bin/pairpilot_test.py                  # the supervisor's behaviour
```

### 2. Fix the standing arrangement

```bash
bin/pairctl roles --writer opencode --critic agy --global
bin/pairctl roles                                 # show it
bin/pairctl roster                                # who is registered
bin/pairctl roster --prune                        # drop the dead ones
```

`--prune` removes a role only when two independent facts agree: its process is dead
**and** its pane is either gone or belongs to another live role. One fact is not
enough — after a crash a live pane may be a shell you opened. It refuses to remove
whoever holds write authority.

### 3. Start the pair

```bash
./finish.sh                 # respects the standing arrangement
```

Or by hand, in two panes:

```bash
# writer pane
PAIR_ROLE=opencode PAIR_STATE=.pair opencode --standalone --auto
# critic pane
PAIR_ROLE=agy PAIR_STATE=.pair agy
```

### 4. Start the supervisor

```bash
bin/pairpilot &                  # in the background, it watches
bin/pairpilot --once             # one pass, to see what it would do
bin/pairpilot --launch agy=agy   # how to raise a role whose process died
```

How far the pair goes without you:

```bash
bin/pairctl autonomy observe   # only reports; the human is at the wheel
bin/pairctl autonomy semi      # default: works alone, closes on your word
bin/pairctl autonomy auto      # closes the round itself, given proof
```

Note that `observe` is not "gates off": proof is still required to close a round in
every mode. `observe` only means it will not restart agents and does not need your
word. See PROTOCOL §10b.

### 5. Work

```bash
bin/pairchat          # the conversation, live
bin/pairctl status    # state, including what blocks closing the round
bin/pairctl tasks     # the task list, and it regenerates .pair/tasks.md
bin/pairctl task --done 3              # close a task
bin/pairctl task --drop 3 --reason "covered by PR #5 instead"
bin/pairctl ok                       # say yes at the verify checkpoint
bin/pairctl ok --revoke              # take it back
```

A round cannot close while a task is open, so anything added mid-round cannot be
forgotten: either it gets done, or it gets dropped with a reason on the record.

---

## The rule that saves the most time

**After editing code in this repository, restart the agents.**

The MCP server holds modules in memory. A file change on disk does nothing while the
process lives. This is not theory: a server started at 09:43 held the 09:43 version of
`composer.py`, the fix landed at 12:04, and eleven consecutive delivery refusals were
explained by that difference.

Check whether the code is fresh:

```bash
ps -o pid,lstart,cmd -p $(pgrep -f 'server.py --role' | head -1)   # when the server started
git log -1 --format=%cd                                            # when the last commit landed
```

If the start time is older than the last change, the code is stale.

The workaround that gets suggested — `importlib.reload(composer)` on every call — do
not do this. It leaves two versions of the module in one process, because the earlier
imports still point at the first. Restarting is honest and predictable.

---

## Why you were being asked to confirm things

Two separate causes, both fixed, both worth knowing.

**The agent ran with a stale config that had no `--auto`.** Now
`~/.config/opencode/opencode.jsonc` carries a `permission` block, and the opencode
profile in `cli-profiles.json` has `launch: opencode --standalone --auto`. So
everything the protocol already requires without a human confirms itself, and §12,
system destruction, git history and the network are always refused.

**The supervisor's allowlist file never existed.** `.pair/permissions.json` was
absent, and `load_allow_patterns` turned the path to the missing file into a single
allow pattern. It matched no prompt, so the supervisor refused **every** permission —
which from the outside is indistinguishable from a peer that stopped helping. Missing
file now means the default list, and it is written to the transcript
(`pilot_default_allowlist`, `unblock_default_allowlist`) so you can see which rule
approved a command.

Check the deny rules:

```bash
python3 -c "
import re,pathlib
t=(pathlib.Path.home()/'.config/opencode/opencode.jsonc').read_text()
print('deny rules:', len(re.findall(r'\"deny\"', t)))"
```

If that prints 0, the install did not get that far and the agent will ask about
everything.

---

## When something looks wrong

| symptom | likely cause |
|---|---|
| prompts reach you for things the protocol allows | the allowlist file is missing or the CLI's `--auto` did not apply — check both above |
| the supervisor never confirms anything | `bin/pairpilot --once` and read `closing_blocked_by` in `pairctl status` |
| a dead role still sits in the roster | `bin/pairctl roster --prune` |
| the critic stopped being nudged | two dead roles shared its pane; fixed, and `--prune` prevents it |
| edits have no effect | stale MCP server — restart, see the rule above |
| `pair_read` returns empty after a wait | the client cut the wait; the answer says `wait_capped_by_client_timeout` |

`closing_blocked_by` in `pairctl status` names the reason a round cannot close right
now. Read it before spending a turn on a foregone refusal.

---

## Further reading

- [PROTOCOL.md](PROTOCOL.md) — the rules of pair work, §13 about the supervisor
- [HOW-IT-WORKS.md](HOW-IT-WORKS.md) — how the MCP server and the state work
- [SETUP.md](SETUP.md) — installation in detail
- `docs/backlog/` — confirmed defects with reproductions
- `.pair/tasks.md` — the session journal: what was asked, what was done, what is left