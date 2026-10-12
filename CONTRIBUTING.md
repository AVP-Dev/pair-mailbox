# Contributing to pair-mailbox

Thank you for your interest in contributing to **pair-mailbox**!

pair-mailbox coordinates two independent autonomous AI coding agents on different CLIs under strict engineering invariants. Because our codebase is developed and verified by autonomous agent pairs, we maintain rigorous verification standards and architectural contracts.

---

## 🏛️ Core Principles & Invariants

Before proposing changes, please review [`docs/PROTOCOL.md`](docs/PROTOCOL.md) and [`docs/project-structure.md`](docs/project-structure.md). Every contribution must respect these invariants:

1. **Zero External Runtime Dependencies**:
   The core engine (`pair-mailbox`) runs entirely on Python 3 standard library (`3.8+`) and POSIX `tmux`. Do not introduce third-party package dependencies (`pip`) for runtime execution.

2. **Single Writer Protocol Invariant**:
   At any time, exactly one agent role holds write authority. Reviewers must propose changes via cryptographically sealed patches (`pair_propose` / `pair_patch_seal`).

3. **File Size Ratchet**:
   - Files are budgeted by size: `<= 300` lines is standard, `300–500` acceptable with single responsibility, `500–800` requires boundary review, `> 800` allowed only with explicit rationale in `scripts/file-size-baseline.tsv`.
   - Never expand existing exceptions without justification.
   - Run `./scripts/file-size-gate.sh` to check compliance.

4. **Comment Hygiene**:
   - Comments must describe **current state and architectural rationale ("why")**, with citations to measurements or backlog cards.
   - Comments must never recite past git history or progress (e.g., "was added", "fixed in round X", "теперь использует").
   - Verified automatically by `bash scripts/comment-lint.sh`.

5. **One Test File Per Source File**:
   - Keep tests side-by-side with source modules (`module.py` &rarr; `test_module.py` or beside it).
   - Never create third helper files outside established directories.

---

## 🛠️ Development Setup & Workflow

### 1. Prerequisites
- Python 3.8+
- tmux 3.0+
- git

### 2. Fork and Clone
```bash
git clone https://github.com/<your-username>/pair-mailbox.git
cd pair-mailbox
```

### 3. Verification Commands
Always verify your changes locally before submitting a pull request:

```bash
# Fast check for staged / modified files:
./scripts/verify-changed.sh

# Run file size ratchet:
./scripts/file-size-gate.sh

# Run comment hygiene lint:
bash scripts/comment-lint.sh

# Run security and secret leak scanner:
./scripts/security-scan.sh

# Run the complete project verification gate (sequential for live-tmux suites):
./scripts/verify-project.sh --jobs 1
```

---

## 🔌 Adding Support for a New CLI Agent

To add support for a new AI coding CLI:
1. Open `cli-profiles.json`.
2. Add an entry specifying:
   - `label`: Human-readable name.
   - `launch`: Shell command to launch the agent (e.g. `--auto` or standalone mode).
   - `mcp`: Path to the CLI's MCP configuration file, format (`json` or `toml`), and server entry.
   - `skills_dir`: Directory where agent skills are stored.
   - `instructions`: Path to global instructions file (e.g., `AGENTS.md` or `CLAUDE.md`).
3. Run `./install.sh <profile_name> --check` to test configuration parsing.
4. Run `python3 install_test.py` to ensure profile invariants hold.

---

## 📬 Submitting a Pull Request

1. **Branching**: Create a focused topic branch (`git checkout -b fix-xyz` or `feat-abc`). Never work directly on `master`.
2. **Commit Message**: Use descriptive commit titles describing **what changed and why** (e.g. `feat(pilot): park supervisor on done without open tasks`).
3. **Evidence**: Ensure all automated checks pass cleanly.
4. **Pull Request**: Open a PR against `master`. Our GitHub Actions CI will run `./scripts/verify-project.sh` across supported Python versions.
