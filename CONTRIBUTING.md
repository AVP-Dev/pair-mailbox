# Contributing

Contributions target `master`. Independent CLI work is supported; the
[pair protocol](docs/PROTOCOL.md) applies when two CLIs are explicitly enrolled.
See [project structure](docs/project-structure.md) for module ownership.

## Development environment

Use Python 3.8+, Git, tmux and Bash 4.3+ (Bash 5 recommended). The full CI suite
runs on Ubuntu 24.04 with Python 3.8–3.12. Some process and tmux tests are Linux-only.
The default runtime is standard-library Python; optional Ed25519 signing and the
complete test suite need PyNaCl.

From your clone:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pynacl
```

Agent CLIs and GitHub CLI are separate installations. Do not run `install.sh`
unless you intend to update local CLI integrations, instructions and permissions.

## Change and verify

Start from current trunk and create a focused branch:

```bash
git fetch origin
git switch -c fix/readme origin/master
```

Branch classes are defined in [branch_classes.py](mcp/pair_mailbox/branch_classes.py):
`fix/`, `feature/`, `chore/`, `round-` and other declared ephemeral prefixes.
`feat-abc` is not a declared class. Avoid accumulator branches such as `dev`.

```bash
./scripts/verify-changed.sh
./scripts/verify-project.sh --list
./scripts/verify-project.sh --jobs 1
```

Run one gate per worktree. The changed-file gate runs static checks and affected
suites selected by `scripts/verify-map.tsv`. The full gate uses
`scripts/verification-profile.tsv`; do not keep a second fixed suite list in docs.
Record the actual scope and result, including platform/dependency failures.

Keep source and its tests together: `module.py` and `test_module.py`, or the
established `bin/name` and `bin/name_test.py` convention. Add new checks to the
verification profile and map. Gate scripts also check comments, file sizes,
secrets, documentation parity and executable test coverage.

The file-size target is 300 lines; over 800 needs an explained exception in
`scripts/file-size-baseline.tsv`. Update a ceiling only when the actual change
requires it, with a concrete reason. Avoid unrelated decomposition during a fix.

## Adding a CLI profile

1. Add MCP configuration, launch command, instruction path and skill directory
   to `cli-profiles.json`.
2. Update the launchers' supported-profile lists and native permission handling
   where applicable; a JSON entry alone does not extend every shell dispatcher.
3. Check process enrollment, including interpreter wrappers and nested independent
   CLI starts. A failed paired connection must not promote a critic to writer.
4. Add focused installation/launch coverage, then run the affected suites.
5. Install the profile and check it from the engine clone:

```bash
./install.sh mycli
./install.sh mycli --check
```

`--check` verifies an existing configuration; it does not install the new one.
Do not publish unverified host-specific UI commands as universal setup instructions.

## Shipping and cleanup

Describe the problem, resulting behavior and measured validation in the commit
and PR. Keep English documentation and its Russian mirror consistent. PR means
pull request: reviewable changes proposed for merging into `master`.

An explicitly paired round uses its protocol's evidence and review gates,
including `pairctl pr merge`. Never forge a peer verdict or evidence to bypass a gate.
For any workflow, merge only the checked head under the repository's actual rules.

Remove temporary branches after their work is merged. Automatic worktree cleanup
must verify PR/repository/head identity and preserve dirty or ignored files.
Do not treat unmerged branches, `.pair` runtime state, backlog cards or historical
round records as disposable generated files.
