# Threat Model — pair-mailbox

> Language: this is the English original. The Russian mirror is [THREAT_MODEL.ru.md](./THREAT_MODEL.ru.md).

A short statement of record: what we protect, from whom, where the trust boundary lies, and what is out of scope.
Grounded in the round 74 audit (task #77): allowlist-bypass vectors via
auto-approval, shell metacharacters, env prefixes, and honest phrasing.

## Assets

1. Operator's machine: files, secrets (`~/.ssh`, `.env`, tokens), network on their behalf.
2. Project repository: code, git history, branches, PRs.
3. Pair integrity: state.json, transcript, mailbox — write and phase decisions.
4. Neighbouring projects on the same tmux host: panes, processes, files.

## Attackers

1. **Untrusted text in the agent's context** (primary vector): contents of
   issues/PRs/web pages that the agent retells as a command. The agent is a
   confused deputy: not itself malicious, but it executes someone else's text as its own.
2. **Malicious command in a permitted wrapper**: `pytest; rm -rf ~`,
   `LD_PRELOAD=/tmp/x.so pytest`, `cp key /etc/passwd`, `python3 -c … # test`.
3. **Neighbour process on a shared host**: someone else's tmux panes, shared state.json.
4. **NOT an attacker**: the operator themselves (their `pairctl ok`, their allowlist file —
   trusted decisions, not attacks) and the second agent of the pair (it has no motive
   to break the shared round; we defend against its mistakes only, not its malice).

## Trust boundaries

- Auto-approval (`pair_unblock`, pilot) trusts ONLY the
  `permission_verdict` match: NEVER above everything, allow strictly inside.
  Everything else goes to a human.
- The SHA-256 patch seal is an integrity seal between proposal and application
  (protection against races/substitution by a third process), NOT an authorship signature and NOT
  protection against a malicious critic: the critic writes the diff itself, the hash does not cure that.
- `pair_evidence` is the agent's self-declaration; the close gate trusts it.
  The model does not protect against forged evidence: that is bookkeeping, not verification.
- state.json and mailbox are a trusted zone: access to them = access to pair
  control. Isolation is via filesystem permissions, not cryptography.
- Tests and build scripts run on the host as the user with their full
  privileges (P0 open: there is no isolation, see out-of-scope).

## Already closed (round 74, P1)

- Segmentation of compound commands: each segment split on `; && || | &` outside quotes
  is checked separately (`split_command_segments`, `composer.py`).
- Env prefixes that change process code (`LD_PRELOAD`, `PYTHONPATH`, etc.) — refuse.
- `cp`/`mv`/`ln` destinations outside the project and `/tmp` — refuse.
- The adversarial suite `test_allowlist_bypass.py` guards every closed bypass.

## Out-of-scope (deliberately not defended here)

1. **P0 — test-run isolation**: tests run on the host with operator privileges.
   A container/sandbox without network and without `$HOME` access is a separate round.
2. **Agent-forged evidence**: detecting lies about runs is outside the model;
   the defence is human review and `pairctl log`, not a mechanism.
3. **Malicious second agent**: the pair is a cooperation; the protocol does not
   survive Byzantine agents by construction (single writer + seal catch only
   mistakes and races).
4. **Host/operator compromise**: if the enemy is root or the operator themselves,
   no patterns in `composer.py` will help.
5. **The network as such**: `curl`/`wget` are forbidden not because the network is evil,
   but because it carries outwards what §12 protects, and carries inwards
   what nobody has checked.
