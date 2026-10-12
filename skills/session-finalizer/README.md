# session-finalizer

> Language: this is the English original. The Russian mirror is [README.ru.md](./README.ru.md).

A pointer for the session-finalization skill. Contracts live in `SKILL.md`,
the scripts, and the tests — not here; this file does not duplicate them.

## How to invoke

- User: `/finish`, `/finish --check`, `/finish --cleanup`, `/finish --yes`.
  There is no `--force` flag. Details — `SKILL.md`, sections 1–2.
- Not to be confused with `./finish.sh` at the checkout root: that is a shell launcher for the pair session,
  it has nothing to do with the close protocol (`SKILL.md`, disclaimer in the header).

## Where things live

All paths from the `umputum` checkout root:

| Path | What it is |
|---|---|
| `skills/session-finalizer/SKILL.md` | Contract: triggers, modes, protocol, safety, idempotency, report, scripts, pair-mailbox |
| `skills/session-finalizer/scripts/finalize-check` | Repo/session state audit: `cwd=<repo>`, no arguments, stdout = check.json. Does not write to git/marker/worktrees/skill files; executes detected project commands for real (see `SKILL.md` §2) |
| `skills/session-finalizer/scripts/finalize-plan` | Pure function: `finalize-plan <check.json>`, stdout = plan.json |
| `skills/session-finalizer/scripts/finalize-apply` | The only writer: `finalize-apply <plan.json> [--yes]`, stdout = apply.json |
| `skills/session-finalizer/scripts/finalize-report` | Report and marker: `finalize-report <check.json> <apply.json> [plan.json]` |
| `skills/session-finalizer/lib/git.sh` | Read-only git helpers (sourced, do not execute) |
| `skills/session-finalizer/lib/json.sh` | JSON one-liners on python3 stdlib (no jq) |
| `skills/session-finalizer/adapters/agent.sh` | Agent registry, session artifacts, close order (`list\|artifacts\|close\|order`) |
| `skills/session-finalizer/adapters/hosting.sh` | Base-branch and PR proof via `gh`, then `glab`, else UNKNOWN |
| `skills/session-finalizer/adapters/commands.sh` | `test/lint/build/typecheck` command detection (`detect <repo>`) |
| `skills/session-finalizer/config/defaults.json` | Config: marker, protected branches, temp patterns, check slots, close_order |
| `skills/session-finalizer/tests/run.sh` | Suite runner; `tests/test_*.sh` — cases, `tests/lib.sh` — shared helpers, `tests/fixtures/` — fixtures |

## Verification

```sh
skills/session-finalizer/tests/run.sh
```

Green run: all tests green (fact — in the `tests/run.sh` runner output;
the count grows with the suite, so it is not pinned here). The runner guards the working copy: `git status` and `HEAD`

## How to plug in an agent / provider / command

No core changes needed:

- New agent: add a key to `cli-profiles.json` (top level, no
  `_` prefix) or to `agents_config_overrides` in `config/defaults.json`.
  Unknown names are preserved automatically (PRESERVE).
- New hosting provider: extend `adapters/hosting.sh` (contract:
  missing/failing provider = UNKNOWN, never success).
- New check command: extend the detect matrix in `adapters/commands.sh`;
  slots are listed in `checks_slots` in `config/defaults.json`.
- Role close order: `close_order` in `config/defaults.json`.

## Install convention

`install.sh` (`write_skills`) ships only `SKILL.md` to profiles — one
file per skill. Scripts, adapters, config, and tests live in the checkout and
run from there; the orchestrator calls them via the table paths above.
Widening the install.sh fan is a deliberate "no" (architectural decision).

## Assumptions

Closed by architect decision:

- UNKNOWN = PRESERVE; merged proof only via git; protected branches
  never touched; `--check` three-level honest (grep-gate with no write verbs in code +
  tree_sha-guard for tree cleanliness in `tests/run.sh`/`test_check.sh` + project commands only
  from the `adapters/commands.sh` detect matrix); `--yes` does not pierce PRESERVE;
  `--force` does not exist; only `finalize-report` writes the marker (on success) — not `apply`.

Still open:

- Project-command detection covers `package.json` / `Makefile` / python /
  `go.mod` / `Taskfile`; exotic build systems yield UNKNOWN slots,
  the orchestrator runs their checks manually.
- Hosters other than `gh`/`glab` yield UNKNOWN — PR proof is manual then.
- Mail drain and write-right transfer are the orchestrator's manual work via
  pair tools (`pair_read`, `pairctl`); the `close` adapter only records
  a note.
