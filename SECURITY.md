# Security policy

Security fixes target `master`. Historical snapshots do not receive a separate
maintenance promise. See the [threat model](docs/THREAT_MODEL.md).

## What the protocol checks

- Pair operations use fixed configured CLI roles and one writer. The server
  rejects unauthorized protocol actions; host filesystem access still depends
  on the CLI's permissions and the agent's instructions. This is not an OS sandbox.
- SHA-256 seals detect a changed patch between proposal and application. They
  are integrity checks, not authorship signatures or proof that the patch is safe.
- tmux delivery validates project and pane identity. The mailbox is the durable
  communication channel; a wake-up is best-effort.
- Automatic prompt confirmation uses the allow/deny rules in `composer.py`.
  Routine project edits, tests and feature-branch operations can be permitted.
  Rules can refuse actions but cannot contain arbitrary malicious build scripts.
- Default worktrees do not share mutable output or dependency directories.
  Sharing through `worktree-share` is an explicit project choice. Post-merge
  cleanup preserves dirty/ignored work and verifies the merged PR head.

Ordinary CLI sessions do not acquire pair authority from installed MCP entries,
old state or inherited variables. Explicit launch enrollment is process-scoped;
losing MCP does not grant a critic permission to write.

The default transport is standard-library Python. Optional Ed25519 signatures
use PyNaCl; the full test suite also requires it. Dependencies and host tools
remain part of the operator's environment, not a supply-chain isolation boundary.

Tests and builds run with the host user's permissions. Pair evidence is reported
by agents and does not independently rerun their commands. These limits apply
even when all protocol gates pass.

## Report a vulnerability

Use GitHub private vulnerability reporting in the repository's Security tab, or
contact `aliaksei.patskevich@gmail.com`. Include the affected revision, CLI/version,
reproduction steps and the observed impact. Avoid public disclosure of secrets
or an unmitigated exploit in a normal issue.
