"""Task Receipt: the round machine check-report (.pair/RECEIPT-<round>.md).

Why: when the round ends (§10, done) an immutable artefact must stay, pinning:
- The round number, the round task;
- Who wrote and who criticised;
- The round closed tasks (tasks);
- The gathered evidence (evidences);
- The actual budget metrics (messages, moves, hours, restarts);
- The HEAD hash and the close date.
- [R82] The Ground Truth section: the facts dirty_hash, the anti-mock verdict
  (PASS/BLOCKED + count), the ledger record link. The receipt is fail-closed:
  on combat mocks no receipt writes, GroundTruthBlocked raises instead.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class GroundTruthBlocked(Exception):
    """Facts forbid the receipt: combat mocks or diff drift."""




def generate_receipt(state_dir: Path, state: dict[str, Any], head_commit: str = "") -> Path:
    """Generate and write .pair/RECEIPT-<round>.md."""
    state_dir = Path(state_dir)
    round_number = int(state.get("round", 0))
    target = state_dir / f"RECEIPT-{round_number}.md"

    writer = state.get("writer", "—")
    roles = state.get("roles", {})
    peers = [r for r in roles if r != writer]

    # Round task gathering: bound by CLOSE-round (round 94 D2). Tasks closed
    # before D2 carry no closed_round — they fall back to the add-round.
    tasks = state.get("tasks", [])
    round_tasks = [t for t in tasks
                   if int(t.get("closed_round", t.get("round", -1)) or -1) == round_number]

    # Round evidence gathering
    evidences = state.get("evidence", [])
    round_evidences = [e for e in evidences if e.get("round") == round_number]

    # Budget metrics
    metrics_str = "—"
    try:
        from budget import collect_metrics
        m = collect_metrics(state_dir, state)
        metrics_str = (
            f"- Messages: {m['messages']}\n"
            f"- Moves (advances): {m['advances']}\n"
            f"- Round time: {m['hours']} h.\n"
            f"- Pilot restarts: {m['restarts']}"
        )
    except Exception:
        pass

    # [R82] Ground Truth: live facts from the repo root, the gate verdict,
    # the ledger link. Fail-closed: combat mocks raise before the write.
    ground_lines = _ground_truth_lines(state_dir, round_number)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Round subject from the round's own closed tasks, never the legacy
    # single-string state['task'] (round 94 D1: it named round 88's task).
    done_here = [x for x in round_tasks if x.get("status") == "done"]
    subject = done_here[0].get("text", "—") if done_here else "(no tasks closed — see Tasks Closed)"
    lines = [
        f"# Task Receipt — Round {round_number}",
        "",
        f"- **Closed at**: {now_iso}",
        f"- **Task**: {subject}",
        f"- **Writer**: {writer}",
        f"- **Peers**: {', '.join(peers) or '—'}",
        f"- **Mode**: {state.get('mode', 'fix')}",
        f"- **Autonomy**: {state.get('autonomy', 'auto')}",
        f"- **Delivery**: {_delivery_line(state_dir, state)}",
        f"- **Head Commit**: {head_commit or '—'}",
        "",
        "## Tasks Closed in Round",
    ]

    if round_tasks:
        for t in round_tasks:
            status_mark = "x" if t.get("status") == "done" else " "
            lines.append(f"- [{status_mark}] #{t.get('id')}: {t.get('text')} ({t.get('status')})")
    else:
        lines.append("*(no tasks bound to the round)*")

    lines.extend([
        "",
        "## Evidences",
    ])

    if round_evidences:
        for e in round_evidences:
            ok_mark = "OK" if e.get("ok") else "FAIL"
            lines.append(f"- [{ok_mark}] {e.get('kind')}: {e.get('value')} ({e.get('role')})")
    else:
        lines.append("*(no evidence)*")

    lines.extend([
        "",
        "## Budget Metrics",
        metrics_str,
        "",
        "## Ground Truth",
        *ground_lines,
        "",
    ])

    content = "\n".join(lines)
    target.write_text(content, encoding="utf-8")
    return target


def _ground_truth_lines(state_dir: Path, round_number: int) -> list[str]:
    """Ground Truth section: facts, verdict, ledger. A block raises."""
    import anti_mock_gate
    import session_facts
    from ledger import read_round
    repo = state_dir.resolve()
    while not (repo / ".git").is_dir() and repo.parent != repo:
        repo = repo.parent
    if not (repo / ".git").is_dir():
        # Outside a repo there are no facts — UNKNOWN, not a block (contract: PRESERVE).
        return ["- facts: UNKNOWN (state outside the git tree)"]
    facts = session_facts.collect_facts(repo)
    verdict = anti_mock_gate.judge(facts)
    if not verdict["ok"]:
        first = verdict["blockers"][0]
        raise GroundTruthBlocked(
            f"anti-mock BLOCKED ({len(verdict['blockers'])}): "
            f"{first['file']}:{first['line']} [{first['pattern']}]")
    ledger_ref = "—"
    try:
        records = read_round(state_dir, round_number)
        if records:
            ledger_ref = f"ledger.jsonl: {len(records)} round record(s) {round_number}"
    except Exception:
        pass
    return [
        f"- dirty_hash: {facts.get('dirty_hash', '—')}",
        f"- anti-mock: PASS ({len(verdict['blockers'])} blockers, "
        f"{len(verdict['warnings'])} warnings)",
        f"- ledger: {ledger_ref}",
    ]


def _delivery_line(state_dir: Path, state: dict[str, Any]) -> str:
    """One auditable line: which level closed the round and what it did.

    C1/C2: the closing record states the autonomy in force and whether the
    round prepared the PR (semi) or merged (auto). Local facts only — the floor
    from `review.delivery_floor`; the ceiling action follows from the level,
    since reaching the receipt means the advance-path ceiling passed. Audit
    carries no delivery gate.
    """
    import sys

    autonomy = str(state.get("autonomy", "auto"))
    if state.get("mode", "fix") == "audit":
        return f"{autonomy}: n/a (audit carries no delivery gate)"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import review

        repo = Path(state_dir).resolve()
        while not (repo / ".git").is_dir() and repo.parent != repo:
            repo = repo.parent
        status, detail = review.delivery_floor(repo)
    except Exception:  # noqa: BLE001 - the receipt must still write
        status, detail = "unknown", "delivery check failed to run"
    action = (
        "merged (auto means end to end)" if autonomy == "auto"
        else "PR prepared, human word owed (semi stops at prepared)"
        if autonomy == "semi"
        else "floor applies, human at the wheel (observe counts, not commands)"
    )
    return f"{autonomy}: {action}; floor {status} ({detail})"

