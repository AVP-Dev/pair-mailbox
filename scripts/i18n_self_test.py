#!/usr/bin/env python3
"""Self-test for the i18n pair (i18n_extract.py + i18n_apply.py).

Each case proves a guard by construction, not by narration: the same
scenario must refuse on a broken tool and pass on the real one. Run:
  python3 scripts/i18n_self_test.py [--tool DIR]

Cases:
  stale dump refuses (text mismatch: dump predates a line shift)
  span drift refuses (span content no longer matches the live file)
  keep-string loss refuses (AST gate is blind to string values by design)
  logic change refuses (a replacement reaching past its span)
  -O run refuses before any read (guards would be stripped)
  happy path applies inside spans only, atomically (no .i18n- leftovers)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = sys.argv[sys.argv.index("--tool") + 1] if "--tool" in sys.argv else HERE
EXTRACT = os.path.join(TOOL, "i18n_extract.py")
APPLY = os.path.join(TOOL, "i18n_apply.py")

SOURCE = 'x = 1\n# комментарий\ns = "строка"\nkeep("pilot_wake_damaged")\n'

def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def _dump(path):
    proc = _run(sys.executable, EXTRACT, path)
    assert proc.returncode == 0, f"extract failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _rep_file(dump, replacements, keep=()):
    fd, name = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"replacements": [
            {"id": item["id"], "text": item["text"], "replacement": rep}
            for item, rep in zip(dump, replacements)], "keep": list(keep)},
            handle, ensure_ascii=False)
    return name


def _write(root, text):
    path = os.path.join(root, "case.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def case_stale_dump_refuses(root):
    # A line shift that keeps the SAME span texts is fine: the tool
    # re-tokenizes the live file and edits current spans. Stale means the
    # text the translation was made from is gone — that must refuse.
    path = _write(root, SOURCE)
    dump = _dump(path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SOURCE.replace("# комментарий", "# правка шла параллельно"))
    reps = _rep_file(dump, ["# EN", '"EN"'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode != 0, "stale dump must refuse"
    assert "refusing" in proc.stderr, proc.stderr
    assert "# правка шла параллельно" in open(path, encoding="utf-8").read(), \
        "refusal must leave the target untouched"


def case_span_drift_refuses(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SOURCE.replace("# комментарий", "# другой"))
    reps = _rep_file(dump, ["# EN", '"EN"'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode != 0, "drifted span must refuse"
    assert "refusing" in proc.stderr, proc.stderr


def case_keep_loss_refuses(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    reps = _rep_file(dump, ["# EN", '"EN"'], keep=['keep("pilot_wake_damaged")'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode == 0, f"legit apply failed: {proc.stderr}"
    # Zero edits with a missing keep-string: nothing to gate on except keep.
    evil = _rep_file([], [], keep=["pilot_wake_damaged_RENAMED"])
    proc = _run(sys.executable, APPLY, path, evil)
    assert proc.returncode != 0, "keep loss must refuse"
    assert "keep-string" in proc.stderr, proc.stderr


def case_keep_caught_inside_strings(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    reps = _rep_file(dump, ["# EN", '"RENAMED"'], keep=['"строка"'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode != 0, "string-value change past keep must refuse"
    assert "keep-string" in proc.stderr, proc.stderr


def case_logic_change_refuses(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    reps = _rep_file(dump, ["# EN\nx = 999", '"EN"'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode != 0, "logic change must refuse"


def case_optimize_refuses(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    reps = _rep_file(dump, ["# EN", '"EN"'])
    proc = _run(sys.executable, "-O", APPLY, path, reps)
    assert proc.returncode != 0, "-O run must refuse"
    assert "python -O" in proc.stderr, proc.stderr
    assert open(path, encoding="utf-8").read() == SOURCE, "-O must not touch the file"


def case_happy_path(root):
    path = _write(root, SOURCE)
    dump = _dump(path)
    reps = _rep_file(dump, ["# EN", '"EN"'], keep=['keep("pilot_wake_damaged")'])
    proc = _run(sys.executable, APPLY, path, reps)
    assert proc.returncode == 0, f"happy path failed: {proc.stderr}"
    body = open(path, encoding="utf-8").read()
    assert body == 'x = 1\n# EN\ns = "EN"\nkeep("pilot_wake_damaged")\n', repr(body)
    leftovers = [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".i18n-")]
    assert not leftovers, f"temp files left: {leftovers}"


CASES = [value for name, value in sorted(globals().items())
         if name.startswith("case_")]


def main():
    root = tempfile.mkdtemp(prefix="i18n-self-")
    failed = 0
    for case in CASES:
        os.makedirs(root, exist_ok=True)
        for name in os.listdir(root):
            target = os.path.join(root, name)
            os.unlink(target) if os.path.isfile(target) else None
        try:
            case(root)
            print(f"ok: {case.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {case.__name__}: {exc}")
    print(f"{len(CASES) - failed}/{len(CASES)} self-test cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
