#!/usr/bin/env python3
"""Require a test sibling or a documented exemption for each executable in bin/."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS = ROOT / "scripts" / "bin-test-exemptions.json"


def tracked_modes(root: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", "bin"], cwd=root,
        capture_output=True, text=True, check=True)
    paths = {}
    for line in result.stdout.splitlines():
        metadata, path = line.split("\t", 1)
        mode = metadata.split()[0]
        paths[path] = mode
    return paths


def has_test_sibling(path: str, tracked: set[str]) -> bool:
    item = Path(path)
    if item.name.endswith("_test.py"):
        return True
    stem = item.stem if item.suffix == ".py" else item.name
    candidates = {str(item.with_name(f"{stem}_test.py")),
                  str(item.with_name(f"test_{stem}.py"))}
    return bool(candidates & tracked)


def audit(paths: set[str], exemptions: object, tracked: set[str] | None = None) -> list[str]:
    if not isinstance(exemptions, dict):
        return ["exemption file must contain a JSON object mapping paths to reasons"]
    errors: list[str] = []
    tracked = tracked or paths
    for path, reason in exemptions.items():
        if not isinstance(path, str) or path not in paths:
            errors.append(f"stale exemption: {path!r} is not a tracked executable")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"exemption for {path!r} needs a non-empty reason")
    for path in sorted(paths):
        if Path(path).name.endswith("_test.py") or has_test_sibling(path, tracked):
            continue
        if path not in exemptions:
            errors.append(f"{path} has no *_test.py sibling and no explicit exemption")
    return errors


def main() -> int:
    try:
        modes = tracked_modes(ROOT)
        paths = {path for path, mode in modes.items() if mode == "100755"}
        exemptions = json.loads(EXEMPTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"bin-test-coverage: cannot inspect tracked executables: {exc}", file=sys.stderr)
        return 2
    errors = audit(paths, exemptions, set(modes))
    if errors:
        print("bin-test-coverage: uncovered executables:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"bin-test-coverage: {len(paths)} executable files have tests or documented exemptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
