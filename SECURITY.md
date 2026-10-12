# Security Policy

## Supported Versions

We provide security updates for the following versions:

| Version | Supported |
|---|---|
| `master` (latest release) | :white_check_mark: |
| Older releases | :x: |

---

## Security Model & Design Invariants

pair-mailbox operates within multi-process environments driving local CLI shells. Security is built into the protocol through the following mechanisms:

1. **Strict Single Writer Invariant**:
   Only the verified writer agent may modify repository files or execute mutating commands. The read-only peer proposes changes exclusively through sealed patches.

2. **Cryptographically Sealed Patches**:
   Proposed patches are stored with restrictive permissions (`0600`) and signed with SHA-256 checksums. The writer recomputes and verifies the checksum immediately before application. Any mismatch halts execution immediately.

3. **Multi-Project tmux Isolation**:
   Tmux target panes are validated by `pane_current_path` and `pane_pid`. Commands and signals cannot cross workspace boundaries into unrelated sessions or projects.

4. **Guarded Permission Unblocking (`pair_unblock`)**:
   Autonomous confirmation of interactive prompts is strictly confined to an immutable allowlist (e.g. read-only inspections). Destructive commands, migrations, and secrets access are never automatically confirmed.

5. **Zero External Runtime Dependencies**:
   The engine relies solely on the Python 3 standard library, eliminating PyPI supply chain attack vectors.

---

## Reporting a Vulnerability

If you discover a security vulnerability within pair-mailbox, please **do not open a public issue**.

Instead, report it via GitHub Private Vulnerability Reporting:
1. Navigate to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Include:
   - Description of the issue and attack vector.
   - Proof of concept or reproduction steps.
   - Affected CLI profiles or environments.

You can also contact the maintainer directly at `aliaksei.patskevich@gmail.com`.

We aim to acknowledge reports within 48 hours and provide a fix or mitigation plan within 5 business days.
