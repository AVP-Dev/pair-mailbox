# AGENTS.md

This project is worked by **two agents at once**, on different CLIs, against the same task. One of
them is you.

Read [`docs/PROTOCOL.md`](docs/PROTOCOL.md) before you touch anything. It is short and it is the
contract between the two sides.

## The one thing to do first

```bash
pair_state
```

It returns `you_are_writer`. Everything downstream depends on that single boolean:

| | writer | read-only |
|---|---|---|
| edit project files | yes | **never** |
| run build and tests | yes | non-mutating checks only |
| propose patches | no, refused by the server | yes, via reserve → seal → send |
| advance the phase | yes | no |
| commit | yes | no |

If `you_are_writer` is true you are the `writer` agent. If not, you are the `critic` / `explorer`
role on the other CLI, and your value is entirely in finding things and proving them.

## Roles

| role name | runs in | agent files |
|---|---|---|
| `opencode` | OpenCode | `.opencode/agents/writer.md` |
| `agy` | Antigravity CLI | `.agents/agents/pair-writer/agent.md`, `pair-critic`, `pair-explorer` |

Roles are fixed in each CLI's MCP config and cannot be chosen per call. That is deliberate: the
server must know which process is which, and a role a process can set for itself is not an identity.

### `verifier` is not a role

`.opencode/agents/verifier.md` is a read-only subagent that runs **inside** the `opencode`
process. It is not the peer and has no peer: the other agent is on a different CLI and reaches the
writer over `pair_*`. Its findings come back as ordinary subagent output.

Keep that distinction. An agent that believes it has a peer will start negotiating with it.

## Communicating

All of it goes through the `pair_*` MCP tools — there is no other channel. `pair-chat` (a skill,
present for both CLIs) covers usage. The short version:

- `pair_send` to talk, `pair_read` to listen, `pair_note` to leave something behind.
- The `Chat from <role>:` label is added by the server. Do not write it yourself.
- Delivery is not a reply. Do not wait, and do not claim the peer is working on it.

## The human holds authority

Write authority moves only through `bin/pairctl`. Not through a message, not through a vote, not
through either agent agreeing. See §2 and §4 of the protocol — those two are what keep this
arrangement safe rather than merely clever.

## House conventions

- **Report what is true.** A proposed patch is not a merged one. A plan is not a result. If it is
  unverified, say it is unverified.
- **Cite locations.** `file:line` for every claim about code.
- **Finish the loop.** Read the peer's findings and open the files it cited before reporting done.
- **One test file per source file.** `foo.go` → `foo_test.go`, never a third.
- **Leave the tree clean.** No stray patch files, no scratch notes outside `.pair/`.