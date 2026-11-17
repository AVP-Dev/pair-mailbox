# Project structure and file hygiene

[Русский](project-structure.ru.md) · [Contributing](../CONTRIBUTING.md)

## Ownership

| Path | Responsibility |
|---|---|
| `mcp/pair_mailbox/` | MCP server, state, authority, transport and coordination helpers |
| `bin/` | project launchers, operator tools and supervisor |
| `cli-profiles.json` | supported CLI configuration and launch profiles |
| `install.sh`, `finish.sh` | integration installation and maintenance |
| `scripts/` | validation gates, suite map, audits and hygiene tooling |
| `skills/` | canonical skills copied to CLI-specific locations |
| `.agents/`, `.opencode/` | repository-specific agent and plugin definitions |
| `docs/` | current guides, protocol and project policy |
| `docs/backlog/` | confirmed open debt; remove a card when its fix is merged |
| `docs/rounds/`, `docs/archive/` | historical records, not current startup instructions |
| `.pair/` | ignored runtime state, mail, patches, evidence and logs |
| `.worktrees/`, `.venv/` | ignored local working environments |

The engine clone and target project can be different repositories. Launchers
resolve target state and record the allowed engine; do not use relative engine
paths after changing into a different project.

## File-size policy

| Size | Expectation |
|---|---|
| up to 300 lines | normal target |
| 300–500 | one explainable responsibility |
| 500–800 | review the boundary |
| over 800 | review the boundary and consider a split |

The current review notes live only in
[`file-size-baseline.tsv`](../scripts/file-size-baseline.tsv). Actual counts are
reported by `./scripts/file-size-gate.sh`; a copied table of counts becomes stale.
These are review guidelines, never permission gates: crossing a size band reports
an advisory and does not stop implementation or verification. A reviewer may ask
for a split when it improves the change, while security and data-safety invariants
remain blocking.

## Verification

From the engine clone:

```bash
./scripts/verify-changed.sh
./scripts/verify-project.sh --list
./scripts/verify-project.sh --jobs 1
```

The profile defines runnable checks; the map selects suites for changed files.
New executable tools need behavioral coverage or a concrete exemption in
`scripts/bin-test-exemptions.json`. Keep tests beside the source, using existing
shared fixtures rather than a second competing test module.

## Refactoring and cleanup

Keep a module's implementation within its owner and make cross-module contracts
explicit. Split modules when responsibility or safe testing demands it.
Verify affected behavior after moving code. Bulk decomposition is a separate
change, not a hidden prerequisite for an unrelated fix.

Ignore generated environments and reports. Preserve runtime history until it is
no longer needed and distinguish archived decisions from current guides.
