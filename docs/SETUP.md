# Setup

[Русский](SETUP.ru.md) · [Quick start](../README.md) · [Daily operations](RUNBOOK.md)

## Dependencies

Install Python 3.8+, Git, tmux and a recent Bash. Agent CLIs must be installed and
authenticated separately. The engine does not provide model access or select models.

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv git tmux
```

macOS with Homebrew:

```bash
brew install python bash git tmux
export PATH="$(brew --prefix)/bin:$PATH"
```

Check the actual commands selected by your shell:

```bash
python3 --version
bash --version
git --version
tmux -V
```

Verification uses `wait -n`, requiring Bash 4.3+; Bash 5 is recommended.
The complete process/tmux test suite targets Linux. macOS launch support does not
mean every Linux-specific test runs on macOS.

The mailbox server uses the Python standard library. PyNaCl is optional for
Ed25519 role signatures and required for the complete test suite. For development,
use a virtual environment as described in [Contributing](../CONTRIBUTING.md).

## Install the selected CLI profiles

Keep the engine clone at a stable path. From that clone:

```bash
./install.sh --list
./install.sh codex agy
./bin/pairup --install
export PATH="$HOME/.local/bin:$PATH"
```

`--list` shows detection. Use the profile names you actually installed.
`./install.sh` with no profile names selects all detected CLIs.
The global `pairup` shim points back to this clone. Persist the PATH line in the
appropriate shell startup file; run the shim installation again if the clone moves.

Installation updates the pair MCP entry, global instructions, copied skills and
native CLI permission settings. Existing MCP/instruction files are backed up with
a `.bak` suffix before replacement. Other user text around the managed instruction
block is retained; ambiguous modified legacy blocks require manual review.

The installer verifies its MCP entry in temporary state and runs protocol checks.
To check existing configuration without rewriting it:

```bash
./install.sh codex agy --check
```

This may take longer than a handshake because it also runs tests. It does not
launch a model session. A standalone MCP entry normally exposes no pair tools.

## Choose independent work or an explicit pair

For independent work, run `codex`, `agy` or your other CLI normally in the target
project. No state initialization, writer assignment or peer is required.

For pairing, from the target project:

```bash
pairup --writer codex "Inspect the project and fix the retry bug"
```

The launcher creates `.pair` when needed, records the engine path, stores the task
and starts the supervisor and two CLI panes. An existing `anti-mock.json` is kept;
a new project receives the engine's ground-truth template.
The default launch uses an isolated Git worktree and shared project state in the
main checkout. Use `pairup --in-place` to work in the current checkout.
Only the writer is pinned. The critic is another available CLI; use
`pairup --swap agy` to replace it. Repeating `pairup` attaches to the session.

An installed MCP server, `.pair` directory or `PAIR_STATE`/`PAIR_ROLE` variables
alone do not enroll a CLI. Manually starting two bare CLIs does not create a pair.
Use `pairup` or `pair-duo`; native subagents report to their parent without
claiming separate pair roles. A paired critic retains its write boundary on MCP failure.

## Target project's trunk

The engine's trunk is `master`. For a target project using `main`, set its local
ignored policy once from that project's main checkout, before pairing:

```bash
mkdir -p .pair
printf '%s\n' main > .pair/trunk
```

Use the actual branch name. Precedence is `PAIR_TRUNK`, `.pair/trunk`, the
committed `.pair-trunk` policy, then `master`. A default worktree needs a Git
repository with an existing commit. `--in-place` opts out of worktree creation.

## Configuration ownership

[cli-profiles.json](../cli-profiles.json) defines MCP paths, instruction paths,
skill directories and launch commands. The installer owns only its pair entry
and marked instruction block; unrelated CLI configuration remains user-owned.
The default transport is a child server. The optional [socket transport](socket-transport-flip.md)
keeps a detached daemon between enrolled CLI restarts.

Codex pairing uses `--no-daemon` and forwards the launch variables through the
MCP entry's `env_vars`. The installed Codex must support that flag; check
`codex --help`. See the [official CLI source](https://github.com/openai/codex/blob/main/codex-rs/tui/src/cli.rs)
and [MCP configuration](https://github.com/openai/codex/blob/main/codex-rs/config/src/mcp_types.rs).

OpenCode pairing uses its profile's `--standalone --auto` flags. CLI-specific
permission prompts depend on the installed CLI version; inspect that CLI's MCP
and permission settings if calls still require confirmation. `auto` in pair-mailbox
cannot override the host CLI's own permission policy.

After an update, restart already open CLIs to reload their configuration and
instructions. A successful standalone initialization with zero tools is expected;
a successful enrolled initialization exposes `pair_*` tools. Do not rely on a fixed tool count.

## Maintenance and removal

Use `pairup` for normal launches. `finish.sh` is an engine maintenance entry point
that runs tests and reinstalls profiles; inspect `./finish.sh --help` before using it.

To uninstall an integration, remove its pair MCP entry, its managed instruction
block and the installed pair skills from the selected CLI configuration. Preserve
other settings and skills. `.pair` contains tasks, messages, patches and evidence;
back up anything needed before removing runtime state. Do not delete whole agent
configuration directories as an uninstall shortcut.
