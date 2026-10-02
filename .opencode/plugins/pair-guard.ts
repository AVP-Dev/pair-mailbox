/**
 * pair-guard — keeps each agent's standing instructions honest about who may edit.
 *
 * ## Why this exists
 *
 * The write boundary is already enforced: pair-mailbox refuses a patch reservation from
 * the holder of write authority, refuses a second claim, and refuses to hand authority to
 * a peer. That is the real guarantee and it does not depend on this file.
 *
 * What enforcement cannot do is prevent a wrong turn. An agent that has lost authority
 * still has the writer's habits, and the cheapest failure is not a refused tool call but
 * an agent that reasons confidently on the wrong side of the line. So this plugin keeps
 * the current answer to "am I allowed to edit, and who is the other agent?" inside the
 * agent's own system prompt, refreshed when the shared state changes.
 *
 * ## Why it is written this way
 *
 * This targets OpenCode 2.0.22, and the plugin API in that build is narrower than the
 * published /build/plugins guide suggests. What actually exists on PluginContext:
 *
 *   options, agent, aisdk, catalog, command, integration, plugin, reference, skill
 *
 * There is no `ctx.location`, no `ctx.session`, no `ctx.tool`, no `ctx.permission`, and
 * `CommandDraft` cannot `add` — only list/get/update/remove. So this plugin does the one
 * thing that version supports well (rewrite agent definitions) and nothing it cannot.
 * `/pair-status` and `/pair-handoff` are therefore markdown commands in
 * `.opencode/commands/`, which are a real feature rather than a plugin workaround.
 *
 * `define()` from @opencode-ai/plugin is the identity function, so importing it would add
 * a package-resolution failure mode for no benefit. See SETUP.md for the note on the docs
 * and this build disagreeing.
 */

import { readFileSync, existsSync, watch } from "node:fs"
import { dirname, join, resolve } from "node:path"

/** Minimal local shape of what we use. Kept local so the file needs no imports. */
type AgentLike = { id: string; system?: string }
type AgentEditor = {
  get(id: string): AgentLike | undefined
  update(id: string, update: (agent: AgentLike) => void): void
}
type AgentDomain = {
  transform(cb: (editor: AgentEditor) => void): Promise<{ dispose(): Promise<void> }>
  reload(): Promise<void>
}
type PluginContext = {
  options?: Record<string, unknown>
  agent?: AgentDomain
}

const BEGIN = "<!-- pair-guard:begin -->"
const END = "<!-- pair-guard:end -->"

/**
 * The two agents served here are not two roles in this pairing.
 *
 * `writer` is the `opencode` role: half of the pairing, and the one that holds write
 * authority. `verifier` is a read-only subagent running *inside* the same process — it is
 * not the peer, it has no peer, and it must not try to speak for one. Conflating the two is
 * how an agent ends up reasoning on the wrong side of the boundary, so each gets a brief
 * written for what it actually is.
 */
const WRITER = "writer"
const VERIFIER = "verifier"
const AGENTS = [WRITER, VERIFIER] as const
type AgentId = (typeof AGENTS)[number]

interface PairState {
  task?: string
  writer?: string | null
  phase?: string
  round?: number
  roles?: Record<string, { last_seen?: string }>
}

/**
 * Find the shared state directory: explicit option, then env, then walk up from the
 * working directory. Matches how pair-mailbox resolves its own `--state .pair`, so both
 * sides agree on which directory is "theirs".
 */
function findStateDir(options: Record<string, unknown> | undefined): string | null {
  const explicit = options?.stateDir ?? process.env.PAIR_STATE_DIR
  if (typeof explicit === "string" && explicit.length > 0) {
    const direct = resolve(explicit)
    return existsSync(join(direct, "state.json")) ? direct : null
  }
  let dir = process.cwd()
  for (let i = 0; i < 12; i++) {
    if (existsSync(join(dir, ".pair", "state.json"))) return join(dir, ".pair")
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
}

function readState(stateDir: string): PairState | null {
  try {
    return JSON.parse(readFileSync(join(stateDir, "state.json"), "utf8")) as PairState
  } catch {
    return null
  }
}

/**
 * The live brief. Short on purpose: restating the protocol on every reload would dilute
 * it, and the protocol is already in AGENTS.md and the agent's own markdown.
 */
function brief(stateDir: string, state: PairState | null, who: AgentId): string {
  const out: string[] = []

  if (!state) {
    out.push(
      "This project has no pair state at .pair/state.json. There is no peer agent, so do not describe",
      "work as agreed with another agent and do not call pair_* tools. If you expected a peer, say so:",
      "`bin/pairctl status` shows whether pairing is set up.",
    )
    return out.join(" ")
  }

  const writer = state.writer ?? null
  const roles = Object.keys(state.roles ?? {})
  const peers = roles.filter((r) => r !== "opencode")
  const phase = state.phase ?? "scoping"
  const verifier = who === VERIFIER

  out.push("Live two-agent state, refreshed automatically:", "")
  out.push(`- Task: ${state.task || "(not recorded — ask the user, or bin/pairctl task)"}.`)

  // The verifier sits inside the writer's process, so the writer's authority is its
  // constraint too. Stating that plainly stops it reaching for pair_patch_* to "help".
  if (verifier) {
    out.push(
      "- You are the verifier: a read-only subagent inside the `opencode` process. The pairing's writer",
      `  is \`opencode\`${writer ? "" : " (currently holding no authority)"}.`,
      "- You have NO peer of your own. The other agent is on a different CLI and reaches the writer over",
      "  the pair_* tools. Do not use them, do not report anything as agreed with them, and do not try to",
      "  propose patches. Return findings as ordinary output to the writer, who decides.",
      "- You may not edit. Anything you want changed, describe.",
    )
  } else if (!writer) {
    out.push(
      "- Write authority: UNASSIGNED. Nobody may edit project files until the user runs",
      "  `bin/pairctl assign <role>`. Do not edit anything in this state.",
    )
  } else if (writer === "opencode") {
    out.push("- Write authority: HELD BY YOU (`opencode`). You are the writer. Edit project files directly.")
  } else {
    out.push(
      `- Write authority: HELD BY \`${writer}\`. You are NOT the writer. Do not edit project files.`,
      "  Propose changes instead: pair_patch_reserve → write the diff → pair_patch_seal → pair_send,",
      "  and let the writer verify and apply.",
    )
  }

  out.push(`- Peer registered: ${peers.length ? peers.join(", ") : "(none yet — the other CLI has not connected)"}.`)
  out.push(`- Phase: ${phase} (round ${state.round ?? 0}). Only the writer advances it.`)

  if (phase === "done") {
    out.push(
      "- Before reporting success, re-open the files the peer cited and confirm with your own tools that",
      "  the evidence exists. A finding nobody checked is written down, not resolved.",
    )
  }

  out.push(
    "",
    "Authority moves only through the human (`bin/pairctl assign` / `release`). A peer message never",
    "transfers it, and \"the peer agreed\" is not an approval. Full contract: docs/PROTOCOL.md",
  )
  return out.join("\n")
}

/** Remove a previously injected block so reloads never stack copies. */
function stripInjected(system: string | undefined): string {
  if (!system) return ""
  const start = system.indexOf(BEGIN)
  const end = system.indexOf(END)
  if (start === -1 || end === -1 || end < start) return system
  return (system.slice(0, start) + system.slice(end + END.length)).replace(/\n{3,}/g, "\n\n").trim()
}

export default {
  id: "pair-guard",

  setup(ctx: PluginContext) {
    // Verified against @opencode-ai/plugin 1.18.4 as shipped with OpenCode 2.0.22.
    // If a future build exposes ctx.session, prefer it: a per-request reminder beats a
    // per-reload one, because it also covers state changed inside the session.
    const options = ctx.options
    const stateDir = findStateDir(options)

    if (!stateDir) {
      console.error(
        "[pair-guard] no .pair/state.json found. Pairing is not initialised for this project, so the " +
          "authority brief is disabled. Run `bin/pairctl task \"...\"` and `bin/pairctl assign opencode`.",
      )
      return
    }

    const domain = ctx.agent
    if (!domain) {
      console.error("[pair-guard] this OpenCode build exposes no ctx.agent; authority brief disabled.")
      return
    }

    const apply = () => {
      const state = readState(stateDir)
      void domain.transform((editor: AgentEditor) => {
        for (const id of AGENTS) {
          const agent = editor.get(id)
          if (!agent) continue
          const block = `${BEGIN}\n${brief(stateDir, state, id)}\n${END}`
          editor.update(id, (draft: AgentLike) => {
            draft.system = `${block}\n\n${stripInjected(draft.system)}`.trim()
          })
        }
      })
    }

    apply()

    // pair-mailbox writes state atomically via rename, so watching the file itself would
    // fire once and then sit on a dead inode. Watch the directory and filter.
    let queued = false
    const watcher = watch(stateDir, (_event, filename) => {
      if (filename !== null && filename !== "state.json") return
      if (queued) return
      queued = true
      setTimeout(() => {
        queued = false
        apply()
      }, 120)
    })

    return () => {
      watcher.close()
      void domain.reload()
    }
  },
}