"""Role-specific autonomous launch policy for the Pi CLI."""
from __future__ import annotations


def safe_pi_command(command: list[str], is_writer: bool) -> list[str]:
    """Trust managed project resources and keep Critic file tools disabled.

    Pi tool calls run with the process's OS permissions. ``--approve`` only
    handles project-local trust; Critic write prevention remains the outer
    Seatbelt/bwrap sandbox's job.
    """
    args = command[1:]
    removed_flags = {
        "--approve", "-a", "--no-approve", "-na",
        "--no-tools", "-nt", "--no-builtin-tools", "-nbt",
    }
    value_flags = {"--tools", "-t", "--exclude-tools", "-xt"}
    cleaned = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in removed_flags or arg.startswith(("--tools=", "--exclude-tools=")):
            index += 1
            continue
        if arg in value_flags:
            if index + 1 >= len(args):
                raise ValueError(f"Pi option {arg} needs a value")
            index += 2
            continue
        cleaned.append(arg)
        index += 1
    policy = ["--approve"]
    if not is_writer:
        policy += ["--exclude-tools", "edit,write"]
    return [command[0], *cleaned, *policy]
