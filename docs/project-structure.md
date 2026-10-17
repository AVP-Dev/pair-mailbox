# Project structure and file hygiene

> Language: this is the English original. The Russian mirror is [project-structure.ru.md](./project-structure.ru.md).

> **Why this document:** the project has nine files over 300 lines, and two over 3000
> (`server.py` 4119, `test_protocol.py` 4381 — measured with `wc -l`, ceilings in
> `scripts/file-size-baseline.tsv`). That crossed the threshold before the gate existed.
> This document and `scripts/file-size-gate.sh` record the debt honestly: it is named, frozen,
> and cannot grow. Bulk splitting is done by separate tasks, not on the side
> of someone else's task.
>
> **Source of the scale:** `agent-starter-kit`, `docs/project-structure.md` §2. The order is the same,
> the numbers are the same, but applied to our actual map, not copied.
## 1. Leading principle

A small file with a single responsibility, an explicit name, local implementation inside its owner.
Boundaries must be explainable without reading the whole codebase.

## 2. File sizes

| Size | Interpretation |
|---|---|
| up to 300 lines | normal target size |
| 300–500 | acceptable with a single responsibility |
| 500–800 | mandatory boundary review |
| over 800 | only with a recorded reason and a split plan |

A guideline, not a ban. An exception is described in `scripts/file-size-baseline.tsv`
with a reason, and the reason is mandatory: an exception nobody can explain
is just a number hiding a problem.

### 2.1 Ratchet instead of splitting

The gate does not demand rewriting `server.py` today. It demands that the file **stop growing**.

A baseline entry means: the file has been reviewed, it has one responsibility, and it is frozen
at that ceiling. It cannot grow by a single line without a new entry with a new reason.
It may shrink, and that is welcome.

The expiry rule is deliberately narrow: an entry expires only when the file **no longer
needs** the exception, i.e. it has gone under the hard 800-line threshold. Cap is a ceiling,
not an exact value, so a file may sit far below it for years, and the gate
does not care. Previously the check compared cap against current size directly,
and then any edit to `server.py` would have required a baseline edit — the test showed that immediately,
and the gate would have become a mandatory obstacle instead of a ratchet.

Check: `bash scripts/file-size-gate_test.sh`, 30 scenarios.

## 3. Our files over 300 lines

Numbers — from `scripts/file-size-baseline.tsv` (ceilings; actual — `wc -l`).
The last five rows are under the 300-line target and ungated: their numbers are
current sizes, not ceilings, and the gate does not watch them.

| File | Ceiling | Responsibility | Status |
|---|---|---|---|
| `mcp/pair_mailbox/test_protocol.py` | 4429 | protocol invariants | frozen, follows server.py |
| `mcp/pair_mailbox/server.py` | 4226 | whole protocol: helpers, `Store`, `PairSession`, MCP layer | frozen, plan below |
| `bin/pairpilot_test.py` | 2749 | supervisor tests | frozen |
| `bin/pairpilot` | 2072 | supervisor: panes, role launches, throttling | frozen |
| `mcp/pair_mailbox/composer.py` | 1465 | composer and permission menu | frozen |
| `mcp/pair_mailbox/pairctl_authority.py` | 462 | writing to round state | extracted from server.py (round 15) |
| `mcp/pair_mailbox/pairctl_read.py` | 351 | read-only + project overview | extracted from server.py (round 15) |
| `install.sh` | 572 | installer | frozen |
| `bin/pairchat` | 438 | CLI: thin over `pair_send` | frozen |
| `mcp/pair_mailbox/board_snapshot.py` | 321 | what the pair recorded about itself: phase, roles, tasks, log tail | dashboard, split by scale |
| `mcp/pair_mailbox/assets/board.js` | 328 | client: draws the snapshot, theme, pause. Requests no data | dashboard, split by scale |
| `mcp/pair_mailbox/board_render.py` | 187 | assembling one self-contained HTML page | dashboard, split by scale |
| `mcp/pair_mailbox/board_hosts.py` | 184 | liveness on this machine: panes, pids, pilot process | dashboard, split by scale |
| `bin/pairboard` | 177 | CLI: single pass or timer-driven rebuild | dashboard, split by scale |
The boundary between `board_hosts` and `board_snapshot` is not stylistic. The first answers
questions to the system and must degrade to "don't know"; the second reads what
the pair recorded, and must survive a corrupt file. Mixed in one file, they drag along
a test that needs both tmux and `/proc` for a green result.

## 4. Split plan

Round 15 extracted `pairctl` from `server.py` (the CLI depends on the server, not vice versa;
details in `scripts/file-size-baseline.tsv`). The remainder is `class PairSession`:
one responsibility per class is divided by tool groups (state, communication,
patches, tasks). The first next candidate named is the supervisor (`bin/pairpilot` —
1567 lines with no split plan, see baseline).

Splitting is done by **separate tasks**, each with a green gate. Not on the side.

## 5. Boundaries that must not be crossed silently

- `composer.py` is imported **lazily**, inside functions. That is not an optimization: it pulls in
  only `os` and `re`, while at top level it would intercept everything for everyone, including
  the `ctl` subcommand, which does not need the composer.
- `serve.sh` is the transport server launch: `exec` with `<role>` and `--state`
  (`mcp/pair_mailbox/serve.sh:10,28`). The name and path `mcp/pair_mailbox/server.py`
  must not change: `install.sh`, `finish.sh`, and the MCP configuration
  of all six roles depend on it (`cli-profiles.json`: opencode, agy, codex, claude, pi, omp).
- Tests run as `python3 mcp/pair_mailbox/test_protocol.py`, so the repository
  root must remain an importable path. The file must not be moved.

## 6. Verification

`./scripts/verify-project.sh` — the single gate, profile in `scripts/verification-profile.tsv`.
Gate layers: static → positive → integration → security; fast loop `--only static,positive`,
the full gate adds the installer and runs before round finalization.
Parallelism via the `--jobs N` flag (default 1). Rule: live-tmux suites
always run sequentially (`--jobs 1`), because they share one tmux server
and end-to-end pane numbering — details in backlog `live-tmux-parallel-flake`.

The profile contains only confirmed commands. A missing check is recorded
as `blocked`, not written as a line that is always green.
For a single commit — `scripts/verify-changed.sh` (static + only affected suites
per `scripts/verify-map.tsv`).

## 7. Refactoring

Considered when a file crosses several responsibilities, one module reaches
into another's internals, or size/imports make safe work impossible.

Bulk cleanup is not done on the side of a functional task. Each
split task is separate and passes the gate.
