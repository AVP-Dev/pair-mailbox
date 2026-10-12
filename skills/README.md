# pair-mailbox Skills Catalog

This directory contains the modular skill definitions deployed by `install.sh` across all supported AI coding agent CLIs (OpenCode, Antigravity, OpenAI Codex, Claude Code, Pi, OMP).

Skills equip each agent with structured procedures, behavioral invariants, and operational runbooks for multi-agent pairing.

---

## 📦 Installed Skills

| Skill | Purpose | Primary Use Case |
|---|---|---|
| [`pair-chat`](./pair-chat/SKILL.md) | Asynchronous & synchronous messaging protocol | Communicating with the peer agent via `pair_deliver` / `pair_send` / `pair_read` |
| [`pair-work`](./pair-work/SKILL.md) | Autonomous two-agent lifecycle | Full autonomous task execution: scoping, planning, implementation, adversarial review, test evidence, PR |
| [`pair-audit`](./pair-audit/SKILL.md) | Evidence verification & anti-mock verification | Validating test evidence before closing rounds; preventing weakened or fake test passes |
| [`pair-backlog`](./pair-backlog/SKILL.md) | Defect triage and structured parking | Recording real out-of-scope findings into `docs/backlog/<slug>.md` without stalling current rounds |
| [`root-cause`](./root-cause/SKILL.md) | 5-Why root cause analysis | Investigating flaky tests, unexpected crashes, or repeated regressions before writing fixes |
| [`session-finalizer`](./session-finalizer/SKILL.md) | Clean session wrap-up and verification | Closing the protocol loop `implement → review → finalize` with proof of git cleanliness and PR merge |

---

## 🚀 How Skills Are Distributed

When you run `./install.sh`:
1. `install.sh` reads `cli-profiles.json`.
2. For each detected CLI profile, it resolves `skills_dir`:
   - **OpenCode**: `~/.config/opencode/skills/`
   - **Antigravity CLI**: `~/.gemini/config/skills/`
   - **Codex CLI**: `~/.codex/skills/`
   - **Claude Code**: `~/.claude/skills/`
   - **Pi**: `~/.pi/agent/skills/`
   - **OMP**: `~/.omp/agent/skills/`
3. Every skill directory from `skills/<name>/SKILL.md` is copied to the agent's global skill directory.
4. Agents automatically discover skills upon session start or invocation.

---

## 🛠️ Usage Guidelines

- **Autonomous Pair Work**: Triggered by user prompts like `"work with peer on X"`, `"работаем в паре"`, or via `bin/pairup --task "..."`.
- **Peer Communications**: Agents use `pair_deliver` for high-priority immediate handoffs (typing into the peer's tmux pane) and `pair_send` for background notes, reviews, and reports.
- **Never Human Relay**: Agents are strictly instructed never to ask human users to relay messages between windows. All communication flows autonomously through the `pair_*` MCP tools.
