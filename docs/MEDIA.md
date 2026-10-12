# pair-mailbox Media & Showcase Guide

This guide describes the visual assets, screenshot specifications, and automated terminal recordings (GIF/VHS) for showcasing **pair-mailbox** in documentation, GitHub READMEs, and project presentations.

---

## 🎨 Vector Assets

The repository includes standalone SVG vector assets that render cleanly in GitHub dark and light modes:

- [`assets/pair-mailbox-banner.svg`](../assets/pair-mailbox-banner.svg): Main header banner with architecture overview and core invariants.
- [`assets/pair-flow-diagram.svg`](../assets/pair-flow-diagram.svg): Step-by-step lifecycle flow from scoping to PR.

---

## 📸 Recommended Screenshots Specification

To give new users an immediate visual grasp of pair-mailbox in action, take the following 4 screenshots and place them in `assets/screenshots/`:

### 1. Two-Agent Split Session (`assets/screenshots/pair-split-session.png`)
*Shows the reality of two different agent CLIs working concurrently in tmux without human intervention.*

- **Layout**: Horizontal split in `tmux` (two vertical panes).
- **Left Pane (Writer)**:
  - Command: `opencode --standalone --auto` or `agy`
  - Content visible: Writer executing tests (`verify-changed.sh`), applying a sealed patch, and committing code.
- **Right Pane (Critic)**:
  - Command: `codex` or `claude`
  - Content visible: Critic inspecting a `git diff`, reviewing code with `file:line` citations, proposing a sealed patch via `pair_propose`.
- **Status Bar**: `tmux` status bar showing window name `pair` and project directory.

### 2. Live Chat Streaming (`assets/screenshots/pair-chat-tui.png`)
*Highlights the inter-agent communication channel with colorized role labels and protocol events.*

- **Command**:
  ```bash
  bin/pairchat
  ```
- **Content visible**:
  - Colored speaker tags: green `Chat from agy:`, yellow `Chat from codex:`.
  - Notification of patch reservation & SHA-256 seal.
  - Plan acknowledgement and phase advancement.
  - Bounded wait indicators without stalls.

### 3. Real-Time Web Dashboard (`assets/screenshots/pair-board-web.png`)
*Showcases the zero-dependency, self-contained HTML dashboard.*

- **Command to generate**:
  ```bash
  bin/pairboard
  # Open board.html in browser:
  xdg-open board.html  # or open board.html on macOS
  ```
- **Content visible**:
  - Header: Active round, phase badge (`implement` / `verify`), autonomy mode (`auto`).
  - Active roles: Writer (pinned), Critic (live process and pane).
  - Shared task board: Completed tasks with timestamps and authors.
  - Supervisor status: `pairpilot` process status, obligations queue, recent audit transcript.

### 4. Health & Environment Diagnostics (`assets/screenshots/pair-doctor.png`)
*Demonstrates instant installation verification and zero-friction setup.*

- **Command**:
  ```bash
  bin/pairdoctor
  ```
- **Content visible**:
  - Green checkmarks across installed CLIs (`opencode`, `agy`, `codex`, `claude`, `pi`, `omp`).
  - Verification of `tmux` version and permissions allowlist.
  - MCP configuration validity.

---

## 📹 Automated Terminal Demos (VHS / asciinema)

You can generate animated SVG/GIF recordings using [Charm VHS](https://github.com/charmbracelet/vhs).

### VHS Tape Script: `assets/demo.tape`

Create `assets/demo.tape` with the following content:

```vhs
# Output configuration
Output assets/pair-demo.gif
Set FontSize 14
Set Width 1200
Set Height 680
Set Padding 20
Set Theme "Catppuccin Mocha"

# Launch pairup
Type "pairup --task 'fix retry jitter handling in client'"
Sleep 500ms
Enter

# Wait for supervisor & pair initialization
Sleep 4s

# Switch to streaming chat view
Type "bin/pairchat --once"
Sleep 500ms
Enter
Sleep 3s

# Inspect shared state
Type "bin/pairctl why"
Sleep 500ms
Enter
Sleep 3s
```

Run VHS to render:
```bash
vhs assets/demo.tape
```

### Asciinema Recording

To record an interactive terminal session for sharing via web players:

```bash
asciinema rec assets/pair-session.cast
# In the recorded session:
pairup --task "prepare public release"
bin/pairchat
# Exit recording with Ctrl+D
```
