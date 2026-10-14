"""Dashboard page build: snapshot + assets -> one self-sufficient HTML.

Why all in one file. The dashboard opens on a double click, and the human drags
it to another machine, drops it in a messenger, opens it from a stick. Everything
the page loads by path stops working the moment the path stops matching, and
`file://` also bans `fetch()`. So CSS and JS inline into the document as text, and
the output stays exactly one file with zero external references. Inlining is about
portability, not request savings.

Assets lie as separate files (`assets/board.css`, `assets/board.js`), not lines in
this module. Same cause as everywhere in the project: a markup line in a Python
file is checked by neither the size gate nor reviewer eyes as breakable code.

`refresh_seconds` rides into `<meta http-equiv="refresh">` and keeps the page live
with no server: the generator rewrites the file, the open page reloads itself.
Zero means "never refresh" — different modes, not one with a flag. The same number
duplicates into `data-watch-seconds` on `<body>`: the client auto-refresh pause
recreates this tag and must never invent the period.

Nothing leaves outward: zero network calls, zero `fetch`. Checked by
`test_board_render.py`.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from board_snapshot import snapshot_json

ASSETS = Path(__file__).resolve().parent / "assets"
CSS_FILE = ASSETS / "board.css"
JS_FILE = ASSETS / "board.js"
LABELS_FILE = ASSETS / "labels.js"

# Assets in execution order: the dict must appear before the client reading it
# at load. The order is set here, not in the template, or a template edit quietly
# breaks the page that loaded fine this time.
JS_ORDER = (LABELS_FILE, JS_FILE)

# Assets must never quietly turn empty: a CSS-less page reads, but looks like a
# report, and a lost `board.css` would read as "the dashboard is such".
REQUIRED_TOKENS = ("--background", "--foreground", "--card", "--muted",
                   "--border", "--destructive", "--primary")


class AssetError(RuntimeError):
    """The asset misses or never does its contract part."""


# The char past which the HTML5 parser counts the script done. The rule from the
# spec: the "</script" closing sequence ends the element when followed by tab, LF,
# FF, space, "/" or ">". Case never matters.
SCRIPT_END = "</script"
SCRIPT_END_TERMINATORS = "\t\n\f />"


def breaks_out_of_script(text: str) -> str | None:
    """The first place where `text` closes script, and the chunk around it.

    Not "does the file hold the </script substring" but exactly what the browser
    does: the sequence followed by a spec separator is sought. The difference
    matters — in JavaScript the regex with the same letter past the slash never
    breaks the page, and a substring check would ban writing perfectly working
    code in the asset.

    Escaping the problem literal is banned: with the asset holding it in a COMMENT,
    an edit stops the comment being a comment in meaning, and with it in a string
    the very error text the human reads would change. So the only honest act is
    refusing to build the page and telling which asset line is at fault.
    """
    lowered = text.lower()
    start = 0
    while True:
        found = lowered.find(SCRIPT_END, start)
        if found < 0:
            return None
        after = found + len(SCRIPT_END)
        if after < len(text) and text[after] in SCRIPT_END_TERMINATORS:
            line = text.count("\n", 0, found) + 1
            context = text[max(0, found - 40):found + 40].replace("\n", " ")
            return f"line {line}: ...{context}..."
        start = found + 1


def load_asset(path: Path, required: tuple[str, ...] = (), in_script: bool = False) -> str:
    """Read the asset, checking it sits in place and never runs empty.

    `required` lists mandatory content chunks. For CSS those are tokens from
    `docs/theme-tokens.md`: a page that lost `--border` never breaks silently but
    just turns other, and only eyes catch it. The check here turns such an edit
    into a build error.

    `in_script` means the asset inlines inside `<script>`. Then a closing sequence
    in it never reads "extra text" but the element end: the rest rides into the
    page as raw text, no DOM builds, and the human sees source instead of the
    dashboard. The browser cannot stay silent here, so the build refuses.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AssetError(f"asset unreachable: {path}: {error}") from error
    if not text.strip():
        raise AssetError(f"asset empty: {path}")
    missing = [token for token in required if token not in text]
    if missing:
        raise AssetError(f"{path.name}: missing mandatory tokens: {', '.join(missing)}")
    if in_script:
        offending = breaks_out_of_script(text)
        if offending:
            raise AssetError(
                f"{path.name}: a closing script tag inside the asset would tear "
                f"the page mid-word ({offending}). Rewrite it in words.")
    return text


def render_page(snapshot: dict[str, Any], *, refresh_seconds: int = 0,
                css: str | None = None, js: str | None = None) -> str:
    """Assemble the page. Data inlines as JSON in `type="application/json"`.

    Text runs through `json.dumps` with `<`, `>` and `&` escaped after in
    `snapshot_json`, never `html.escape` inside JSON: HTML-escaping would turn
    `&` into `&amp;`, and after JSON parsing the task text would keep the entity,
    not the ampersand.

    `css` and `js` reassign by text — the test needs it, assembling the page whole
    from small stubs and checking the build itself. An empty line counts as an
    override, not "read the asset", or the test would stub emptiness for the check
    and pass having built nothing.
    """
    css_text = load_asset(CSS_FILE, REQUIRED_TOKENS) if css is None else css
    scripts = "".join(load_asset(path, in_script=True) for path in JS_ORDER) if js is None else js
    data = snapshot_json(snapshot)
    info = snapshot.get("round") or {}
    title = f"{snapshot.get('project') or 'pair'} — round {info.get('number', '?')}"
    meta = ""
    if refresh_seconds > 0:
        meta = f'\n<meta http-equiv="refresh" content="{int(refresh_seconds)}">'

    return f"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>{meta}
<style>
{css_text}
</style>
</head>
<body data-watch-seconds="{int(refresh_seconds)}">
<header class="bar">
  <h1 id="project"></h1>
  <div class="bar-right">
    <span id="stamp" class="muted"></span>
    <button type="button" id="watch-toggle" class="btn" aria-pressed="false">pause</button>
    <button type="button" id="theme-toggle" class="btn" aria-pressed="false">light</button>
  </div>
</header>
<main id="app" aria-live="polite"></main>
<noscript>
  <p class="muted">The dashboard draws the state snapshot in JavaScript. Without it only
  this line shows — enable JS to see the pair state.</p>
</noscript>
<script id="snapshot" type="application/json">
{data}</script>
<script>
{scripts}
</script>
</body>
</html>
"""


def write_page(target: Path, snapshot: dict[str, Any], *, refresh_seconds: int = 0) -> Path:
    """Write the page whole, through a temp file and a rename.

    Atomicity here is not about beauty: the dashboard sits open in the browser at
    this very moment, and a plain write cuts the file mid-way — the page then shows
    half a snapshot and reads broken on its own. The rename either shows whole or
    never shows at all.
    """
    target = Path(target)
    page = render_page(snapshot, refresh_seconds=refresh_seconds)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(target)
    return target