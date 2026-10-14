#!/usr/bin/env python3
"""Extract Cyrillic-bearing STRING/COMMENT tokens with exact spans.

Usage: i18n_extract.py <file> [--lo N] [--hi M]
IDs are GLOBAL (whole-file numbering) even when a line range filters
the output — replacements always address the full-file dump, so an id
can never land on the wrong span. Filtered-out items are omitted from
the dump but keep their ids (ids may be non-contiguous).
Prints JSON list: [{id, type, start:[r,c], end:[r,c], text}].
"""
from __future__ import annotations

import json
import sys
import tokenize

WANT = {"STRING", "COMMENT", "FSTRING_MIDDLE", "TSTRING_MIDDLE"}


def _is_cyrillic(s):
    return any("\u0400" <= c <= "\u045f" for c in s)


def _int_arg(argv, i):
    arg = argv[i]
    if "=" in arg:
        return int(arg.split("=", 1)[1]), i + 1
    try:
        return int(argv[i + 1]), i + 2
    except (IndexError, ValueError):
        print("usage: i18n_extract.py <file> [--lo N] [--hi M]", file=sys.stderr)
        raise SystemExit(2)


def main(argv):
    if len(argv) < 2:
        print("usage: i18n_extract.py <file> [--lo N] [--hi M]", file=sys.stderr)
        return 2
    path = argv[1]
    lo, hi = 1, 10 ** 9
    i = 2
    while i < len(argv):
        arg = argv[i]
        if arg == "--lo" or arg.startswith("--lo="):
            lo, i = _int_arg(argv, i)
        elif arg == "--hi" or arg.startswith("--hi="):
            hi, i = _int_arg(argv, i)
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            return 2
    try:
        with tokenize.open(path) as handle:
            toks = list(tokenize.generate_tokens(handle.readline))
    except (OSError, tokenize.TokenError, SyntaxError, IndentationError) as exc:
        print(f"i18n_extract: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    items = []
    for tok in toks:
        if tokenize.tok_name.get(tok.type, "") not in WANT:
            continue
        (srow, _scol), (erow, _ecol) = tok.start, tok.end
        if not _is_cyrillic(tok.string):
            continue
        item = {
            "id": len(items),
            "type": tokenize.tok_name[tok.type],
            "start": list(tok.start),
            "end": list(tok.end),
            "text": tok.string,
        }
        if erow >= lo and srow <= hi:
            items.append(item)
        else:
            # Count filtered-out items so ids stay global under --lo/--hi.
            items.append(None)
    print(json.dumps([i for i in items if i is not None], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
