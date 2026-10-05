# Setup

## Prerequisites

- `python3` 10 or newer on `PATH` (the server has no third-party dependencies)
- Two agent CLIs. The pairing is configured for **OpenCode** and **Antigravity CLI (`agy`)**; the
  same server works for anything else that speaks MCP — see *Another CLI* below.
- Both agents working the same repository, in the same project directory, so they resolve the same
  `.pair/` state directory.

Check the server runs before wiring anything to it:

```sh
python3 mcp/pair_mailbox/server.py ctl status
python3 mcp/pair_mailbox/test_protocol.py     # 247 tests, ~27s (327 in the full suite)
```

## 1. Initialise the pairing

```sh
bin/pairctl task "fix the retry storm: clients re-send on 502 instead of on 503"
bin/pairctl assign <роль>        # роль = имя CLI: opencode, agy, codex, claude
bin/pairctl status
```

Substitute your own two CLI names throughout. The role is whatever `install.sh` wrote into
each CLI's config, so `--list` on the installer shows what you actually have.

`assign` is the only way authority ever moves. Skipping it leaves the state `UNASSIGNED`, in which
case neither agent may edit — which is a safe default, not a bug.

## 2. Start both agents

In **agterm**, one pane per agent, same working directory:

```
opencode                                    # pane 1
agy                                         # pane 2
```

Any two different CLIs, in any order. Roles come from the standing arrangement if you set
one, and only otherwise from a claim:

```sh
bin/pairctl roles --writer opencode --critic agy --global   # set it once
```

With an arrangement in place, pane order does not decide anything, and neither agent
needs to be told about the other: they find each other through the shared state
directory. Without one, whoever reads the task first ends up as writer.

`pair_deliver` needs both agents inside tmux panes. Outside tmux, `pair_send`, `pair_read`
and `pair_note` still work — only the live wake-up is lost.

### Antigravity: allow the MCP tools

Antigravity runs unconfigured MCP tools in **Ask** mode, so the first `pair_*` call will stop for
approval. Allow the whole server once:

```
/permissions
```

and add `mcp(pair/*)`. Without this, the pairing technically works and practically does not.

Antigravity picks up `.agents/mcp_config.json` from the workspace. Verify with `/mcp` that `pair`
is connected, and that its seventeen tools are listed.

The three agents — `pair-writer`, `pair-critic`, `pair-explorer` — appear under `/agents`.

### OpenCode

`opencode.jsonc` and the `pair-guard` plugin are picked up automatically from `.opencode/`. If the
plugin was added while the service was running:

```sh
opencode service restart
```

Then, in the session:

```text
/pair-status      # who holds authority, the task, the phase, unread messages
/pair-handoff     # leave a handoff note and report what is still open
```

`pair-guard` injects the live authority state into every agent-loop request, so a session that has
been running for hours still knows which side of the boundary it is on.

## 3. Smoke test

Two minutes, and it proves the whole chain including the refusals.

Ask the writer: *"Are you the writer? Reply with one sentence."* — expect yes.

Ask the critic: *"Try to edit README.md."* — expect a refusal, not an edit.

Ask the writer: *"Try `pair_patch_reserve`."* — expect a refusal: the writer does not propose
patches.

From the other pane, run:

```sh
bin/pairctl log --limit 20
```

The refusals are in there. Seeing them means the boundary is real rather than advisory.

## 4. Work

Normal collaboration from here. The things worth doing deliberately:

- **Say something early.** Two agents that never talk produce one agent's opinion with extra steps.
- **Let the critic attack the plan**, not just the diff. The cheapest disagreement is before anyone
  writes code.
- **Hand over, do not stall.** `bin/pairctl release` then `bin/pairctl assign <роль соседа>`, then tell the
  new writer it is the writer and the old one is read-only. Doing this in both sessions is not
  optional bookkeeping; it is how each agent learns the change.

## Layout

```
mcp/pair_mailbox/server.py       the protocol, enforced
mcp/pair_mailbox/test_protocol.py 247 tests (327 with pilot + installer)
bin/pairctl                      the human's authority surface
.opencode/                       OpenCode: config, writer + verifier agents, plugin, commands, skill
.agents/                         Antigravity: mcp_config, writer/critic/explorer, skill
docs/PROTOCOL.md                 the contract
.pair/                           runtime state (git-ignored)
```

`.pair/` holds `state.json`, `pair-transcript.jsonl`, the mailboxes, shared notes, and in-flight
patches. It is runtime, and it is disposable.

## Another CLI

The bridge is MCP, so nothing about it is specific to these two. Add a server entry pointing at the
same wrapper with a new role name:

```jsonc
{ "mcpServers": { "pair": { "command": "sh",
    "args": ["mcp/pair_mailbox/serve.sh", "<role-name>"] } } }
```

Then `bin/pairctl assign <role-name>`. Add the peer name to `pair_send(to=...)`, and register the
role so it shows up in the roster. Copy an existing agent markdown and adjust the tool names if the
CLI's MCP prefix differs.

Two OpenCode sessions on different models works the same way: copy the `opencode.jsonc` block,
give the second one role `opencode2`.

## Config shapes that cost us a bug each

**The `timeout` above must exceed the protocol's wait ceiling.** `pair_read` can wait
60 seconds, and that wait is part of the protocol: an agent that cannot wait never
notices an incoming message, and the human starts relaying by hand. A client that
gives up first tears the call down mid-wait, and the agent cannot tell "nobody
answered" from "I was disconnected" — so it draws a conclusion about its peer instead
of about itself. Hence 65000 against a 60s ceiling. The server also clamps the wait to
what the client can survive and says so in the answer (`wait_capped_by_client_timeout`),
and a test fails the suite if any profile in `cli-profiles.json` is set below the
ceiling.

`opencode` and Antigravity do **not** share an MCP config shape. Writing one in the other's format
fails silently: the server is simply never registered, and the symptom is "the `pair_*` tools do
not exist" rather than a config error.

**OpenCode** — `mcp` maps a server name straight to its config. There is no `servers` level. The
key must be `type: "local"`, `command` must be an **array of strings**, and `args` does not exist.
Schema: `#/$defs/McpLocalConfig`, `additionalProperties: false`.

```jsonc
"mcp": {
  "pair": {
    "type": "local",
    "command": ["sh", "mcp/pair_mailbox/serve.sh", "opencode"],
    "enabled": true,
    "timeout": 65000
  }
}
```

**Antigravity** — `mcpServers`, `command` is a string, and `args` is a separate array.

```json
{ "mcpServers": { "pair": { "command": "sh", "args": ["mcp/pair_mailbox/serve.sh", "agy"] } } }
```

Third trap, same family: the OpenCode permission key is **`bash`**, not `shell`. `"shell": "ask"`
passes schema validation (unknown keys fall through `additionalProperties`) and does nothing at
all, so shell commands quietly stopped prompting for approval. Valid keys include `read`, `edit`,
`glob`, `grep`, `list`, `bash`, `task`, `webfetch`, `websearch`, `skill`, `external_directory`.

Validate rather than trust, especially after an OpenCode upgrade:

```sh
# editor validation: $schema is already in the file
# or check by hand:
python3 -c "import json;print(json.load(open('.opencode/opencode.jsonc')))"
```

The live shape of any field is in `https://opencode.ai/config.json` under `$defs`. If a config
change seems to do nothing, that file is where the answer is.

## The plugin guide disagrees with this build too

Worth knowing before you trust either. The guide at `/v2/docs/build/plugins` documents a much
richer `PluginContext` than OpenCode **2.0.22** actually ships: `ctx.session.hook(...)`,
`ctx.location`, `ctx.tool`, `ctx.permission`, `ctx.worktree`, `ctx.event`, `ctx.storage`. On this
build the real set is:

```ts
interface PluginContext {
  options; agent; aisdk; catalog; command; integration; plugin; reference; skill
}
```

And `CommandDraft` has only `list` / `get` / `update` / `remove` — it cannot `add`. So custom
commands cannot be registered from a plugin here, which is why `/pair-status` and `/pair-handoff`
are markdown files in `.opencode/commands/` instead. That path is a real feature, not a
workaround, and it reloads on save.

Two consequences worth internalising:

- **The package is `@opencode-ai/plugin`, not `@opencode/plugin`** as the guide's examples show,
  and it is only resolvable from OpenCode's own install directory. A project-local plugin cannot
  import it. `pair-guard.ts` therefore imports nothing from it at all: `define()` in that package
  is the identity function, so `export default { id, setup }` is exactly equivalent and removes a
  whole class of resolution failure. That import was the cause of the first
  *"Cannot find package '@opencode/plugin'"* error.
- **When something looks wrong, read the installed types** rather than the docs:

```sh
cat ~/.opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/context.d.ts
cat ~/.opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/agent.d.ts
opencode --version
```

## Things that will surprise you

**Antigravity MCP tools default to Ask.** Covered above; it is the single most common reason a
correct configuration appears broken.

**Roles are baked into config, not environment.** That is on purpose: the server has to know which
process is which, and a role a process can set for itself is not an identity.

**Both agents must run from the same directory.** `--state .pair` resolves against the process
working directory. Two different directories means two empty mailboxes and a silent failure.

**A dead agent keeps its roster entry until someone prunes it.** `bin/pairctl roster`
shows what is registered; `--prune` removes a role only when its process is dead **and**
its pane is either gone or belongs to another live role. One fact is not enough: after
a crash a live pane may be a shell you opened. Left alone, two dead entries sharing a
live pane will make the supervisor skip a living peer.

**Both CLIs ship several times a week.** Re-check the parts that are most likely to move:

| if this changed | re-verify |
|---|---|
| agent definitions | `.opencode/agents/*.md`, `.agents/agents/*/agent.md` frontmatter |
| MCP config | `mcp.servers` in `opencode.jsonc`, `mcpServers` in `.agents/mcp_config.json` |
| permissions | `permission.edit: deny` on `verifier`; `mcp(pair/*)` in Antigravity |
| plugin API | `pair-guard.ts` against `node_modules/@opencode-ai/plugin/dist/v2/promise/context.d.ts` |

Nothing in the protocol depends on any of those. The enforcement lives in `server.py`, which is
plain MCP over stdio and has no opinion about which agent is calling it.

## Unwinding

```sh
bin/pairctl release        # stop anyone from editing
bin/pairctl reset --yes    # wipe state, mailboxes, notes, transcript, patches
```

To remove the pairing entirely: delete the `pair` entry from both MCP configs, delete
`.opencode/` and `.agents/` if you added them only for this.