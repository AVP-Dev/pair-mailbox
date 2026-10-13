#!/usr/bin/env python3
"""Apply translated replacements strictly inside token spans.

Usage: i18n_apply.py <file> <replacements.json>
replacements.json: {"replacements": [{id, text, replacement}], "keep": [...]}
ids address an i18n_extract.py dump of the same file; each replacement
carries the source text it was translated from.

Safety model:
  1. The file is re-read and re-tokenized on every run. Id, live span text
     and dump text must all agree; a moved file refuses without a write.
  2. Edits apply back-to-front by absolute offset, so no edit shifts another.
  3. Two gates before the write: the result must parse, and its AST with
     every string constant normalized must equal the pre-edit tree.
  4. Keep-strings (transcript kinds, state keys, CLI flags) must still be
     byte-present afterwards: the AST gate normalizes strings away and is
     blind to them by construction.
  5. The write is atomic: temporary file in the same directory + os.replace.
  6. Every guard is an explicit check. Asserts vanish under python3 -O and
     would report success over a misfire, so -O refuses outright, first.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import sys
import tempfile
import tokenize

WANT = {"STRING", "COMMENT", "FSTRING_MIDDLE", "TSTRING_MIDDLE"}


class _Normalize(ast.NodeTransformer):
    def visit_Constant(self, node):
        node = copy.copy(node)
        if isinstance(node.value, str):
            node.value = ""
        return node


def _tree_sig(tree):
    return ast.dump(_Normalize().visit(copy.deepcopy(tree)))


def _refuse(msg):
    print(f"i18n_apply: refusing: {msg}", file=sys.stderr)
    return 2


def _is_cyrillic(s):
    return any("\u0400" <= c <= "\u045f" for c in s)


def main(argv):
    if sys.flags.optimize > 0:
        return _refuse("running under python -O, which strips guards")
    if len(argv) != 3:
        print("usage: i18n_apply.py <file> <replacements.json>", file=sys.stderr)
        return 2
    path, rep_path = argv[1], argv[2]
    try:
        raw = json.loads(open(rep_path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        return _refuse(f"cannot read replacements: {exc}")
    if not isinstance(raw, dict) or not isinstance(raw.get("replacements"), list):
        return _refuse('replacements.json must hold {"replacements": [...], "keep": [...]}')
    keep = raw.get("keep", [])
    if not isinstance(keep, list) or any(not isinstance(k, str) for k in keep):
        return _refuse('"keep" must be a list of strings')
    reps = {}
    for item in raw["replacements"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) \
                or not isinstance(item.get("text"), str) \
                or not isinstance(item.get("replacement"), str):
            return _refuse("each replacement needs int id + str text + str replacement")
        reps[item["id"]] = (item["text"], item["replacement"])
    try:
        with open(path, encoding="utf-8") as handle:
            src = handle.read().splitlines(keepends=True)
    except OSError as exc:
        return _refuse(f"cannot read target: {exc}")
    try:
        before_sig = _tree_sig(ast.parse("".join(src)))
    except SyntaxError as exc:
        return _refuse(f"target does not parse: {exc}")
    try:
        with tokenize.open(path) as handle:
            toks = [t for t in tokenize.generate_tokens(handle.readline)
                    if tokenize.tok_name.get(t.type, "") in WANT]
    except (OSError, tokenize.TokenError, SyntaxError, IndentationError) as exc:
        return _refuse(f"cannot tokenize target: {exc}")
    items = [t for t in toks if _is_cyrillic(t.string)]
    if set(reps) - set(range(len(items))):
        return _refuse("replacement id out of range: dump does not match this file")
    starts = []
    pos = 0
    for line in src:
        starts.append(pos)
        pos += len(line)
    full = "".join(src)
    edits = []
    for i, tok in enumerate(items):
        if i not in reps:
            continue
        want_text, rep = reps[i]
        if tok.string != want_text:
            return _refuse(f"text mismatch at id {i}: dump stale")
        (srow, scol), (erow, ecol) = tok.start, tok.end
        start = starts[srow - 1] + scol
        end = starts[erow - 1] + ecol
        if full[start:end] != tok.string:
            return _refuse(f"span drift at id {i}: file moved")
        edits.append((start, end, rep))
    # Keep holds twice: before edits it pins the empty-edit run (otherwise a
    # rename with no other edits slips through), after edits it pins the
    # result (the AST gate below normalizes strings away and cannot see it).
    for kept in keep:
        if kept not in full:
            return _refuse(f"keep-string lost: {kept!r}")
    if not edits:
        print("applied 0 replacements, file untouched")
        return 0
    for start, end, rep in sorted(edits, key=lambda e: -e[0]):
        full = full[:start] + rep + full[end:]
    try:
        after_tree = ast.parse(full)
    except SyntaxError as exc:
        return _refuse(f"result does not parse: {exc}")
    if _tree_sig(after_tree) != before_sig:
        return _refuse("normalized AST differs: a replacement reached past its span")
    for kept in keep:
        if kept not in full:
            return _refuse(f"keep-string lost: {kept!r}")
    parent = os.path.dirname(os.path.abspath(path))
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        return _refuse(f"cannot stat target: {exc}")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".i18n-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(full)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return _refuse(f"atomic write failed: {exc}")
    print(f"applied {len(edits)} replacements, AST-identical (strings normalized)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
