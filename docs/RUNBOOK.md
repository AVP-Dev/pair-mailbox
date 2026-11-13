# Daily operations

[Русский](RUNBOOK.ru.md) · [Setup](SETUP.md) · [Protocol](PROTOCOL.md)

## Paths and launch modes

Examples below use this engine path; change it if your clone lives elsewhere:

```bash
PAIR_HOME="$HOME/pair-mailbox"
cd "$HOME/work/my-project"
```

`PAIR_HOME` is a shell convenience, not a configuration knob. The current directory
is the target project. Only `pairup` is installed on `PATH`; other tools retain
their engine paths. Use their `--help` for supported options.

| Need | Command from the target project |
|---|---|
| Independent agent | `codex` or `agy` |
| Pair with a task | `pairup --writer codex "Fix retry handling"` |
| Attach to an existing pair | `pairup` |
| Replace the critic | `pairup --swap agy` |
| Name the default worktree branch | `pairup --worktree feature/retry "Fix retry handling"` |
| Two copies of one CLI | `"$PAIR_HOME/bin/pair-duo" codex "Fix retry handling"` |

Run `"$PAIR_HOME/bin/pairup" --install` once if the shim is missing, then add
`$HOME/.local/bin` to PATH. Run it again after moving the engine clone.

A fresh pair creates `.pair`, records the engine, copies a missing `anti-mock.json`
and chooses available profiles. An existing writer policy wins over automatic
selection. A new explicit `--state` directory must end in `.pair`; existing custom
state directories are supported. `--state` and worktree options are shown by
`pairup --help`.

The default launch creates/reuses an isolated worktree. State remains in the
main checkout. Use `pairup --in-place` to work in the current checkout. Mutable
dependency/build/cache directory sharing is opt-in through `worktree-share`; see
[project settings](PROTOCOL.md).

## Status, messages and board

```bash
"$PAIR_HOME/bin/pairctl" status
"$PAIR_HOME/bin/pairctl" tasks
"$PAIR_HOME/bin/pairctl" budget
"$PAIR_HOME/bin/pairctl" all
"$PAIR_HOME/bin/pairchat" --once
"$PAIR_HOME/bin/pairchat" --all --once
"$PAIR_HOME/bin/pairboard"
"$PAIR_HOME/bin/pairboard" --print-json
```

`pairchat --all --once` prints one snapshot across known projects; omit `--once`
for a stream. `pairboard` writes `board.html` in the current directory; it does not
start a web server. Open that file with `open board.html` on macOS or
`xdg-open board.html` on a Linux desktop. The board contains recorded state,
not independent proof that a task or test succeeded.

The pair has a project-specific tmux socket. Prefer `pairup` to reconnect; an
unqualified `tmux attach` can connect to another server. Detach from tmux with
Ctrl+B, then D; the agents continue running.

## Tasks and authority

```bash
"$PAIR_HOME/bin/pairctl" task "Also check exponential backoff"
"$PAIR_HOME/bin/pairctl" why
"$PAIR_HOME/bin/pairctl" roles
```

`task` adds work. Agents finish it through the shared task list. In a paired
session, `pair_state`, `pair_claim`, `pair_send` and related names are MCP tools,
not shell executables. Native helpers report to their parent.

To set a preferred writer for future starts:

```bash
"$PAIR_HOME/bin/pairctl" roles --writer codex --global
```

For a deliberate live transfer, the human can use `pairctl release` followed by
`pairctl assign ROLE`. A holder can also release its own authority through the
MCP tool `pair_release`; a peer message alone transfers nothing. Only the writer
edits source files. Losing MCP never promotes a critic to writer.

## Autonomy and round purpose

```bash
"$PAIR_HOME/bin/pairctl" autonomy auto
"$PAIR_HOME/bin/pairctl" mode audit
```

`auto` permits supervisor actions within its allowlist; `semi` also needs the
human's `pairctl ok` at the checkpoint; `observe` disables automatic restarts and
permission confirmation. Evidence requirements still apply. `mode fix` allows
implementation by the writer; `mode audit` makes both roles read-only.
Changing the mode affects the current round, so tell the agents its purpose.

To choose autonomy at launch, put it on the first task line:

```bash
pairup --task $'autonomy: semi
Inspect retry handling'
```

`pairup` has no `--autonomy` flag. Launch resolution uses the task directive,
then `.pair/autonomy`, then `auto`. `pair-duo` also accepts `--autonomy`.
An already running CLI can still ask for approval under its own permission policy.

## Diagnose startup and MCP problems

```bash
"$PAIR_HOME/bin/pairdoctor"
"$PAIR_HOME/bin/pairmcpdoctor" --role codex
"$PAIR_HOME/bin/pairrelaunch" --role codex --check
"$PAIR_HOME/bin/pairrestart" --role codex --dry-run
```

Start with report/plan modes. `pairrestart` refreshes stale role servers;
`pairrelaunch` without `--check` restarts the role's CLI and loses that CLI's
in-memory session. See [socket transport](socket-transport-flip.md) for daemon
lifetime and reconnect limits.

If a normal CLI declares itself critic, restart it to load current instructions
and check the integration from the engine clone with `./install.sh codex --check`.
An independent MCP entry returning zero tools is expected. If an enrolled CLI has
zero tools, check its launch command, process ancestry, current engine path and
Codex `env_vars` forwarding. Do not simulate pairing by exporting old `PAIR_*` values.

If `pairup` opens two panes but no work starts, ensure a task was supplied. An
empty task list tells the agent to report ready. Codex and agy receive a start
prompt; other profiles depend on their host's instruction handling.

## Non-tmux runner (OpenCode only)

The repository also includes an explicit subprocess runner. Preview its generated
MCP configuration without starting a model:

```bash
"$PAIR_HOME/bin/pair-runner" --role opencode --state "$PWD/.pair" --print-config
```

Use `--message "Task"` instead of `--print-config` to start its OpenCode subprocess.
Other CLI profiles are refused. There is no tmux pane: messages reach the mailbox,
but the runner does not promise an active wake-up or a general worker pool.
See `"$PAIR_HOME/bin/pair-runner" --help` before use.

## Maintenance and cleanup

`finish.sh` runs tests and reinstalls engine integrations. It is not the daily
project launcher. Use `./finish.sh --no-pair` from the engine clone for maintenance
without launching model sessions; see `./finish.sh --help` for the full path.

Do not start a second supervisor for a project that already has one. `pairup`
starts one when missing. Before removing runtime state, stop the associated CLI
and supervisor processes and save required messages, patches and evidence.
`pairctl reset --yes` replaces runtime state; it is not a routine startup command.

Keep `master` current and remove temporary branches only after their work is
merged or otherwise retained. The [backlog](backlog/README.md), round records and
archive carry project history; their presence is not a reason to delete them.
