---
name: session-finalizer
description: Finalize a coding session via /finish — close the protocol loop implement→review→finalize with verified cleanup and a final report. Use when the user says "/finish", "finalize session", or "close protocol implement→review→finalize".
---

# Session Finalizer

> Language: this is the English original. The Russian mirror is [SKILL.ru.md](./SKILL.ru.md).

> English text is normative; on EN/RU divergence follow EN.

You close the session, you don't "tidy up". Prove the repo is clean, then
emit the final report. No proof — no close.

**Orchestrator** — the holder of the `/finish` invocation, the one who executes
the protocol in this chat. Not a registry role but a function: performs steps 1–7,
reads the mail before closing, sets the marker via scripts.

**Disclaimer about `./finish.sh`.** At the checkout root of `umputum` lies an
executable `./finish.sh` — a shell launcher for the pair session (installs skills,
raises the pair and the supervisor). That is something else: it knows nothing of the
check→plan→apply→report protocol and does not set the marker. The `/finish` command
below is this skill's protocol; `./finish.sh` does not replace it.

## 1. Triggers

- `/finish` — full cycle: check → plan → confirm → cleanup → final-verify → close.
- `/finish --check` — audit only. Read-only on repo/session state: does not write to git, marker, worktrees, skill files. BUT executes detected project commands (see §2): they run for real and may write their normal artifacts.
- `/finish --cleanup` — audit + plan + apply (cleanup without final close).
- `/finish --yes` — skips **only** the cleanup-plan confirmation. Safety checks always run.
- There is **no** `--force` flag. Do not invent it, do not accept it from the user.

## 2. Modes

| Invocation | What it does |
|---|---|
| `/finish --check` | Audits repo/session state. Does not write to git, `.session/`, `.pair/`, marker, worktrees, skill files. Detected project commands (`test/lint/build/typecheck` from `adapters/commands.sh`: `npm`/`make`/`pytest`/`go`/`task`, no install/publish) execute for real (`bash -c`, timeout 120) — their normal writes are recorded in `log_tail`, not a read-only violation. |
| `/finish --cleanup` | Audit + plan + plan apply. No final-verify and no close (marker not set). |
| `/finish` | Full cycle: check → plan → confirm → cleanup → final-verify → close. |
| `/finish --yes` | Skips **only** plan confirmation. Does not lift BLOCKED, does not cancel safety rules. |

## 3. Protocol

Strict order, 7 steps:

1. **Verification.** Run `finalize-check` (cwd = repo root). Record HEAD, branch, marker.
2. **Cleanup.** `finalize-plan` (pure function of check.json) → show the plan to the human → `finalize-apply` (without `--yes` this is a dry-run: deletes nothing).
3. **Final verify.** Repeat `finalize-check` on the new HEAD. Clean — proceed. Dirty — new plan, not a close.
4. **Reviewer report.** Reviewer verdict: `OK` or `BLOCKED` with a reason and `file:line`.
5. **Implementer report.** What was done: commits, files, what remains.
6. **Orchestrator cleanup.** Temp repos, scratch directories, background processes — stop and delete, each with output confirmation.
7. **Stop.** `finalize-report` on a successful apply with a clean tree writes the `.session/finalized` marker and prints the report. First line is `SESSION FINALIZED` or `SESSION BLOCKED`.

Invariants: reviewer-BLOCKED stops cleanup and close (only the safe part — documentation); single-writer — one holder at a time touches mail and marker; closing over unread mail is forbidden.

## 4. Safety Rules

1. Did not recognize a state — preserve it. **UNKNOWN = PRESERVE.** Mark `?`, move on.
2. Proof of merged is only via git (`git branch --merged`, `git log`, PR status via `gh`/`glab`). No proof in git — treat as unmerged, preserve.
3. Never push to protected branches (`main`, `master`, `develop`, `trunk`, `release/*`, `hotfix/*`). Never force-push. Do not rewrite someone else's history.
4. Do not touch secrets in the repo (`*secret*`, `*.pem`, `*.key`, `.env`, `*credential*` from `secrets_guards`). Do not commit secrets. Outside the repo (`~/.config`, `~/.ssh`, the user's home data) is out of this skill's scope: scripts only work with repo-relative paths and do not go there.
5. Uncommitted work and a named stash are work, not trash: only with explicit human confirmation.
6. `--check` does not write to git/marker/worktrees/skill files (but runs project commands — see §2). `--yes` does not override rules 1–5 and does not lift BLOCKED.
7. Every deletion comes with proof: what, where, why it is safe, command output / exit code.

## 5. Idempotency
The `.session/finalized` marker is JSON `{head, session, closed_at}`; only
`finalize-report` writes it, and only on success (apply not blocked AND tree clean).

- Marker exists and `marker.head == HEAD` — nothing to do: answer `Nothing to clean`, do not touch the repo.
- HEAD differs — new work after closing: full cycle again, marker overwritten after a successful final-verify.
- `--check` ignores marker presence: the marker is not permission, the audit is always full.

## 6. Final Report

First line is always one of two: `SESSION FINALIZED` | `SESSION BLOCKED`.

```text
SESSION FINALIZED | SESSION BLOCKED

Task:          what had to be closed
Implementation: what was done (commits, files)
Git:           branch, HEAD, push/PR status
Issues&PR:     open/closed issues and PRs with links
Cleanup:       what was deleted, with proof per item
Final state:   final-verify output (git status clean)
Agents:        who took part and with what verdict

STATUS: COMPLETE | BLOCKED
```

With BLOCKED, instead of `STATUS: COMPLETE`:

```text
STATUS: BLOCKED
Reason:                why it cannot be closed (with file:line)
Cleanup performed:     what was safely done
Cleanup NOT performed: what was NOT done and why
Action required:       what the human must do
```

Line marks: `✓` — confirmed by command output/exit code (did not see output — do not mark); `!` — PRESERVE decisions and problems; `?` — UNKNOWN. A report without Cleanup and Final state sections is not a report.

## 7. Running the scripts

CLI is positional, no flags for inputs. `cwd` of each call is the target repo root. The orchestrator runs the scripts from the skill checkout (paths below relative to the `umputum` root):

```sh
skills/session-finalizer/scripts/finalize-check > check.json
skills/session-finalizer/scripts/finalize-plan check.json > plan.json
skills/session-finalizer/scripts/finalize-apply plan.json         # dry-run: deletes nothing
skills/session-finalizer/scripts/finalize-apply plan.json --yes  # real apply
skills/session-finalizer/scripts/finalize-report check.json apply.json [plan.json]
```

Each script answers `--help`. Exit codes: `0` — ok (including `blocked:true`), `2` — bad invocation/input, `1` — internal error. Only `finalize-apply --yes` deletes (and only SAFE after re-checking); only `finalize-report` creates the marker file. `install.sh` ships only `SKILL.md` to profiles (`write_skills` copies one file per skill) — scripts live in the checkout and run from there; not widening the install.sh fan is a deliberate "no" (architectural decision).

## 8. Pair-mailbox integration

Role close order is `close_order` from `config/defaults.json` (`critic, explorer, writer`). The role registry (fact of `adapters/agent.sh`): `cli-profiles.json` → `agents_config_overrides` from `config/defaults.json` → `.session/finalizer.json` (`session_config` in config, key `agents`). A name outside all three is unknown: its files are PRESERVE, the close is marked accordingly. Code does not read `state.roles` — the registry is built from files, not from live state.

- Single-writer: one holder of the right writes at a time; the right moves only via a human through `pairctl`, never by message.
- Read the mail before close: drain the mailbox first (`pair_read`), then close. BLOCKED from any role stops the close.
- The `adapters/agent.sh close <role>` adapter is a no-op note (writes nothing, the orchestrator confirms via pair tools); an unknown role exits 3, PRESERVE.
