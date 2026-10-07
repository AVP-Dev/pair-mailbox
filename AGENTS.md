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

It also returns `mode`, the second thing that decides your job. In `audit` mode nobody
edits anything, the server refuses patch reservations, and the writer's share is running
the round rather than committing code. So the table above reads differently: `edit`,
`commit` and `propose patches` are all off for both. See [`docs/PROTOCOL.md`](docs/PROTOCOL.md)
§10a, and `bin/pairctl status` for the mode of any project.

If `you_are_writer` is true you are the `writer` agent. If not, you are the `critic` / `explorer`
role on the other CLI, and your value is entirely in finding things and proving them.

## Roles

A role **is the CLI's name**. It is baked into that CLI's MCP config by `install.sh` and cannot be
chosen per call. That is deliberate: the server must know which process is which, and a role a
process can set for itself is not an identity.

`cli-profiles.json` lists every CLI that can take part:

| role name | runs in | agent files |
|---|---|---|
| `opencode` | OpenCode | `.opencode/agents/writer.md` |
| `agy` | Antigravity CLI | `.agents/agents/pair-writer/agent.md`, `pair-critic`, `pair-explorer` |
| `codex` | Codex CLI | — |
| `claude` | Claude Code | — |

Any two **different** names can pair up. So `codex` + `claude` works exactly like
`opencode` + `agy`: the same server, the same protocol, only the names differ. You will see your
own role name in `pair_state` under `you`, and your peer's in `writer` or `peers`.

To add a CLI: append an object to `cli-profiles.json` and run `./install.sh <name>`.

### `verifier` is not a role

`.opencode/agents/verifier.md` is a read-only subagent that runs **inside** the `opencode`
process. It is not the peer and has no peer: the other agent is on a different CLI and reaches the
writer over `pair_*`. Its findings come back as ordinary subagent output.

Keep that distinction. An agent that believes it has a peer will start negotiating with it.

## Communicating

All of it goes through the `pair_*` MCP tools — there is no other channel. `pair-chat` (a skill,
present for both CLIs) covers usage. The short version:

- `pair_deliver` to actively wake the peer in its composer and hand work over immediately.
- `pair_send` to talk passively via mailbox, `pair_read` to listen, `pair_note` to leave something behind.
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
- **Recheck backlog locations.** A backlog card's `file:line` rots; before working one, open the files and confirm the lines still say what the card claims.
- **Verify changed, gate once.** Per commit: `scripts/verify-changed.sh` (static + affected suites). Full gate: once per round before verify.
- **Finish the loop.** Read the peer's findings and open the files it cited before reporting done.
- **One test file per source file.** `foo.go` → `foo_test.go`, never a third.
- **Leave the tree clean.** No stray patch files, no scratch notes outside `.pair/`.
- **No human pigeon.** Never ask the user to relay messages, reviews, handoffs, or prompts to the
  other window. The two agents are an autonomous pair. Use `pair_deliver` to actively wake the peer.
  The user defines the task and receives the finished PR; the user is not a message bus.