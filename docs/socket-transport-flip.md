# Switching a project to the socket transport

The socket transport (rounds 106–107) makes a pair-MCP drop cost a **reconnect**
instead of a **relaunch**: the server is a daemon that outlives the CLI, the CLI
connects to it, and killing or restarting the CLI no longer kills the server.

This document is the operator procedure, written BEFORE the config flip by the
peer's ruling (seq 1632): the config change is real and safe, the RESTART is the
human's, and nobody should have to guess what happens.

## What the flip changes

The MCP entry in the CLI's config switches from a **child server**

```
sh <project>/mcp/pair_mailbox/serve.sh <role> --state <project>/.pair
```

to a **thin client**

```
python3 <project>/mcp/pair_mailbox/mcp_socket_client.py <role>
      [--state <project>/.pair] [--socket <project>/.pair/.server-<role>.sock]
```

The client connects to a daemon that **pairup raised before the CLI started**
(`bin/pairup`, `PAIR_TRANSPORT=socket`). The client REFUSES with exit 4 rather
than spawning a server — a client that spawns would rebuild the child-lifetime
defect this whole chain removes.

Until the flip, the default stays the child server: `PAIR_TRANSPORT=socket` is
opt-in, so a project that has not switched is unaffected.

## The procedure

```bash
# 1. Raise the daemon for the role (idempotent: a live socket is reused)
cd <project>
PAIR_TRANSPORT=socket bin/pairup            # or: pairrelaunch --role <role>

# 2. Verify the daemon is alive and serving
ls -l .pair/.server-<role>.sock             # the socket exists
bin/pairctl status                          # prints the attach line

# 3. Swap the entry (additive; the old child-server line is kept in the file as
#    a comment for one round, then removed)
PAIR_TRANSPORT=socket ./install.sh          # rewrites the entry + stamps pairTransport

# 4. Restart the CLI — THE ONLY STEP WITH RISK, and it is the human's:
#    the session's in-memory context is lost, exactly as any CLI restart costs.
#    Exit the CLI and launch it again (the launch line is in cli-profiles.json).

# 5. Verify
pgrep -f "server.py --role <role>"          # the daemon survived the restart
bin/pairctl status                          # tmux_socket + attach line
```

## How to tell which lane a project is on

`pairctl status` prints the row's `tmux_socket`, and the MCP entry carries
`pairTransport`:

- `pairTransport: child server (default)` — the pre-flip lane; the server's
  lifetime equals the CLI's.
- `pairTransport: thin socket client (daemon raised by pairup)` — post-flip.

## What a drop costs, per lane

| | child server (pre-flip) | socket client (post-flip) |
|---|---|---|
| server dies | CLI holds a broken pipe; recovery = CLI relaunch (session context lost) or the `/mcp` picker (measured: four failure shapes) | daemon restarted by pairup/pairrelaunch; the CLI **reconnects** |
| CLI dies | server dies with it | **daemon survives**; the next CLI connects |
| CLI restart | server restarts too | **server untouched** — the refresh shape the human asked for |

## Rollback

Remove the `pairTransport` stamp from the entry, restore the child-server line
(`sh serve.sh <role> --state ...`), restart the CLI. One round's grace: the
child-server line stays as a comment until the flip has survived a full round.

## What is NOT automatic, on purpose

- **The daemon is not respawned by the client.** A silent spawn would rebuild
  child-lifetime. The client refuses; pairup raises.
- **The restart is not the agent's.** An agent restarting its own CLI mid-round
  is a self-inflicted outage. The config flip is safest exactly because the
  risky step lands in human hands.
