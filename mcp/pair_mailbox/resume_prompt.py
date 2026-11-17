"""Build an agent's initial instructions, including an explicit resume choice."""
from __future__ import annotations


def initial_prompt(role: str, standing: str, autonomy: str,
                   resume_task: dict | None = None,
                   resume_workspace: str = "") -> str:
    """Build the role prompt while preserving the selected task's recorded status."""
    autonomy_guidance = {
        "auto": ("No intermediate human approvals are expected. Keep working through review, fixes, "
                 "tests, acceptance, and the final report. Finish every open task returned by "
                 "pair_task(action='list'); do not stop after one task or an arbitrary batch size. "
                 "After each green reviewed slice, commit locally when the configured boundary allows. "
                 "If commit or push is denied, preserve the working tree, record the exact blocker and "
                 "evidence, and continue other open work instead of abandoning the round. When policy "
                 "denies an operation, use a permitted route; never weaken the sandbox or falsely close "
                 "the task."),
        "semi": ("Complete routine work autonomously and stop only at the configured human checkpoint. "
                 "Keep the security boundary unchanged."),
        "observe": ("The supervisor observes and reports without taking automatic decisions. "
                    "Keep the security boundary unchanged."),
    }.get(autonomy, "Keep working within the configured security boundary.")
    instruction = "work on the open task"
    resume = ""
    if resume_task:
        task_id = resume_task.get("id")
        status = str(resume_task.get("status") or "unknown")
        title = str(resume_task.get("text") or "").strip()
        instruction = (f"resume the selected task #{task_id} [{status}]: {title}. "
                       "Keep its recorded status/history unchanged while continuing the work")
        resume = " The user explicitly selected this item at startup."
    elif resume_workspace:
        instruction = (f"inspect and resume the uncommitted worktree at {resume_workspace}; "
                       "preserve its existing changes")
        resume = " The user explicitly selected this worktree at startup."
    idle = (" If no task is open, report ready and wait."
            if not resume_task and not resume_workspace else "")
    return (f"You were explicitly launched as {role}, initially the {standing}. "
            "If MCP fails, retain that write boundary and repair the connection. "
            "Read pair_state, then pair_task(action='list'), respect your assigned write boundary, "
            f"and {instruction}. Project autonomy is {autonomy}: {autonomy_guidance} "
            f"Communicate directly through pair_*; do not ask the user to relay messages.{resume}{idle}")
