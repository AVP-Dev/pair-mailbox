# MCP child and socket transports

[Setup](SETUP.md) · [Runbook](RUNBOOK.md)

The global pair MCP entry has two transport forms. Both are inactive outside
explicitly enrolled pairs: standalone initialization succeeds with zero tools
and does not lock a role or create project state.

| Transport | MCP entry | Lifetime in an enrolled pair |
|---|---|---|
| Child (default) | `sh /path/to/engine/mcp/pair_mailbox/serve.sh codex` | server follows the CLI connection |
| Socket | `python3 /path/to/engine/mcp/pair_mailbox/mcp_socket_client.py codex` | thin client connects to a detached role daemon |

`/path/to/engine` represents the engine clone, not the target project. Prefer the
installer over hand-written global entries. A global entry must not pin an old
project with `--state`.

## Select socket transport

From the engine clone:

```bash
PAIR_TRANSPORT=socket ./install.sh codex agy
```

The installer writes the selected profiles and verifies each in temporary state
with a temporary daemon. It does not enroll the current ordinary CLI or attach
the verification to a live project's socket.

Restart existing CLIs to reload their MCP entry. Then, from the target project:

```bash
pairup --writer codex "Inspect retry handling"
```

The enrolled client reuses or starts a detached daemon for its configured role,
at `.pair/.server-ROLE.sock`. Either role can start its daemon. The actual socket
transport is recorded in project state for restart tooling. The daemon keeps its
role lock until it stops; ending its thin client does not end the daemon.

Codex requires the launch environment forwarding installed in `env_vars` and
uses its profile's `--no-daemon` flag. Other raw CLI launches remain independent,
even if they inherit old pair environment variables.

## Failure and reconnect limits

MCP notifications do not have replies. The socket pump forwards asynchronous
server messages while waiting for responses; it does not block initialization
waiting for an answer to `notifications/initialized`.

The client does not retry forever. `PAIR_SOCKET_REDIAL` sets a bounded redial
window in seconds; the default is zero. Redial is transport recovery, not proof
that an interrupted mutating request executed exactly once. After an ambiguous
connection loss, inspect state before repeating an operation.

An explicit low-level invocation with `--state` or `--socket` is a diagnostic
operator path: it connects to an existing daemon and does not auto-start one.
The automatic startup applies to validated enrolled clients.

## Inspect and recover

From the target project, with the engine path set:

```bash
PAIR_HOME="$HOME/pair-mailbox"
"$PAIR_HOME/bin/pairctl" status
"$PAIR_HOME/bin/pairmcpdoctor" --role codex
"$PAIR_HOME/bin/pairrestart" --role codex --dry-run
"$PAIR_HOME/bin/pairrelaunch" --role codex --check
```

`pairrestart` targets stale server code. `pairrelaunch` without `--check` restarts
the CLI and loses its in-memory context. These are different recovery actions.
A disconnected critic retains its write boundary during repair.

## Return to child transport

Finish the active pair and stop its role daemons using the PIDs recorded for
that project. A still-running socket daemon holds the role lock and prevents a
new child server for the same role from starting. Then, from the engine clone:

```bash
PAIR_TRANSPORT=child ./install.sh codex agy
```

Restart the CLIs to load the new entries and use `pairup` for the next pair.
Do not kill a role process identified only by its name across all projects.
