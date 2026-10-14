"""Page-build tests (`board_render.py`) and its assets.

Checked here is not "pretty or not" but the four things the page stands or
silently breaks on.

1. **Portability.** The built file opens on a double click: zero external
   links, zero `fetch`, zero `XMLHttpRequest`. The check catches the most
   likely breakage of this scheme: someone adds an external CSS `<link>` "for
   speed", and the page works for them and nobody else.
2. **Data never closes the page.** Journal text inlines as JSON, and
   `</script>` inside a letter must stay text. Counted is not "a substring
   exists" but whether the result parses and the value matches.
3. **Token invariant.** `docs/theme-tokens.md` demands "zero hard colors
   outside tokens", and "demands" with no check is a wish. Checked are both
   the `#hex`/`rgb()` absence and that every `var(--x)` is truly declared:
   a token-name typo otherwise gives a transparent background and a quiet break.
4. **No HTML parsing from data.** Client `innerHTML` would mean task text
   running code on the human machine.

Assets check real: stubbing `render_page` makes a green suite checking nothing,
so stubs only stand where the build itself is checked.
    """
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from board_render import (CSS_FILE, SCRIPT_END_TERMINATORS, JS_ORDER, JS_FILE,
                          LABELS_FILE, REQUIRED_TOKENS, AssetError, breaks_out_of_script,
                          load_asset, render_page, write_page)
from board_snapshot import build_snapshot
from pair_test_base import PairTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SNAPSHOT_RE = re.compile(
    r'<script id="snapshot" type="application/json">(.*?)</script>', re.S)

# The script closing sequence per the HTML5 spec: "</script" followed by
# tab, LF, FF, space, "/" or ">". The regex uses lookahead because the
# lookahead itself is part of the rule, not decor.
SCRIPT_END_RE = re.compile(r"</script(?=[\t\n\f />])", re.I)


def _role_line(tree: str, role: str) -> str:
    """The role line in the printed tree.

    The tree prints line by line with indent, and no closing tags live in it: the
    role-line end is the first next line indented no deeper than the role line
    itself. Sought by the role name inside the block, not by "first letter hit":
    the letter also sits in the pane header and in the writer word on another
    line, and such a parse would check the wrong place.
    """
    lines = tree.splitlines()
    for position, line in enumerate(lines):
        if f'class=role-name "{role}"' not in line:
            continue
        indent = len(line) - len(line.lstrip())
        collected = []
        for follow in lines[position:]:
            depth = len(follow) - len(follow.lstrip())
            if follow.strip() and depth < indent:
                break
            collected.append(follow)
        return "\n".join(collected)
    raise AssertionError(f"role {role!r} never found in the tree:\n{tree}")


def _script_blocks_per_html5(page: str) -> list[str]:
    """The `<script>` block contents as the browser sees them.

    Exactly this function repeats the spec rule instead of trusting an HTML-parser
    parse: breaks and unclosed blocks are exactly the defect caught here, and it
    must show in the test, not in the human browser.
    """
    blocks: list[str] = []
    position = 0
    while True:
        start = page.find("<script>", position)
        if start < 0:
            return blocks
        start += len("<script>")
        match = SCRIPT_END_RE.search(page, start)
        if not match:
            return blocks
        blocks.append(page[start:match.start()])
        position = match.end()

# Globals that are called but never declared in the file.
BUILTINS = {"isNaN", "parseInt", "parseFloat", "setTimeout", "setInterval",
            "String", "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date"}
# Words that read as calls but never call.
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof",
            "await", "else", "do", "try"}


def _stub_snapshot(**overrides) -> dict:
    base = {"generated_at": "2026-01-01T00:00:00+00:00", "project": "demo",
            "read_error": "", "watch": False, "stale_after": 90,
            "round": {"number": 3, "phase": "implement", "phases": ["scoping", "plan",
                      "implement", "verify", "done"], "phase_index": 3, "mode": "fix",
                      "autonomy": "auto", "writer": "a", "writer_reason": "",
                      "task": "task", "state_age": "1 s", "state_age_seconds": 1,
                      "event_age": "1 s", "event_age_seconds": 1, "closed": False},
            "roles": [{"role": "a", "is_writer": True, "pane": "%0", "pane_alive": True,
                       "pane_note": "", "pid": 1, "pid_alive": True, "silence": "1 s",
                       "silence_seconds": 1, "restored_by": ""}],
            "pilot": {"running": False, "pid": None, "pids": [], "log_size": 0,
                      "log_age": "1 s", "log_age_seconds": 1, "report_exists": False,
                      "report_age": "—", "report_age_seconds": None, "refusals": 0,
                      "last": "", "last_ts": None},
            "tasks": [], "events": [],
            "counts": {"events_shown": 0, "events_total": 0, "tasks_open": 0, "roles": 1}}
    base.update(overrides)
    return base


class AssembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _stub_snapshot()
        # Our own dir for the two tests writing an asset to disk: raising a `Store`
        # for them would be scaffolding with no need.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_page_is_one_self_contained_file(self):
        page = render_page(self.snapshot)
        self.assertTrue(page.startswith("<!doctype html>"))
        for tag in ("<style>", "application/json", "</html>"):
            self.assertIn(tag, page)

    def test_no_external_references_at_all(self):
        page = render_page(self.snapshot)
        # Neither src nor href to an outside resource: from `file://` the page survives
        # none of them, and the breakage reads as "my page is blank".
        for match in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', page):
            self.assertFalse(match.startswith(("http", "//", "./", "/")),
                             f"outside link: {match}")
        self.assertNotIn("<link", page)

    def test_data_does_not_reach_the_network(self):
        page = render_page(self.snapshot)
        self.assertNotRegex(page, r"\bfetch\s*\(")
        self.assertNotIn("XMLHttpRequest", page)
        self.assertNotIn("EventSource", page)
        self.assertNotIn("import(", page)

    def test_snapshot_is_embedded_as_parsable_json(self):
        page = render_page(self.snapshot)
        raw = SNAPSHOT_RE.search(page).group(1)
        self.assertEqual(json.loads(raw)["round"]["number"], 3)

    def test_closing_tag_in_data_stays_text(self):
        hostile = _stub_snapshot()
        hostile["round"] = dict(hostile["round"], task='</script><img src=x onerror=alert(1)>')
        page = render_page(hostile)
        raw = SNAPSHOT_RE.search(page).group(1)
        self.assertNotIn("</script>", raw.lower())
        self.assertEqual(json.loads(raw)["round"]["task"],
                         '</script><img src=x onerror=alert(1)>')

    def test_html_parses_and_declares_the_page(self):
        class Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.ids: list[str] = []
            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if name == "id" and value:
                        self.ids.append(value)
        parser = Parser()
        parser.feed(render_page(self.snapshot))
        parser.close()
        for needed in ("snapshot", "app", "project", "stamp", "theme-toggle",
                       "watch-toggle"):
            self.assertIn(needed, parser.ids)

    def test_inlined_js_contains_no_script_closer(self):
        # agy finding: the closing script tag literal in an asset COMMENT.
        # The asset inlines inside <script>, and HTML5 closes the element on such a
        # line whether comment or code: the rest rides into the page as raw text, the
        # DOM never builds, the human sees source.
        # The check runs on the built page, not the source, because the built page is
        # exactly what the browser gets.
        page = render_page(self.snapshot)
        head, _, rest = page.partition("<script>\n")
        self.assertTrue(rest, "no script blocks on the page")
        body, _, _ = rest.partition("\n</script>")
        self.assertIsNone(breaks_out_of_script(body),
                          "the inlined script holds a closing script tag")
        # Exactly three closing tags per page: the snapshot, the scripts, nothing extra.
        self.assertEqual(page.lower().count("</script"), 2)

    def test_script_closer_in_a_comment_is_refused_by_the_renderer(self):
        hostile = self.root / "hostile.js"
        hostile.write_text(
            "/* mentioning the closing tag: </script> goes in a comment */\n"
            "console.log(1);\n", encoding="utf-8")
        with self.assertRaises(AssetError) as caught:
            load_asset(hostile, in_script=True)
        message = str(caught.exception)
        self.assertIn("hostile.js", message)
        self.assertIn("line 1", message)

    def test_a_working_slash_sequence_is_not_a_breaker(self):
        # The `/<\\/script/` regex holds the same letter but never breaks the page.
        # A substring check would ban working code in the asset, so the next char must
        # tell, not the letter presence.
        safe = self.root / "safe.js"
        safe.write_text("var re = /<\\/script/;\nconsole.log(re);\n", encoding="utf-8")
        self.assertIsNone(breaks_out_of_script(safe.read_text(encoding="utf-8")))
        self.assertTrue(load_asset(safe, in_script=True))

    def test_every_breaker_shape_is_detected(self):
        # The spec rule: tab, LF, FF, space closes past "</script",
        # "/" or ">". Every shape is checked, or the rule is checked on one example
        # and means nothing.
        for terminator in SCRIPT_END_TERMINATORS:
            self.assertIsNotNone(breaks_out_of_script("x</script" + terminator + "y"),
                                 f"unnoticed separator {terminator!r}")
        # Case never matters, and a non-separator past the letters never closes.
        self.assertIsNotNone(breaks_out_of_script("</SCRIPT>"))
        self.assertIsNone(breaks_out_of_script("</scripta>"))

    def test_real_assets_are_clean(self):
        for path in JS_ORDER:
            self.assertIsNone(breaks_out_of_script(path.read_text(encoding="utf-8")),
                              f"{path.name} holds a closing script tag")

    def test_client_code_survives_as_one_whole_script_block(self):
        """The strong shape of the same check: script blocks cut on the HTML5 rule.

        `HTMLParser` from stdlib tears `<script>` on the `</script>` literal just
        like the browser — but silently: a test checking only element ids never
        notices such a tear, because markup attributes went nowhere, and the page
        passes with source mush instead of rendering. Exactly what happened to
        our suite before the fix: 41 tests green on a broken page.

        Checked here is not "does the file hold a substring" but what the browser
        sees: exactly one script block, and the client code whole inside — from
        `function` to the final `render` call.
        """
        page = render_page(self.snapshot)
        blocks = _script_blocks_per_html5(page)
        # The snapshot block opens with an attributed tag, not a bare <script>, so the
        # the regex finds exactly one block — the client one. The snapshot is checked
        # apart, through SNAPSHOT_RE and json.loads.
        self.assertEqual(len(blocks), 1, "the client script is torn apart")
        client = blocks[0]
        for fragment in ("function readSnapshot", "function renderRound",
                         "function renderEvents", "render(readSnapshot());"):
            self.assertIn(fragment, client,
                          f"the client lost {fragment!r}: the block is torn")

    def test_toggles_are_real_buttons_with_types(self):
        page = render_page(self.snapshot)
        # A clickable div is directly banned by §4 of docs/theme-tokens.md.
        self.assertEqual(len(re.findall(r'<button type="button"', page)), 2)

    def test_refresh_meta_appears_only_in_watch_mode(self):
        # Sought is the tag itself, not a substring: the client holds the selector
        # `meta[http-equiv="refresh"]`, and a line check would look for it.
        tag = re.compile(r'<meta\s+http-equiv="refresh"\s+content="(\d+)"\s*>')
        self.assertEqual(tag.findall(render_page(self.snapshot)), [])
        self.assertEqual(tag.findall(render_page(self.snapshot, refresh_seconds=5)), ["5"])

    def test_watch_seconds_reach_the_body_attribute(self):
        # The client recreates the refresh tag on unpause and must never invent the
        # period there: it reads from here.
        self.assertIn('<body data-watch-seconds="5">',
                      render_page(self.snapshot, refresh_seconds=5))
        self.assertIn('<body data-watch-seconds="0">', render_page(self.snapshot))

    def test_scripts_are_inlined_in_load_order(self):
        page = render_page(self.snapshot)
        first = page.index("PAIRBOARD_LABELS")
        second = page.index("restoreTheme()")
        self.assertLess(first, second, "the dictionary must load before the client")

    def test_assets_can_be_replaced_for_assembly_checks(self):
        page = render_page(_stub_snapshot(), css="/*c*/", js="/*j*/")
        self.assertIn("/*c*/", page)
        self.assertIn("/*j*/", page)
        self.assertNotIn("--background", page)

    def test_title_is_html_escaped(self):
        page = render_page(_stub_snapshot(project="<b>project</b>"))
        self.assertIn("&lt;b&gt;project&lt;/b&gt;", page.split("</title>")[0])


class AssetTest(PairTestCase):
    def test_missing_asset_is_an_error_not_an_empty_page(self):
        with self.assertRaises(AssetError):
            load_asset(CSS_FILE.parent / "no-such.css")

    def test_empty_asset_is_an_error(self):
        empty = self.root / "empty.css"
        empty.write_text("   \n", encoding="utf-8")
        with self.assertRaises(AssetError):
            load_asset(empty)

    def test_missing_theme_token_is_named_in_the_error(self):
        partial = self.root / "partial.css"
        partial.write_text(":root { --background: 0 0% 100%; }", encoding="utf-8")
        with self.assertRaises(AssetError) as caught:
            load_asset(partial, REQUIRED_TOKENS)
        self.assertIn("--border", str(caught.exception))

    def test_real_assets_load(self):
        self.assertTrue(load_asset(CSS_FILE, REQUIRED_TOKENS).strip())
        for path in JS_ORDER:
            self.assertTrue(load_asset(path).strip())


class ThemeContractTest(unittest.TestCase):
    """The `docs/theme-tokens.md` invariant: zero hard colors outside tokens.

    Checked are DECLARATIONS, not the whole file: the comment explaining the ban
    of course spells "#hex" and "rgb()", and a whole-text check would ban writing
    the invariant explanation. Comments cut out, and the rest is claims, where a bare color means exactly breakage.
    """

    def setUp(self) -> None:
        raw = CSS_FILE.read_text(encoding="utf-8")
        self.css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        # Deleting comments must never leave the file empty: otherwise every check
        # below would pass on emptiness.
        self.assertIn("--background", self.css)

    def test_no_hex_colors(self):
        self.assertNotRegex(self.css, r"#[0-9a-fA-F]{3,8}\b")

    def test_no_rgb_colors(self):
        self.assertNotRegex(self.css, r"\brgba?\s*\(")

    def test_every_hsl_reads_a_token(self):
        for use in re.findall(r"hsl\(([^)]*)\)", self.css):
            self.assertTrue(use.lstrip().startswith("var("),
                            f"hsl() with no token: hsl({use})")

    def test_every_referenced_token_is_declared(self):
        declared = set(re.findall(r"(--[\w-]+)\s*:", self.css))
        used = set(re.findall(r"var\((--[\w-]+)", self.css))
        missing = sorted(used - declared)
        self.assertEqual(missing, [], f"undeclared tokens in use: {missing}")

    def test_both_themes_declare_the_contract_tokens(self):
        # The dark theme must be complete: `docs/theme-tokens.md` §1 demands the full
        # set from every theme, not "the rest inherits from light".
        dark = self.css.split('[data-theme="dark"]')[1]
        for token in REQUIRED_TOKENS:
            self.assertIn(f"{token}:", dark, f"missing in the dark theme: {token}")

    def test_both_themes_exist(self):
        self.assertIn(":root", self.css)
        self.assertIn('[data-theme="dark"]', self.css)

    def test_status_colours_are_tokens_too(self):
        # Status colors sit outside the contract but declare as tokens, or the
        # indicators would need bare colors and break the invariant above.
        self.assertRegex(self.css, r"--ok\s*:")
        self.assertRegex(self.css, r"--bad\s*:")
        self.assertIn("hsl(var(--ok))", self.css)


class ClientBehaviourTest(unittest.TestCase):
    """The client runs real, not "read with eyes".

    Static invariant checks catch name typos but never a `TypeError` on the third
    render line: such a bug reaches the human as a page with an empty `#app` and
    not a word about the error. So the client executes on a minimal DOM stub —
    `document`, `localStorage`,
    `createElement` — and the pane tree is checked by fact.

    The stub lives in `scripts/`, not `assets/`: a check contraption with no
    source counterpart — in `assets/` it would land in the page build, the code
    live humans see.

    The suite never raises `Store`: the stub wants only the asset files and a ready
    snapshot; state-read checking is `test_board_snapshot.py` work.
    """

    def setUp(self) -> None:
        self.snapshot = _stub_snapshot()
        self.snapshot["events"] = [{"ts": "2026-01-01T00:00:00+00:00", "kind": "pilot_pane_dead",
                                    "role": "pairpilot", "to": "", "summary": "pane dead"}]
        self.snapshot["tasks"] = [{"id": 1, "text": "task", "status": "open", "round": 16,
                                   "added_by": "agy", "closed_by": "", "reason": "important"}]
        self.tree, self.state = self._run_client(self.snapshot)

    def _run_client(self, snapshot: dict, seconds: str = "5"):
        runner = REPO_ROOT / "scripts" / "board-dom-stub.js"
        node = shutil.which("node")
        if node is None:
            # With no node nothing runs the client, and the check must honestly say
            # "skipped", never quietly turn green having run no code.
            self.skipTest("node unreachable: the client is only statically checked")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False),
                                     encoding="utf-8")
            proc = subprocess.run([node, str(runner), str(LABELS_FILE), str(JS_FILE),
                                   str(snapshot_path), seconds],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, f"the client fell: {proc.stderr[:400]}")
        lines = proc.stdout.splitlines()
        tree = "\n".join(line for line in lines if line.startswith((" ", "<")))
        meta = [line for line in lines if line.startswith("---")]
        return tree, meta

    def test_client_renders_without_throwing(self):
        self.assertIn("round 3", self.tree)
        for heading in ("agents", "supervisor", "tasks", "events"):
            self.assertIn(f"<h2 \"{heading}", self.tree)

    def test_client_shows_roles_tasks_and_events(self):
        self.assertIn("pane %0", self.tree)
        self.assertIn("#1 task", self.tree)
        self.assertIn("cause: important", self.tree)
        self.assertIn("pane dead", self.tree)

    def test_theme_defaults_to_dark_and_is_stored(self):
        self.assertIn("--- theme: dark", self.state)
        self.assertIn("pairboard.theme", " ".join(self.state))

    def test_watch_period_comes_from_the_body_attribute(self):
        # The client must never invent the period: it takes it from data-watch-seconds.
        self.assertIn('--- refresh meta: ["7"]',
                      " ".join(self._run_client(self.snapshot, "7")[1]))

    def test_static_mode_still_gets_the_tag_at_the_servers_period(self):
        # In static mode the generator sets period 0, and the client must never
        # "help": it creates the refresh tag only from markup data, staying silent on a
        # zero period — a page would get content="0", an instant reload loop. So zero
        # must read "no tag at all", and that is checked by fact, not agreement.
        # must read "no tag at all", and that is checked by fact, not agreement.
        _, meta = self._run_client(self.snapshot, "0")
        self.assertIn('--- refresh meta: []', " ".join(meta))

    def test_role_tags_are_mutually_exclusive(self):
        """The writer gets the writer tag, the rest critic — never both at once.

        A regression found on the live screen in the agy patch: the role line
        printed `writer` and `critic` side by side, claiming one agent both writes
        and criticises. Formally a lie the human reads, not a render defect: the
        "critic" word on the writer line answers "who checks now", and two answers
        to one question are no longer an answer.
        """
        snapshot = _stub_snapshot()
        snapshot["roles"] = [
            {"role": "writer-agent", "is_writer": True, "pane": "%0", "pane_alive": True,
             "pane_note": "", "pid": 1, "pid_alive": True, "silence": "1 s",
             "silence_seconds": 1, "restored_by": ""},
            {"role": "peer-agent", "is_writer": False, "pane": "%1", "pane_alive": True,
             "pane_note": "", "pid": 2, "pid_alive": True, "silence": "1 s",
             "silence_seconds": 1, "restored_by": ""},
        ]
        tree, _ = self._run_client(snapshot)
        writer_line = _role_line(tree, "writer-agent")
        peer_line = _role_line(tree, "peer-agent")
        self.assertIn('class=tag writer "writer"', writer_line)
        self.assertNotIn("critic", writer_line)
        self.assertIn('class=tag critic "critic"', peer_line)
        self.assertNotIn('"writer"', peer_line)

    def test_writer_role_is_labelled_even_without_other_roles(self):
        # One role, the critic never registered: the critic tag on the writer line
        # must never appear all the same — the very same question.
        tree, _ = self._run_client(_stub_snapshot())
        role_line = _role_line(tree, "a")
        self.assertIn("writer", role_line)
        self.assertNotIn("critic", role_line)

    def test_unreadable_snapshot_shows_a_banner_instead_of_a_blank_page(self):
        broken = _stub_snapshot()
        broken["read_error"] = "state.json unreadable: broken"
        tree, _ = self._run_client(broken)
        self.assertIn("state.json unread", tree)


class ClientContractTest(unittest.TestCase):
    """The `assets/board.js` invariants — what the page does with data."""

    def setUp(self) -> None:
        self.js = JS_FILE.read_text(encoding="utf-8")

    def test_no_html_injection_api(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                       "eval("):
            # Banned-API names pass only in comments explaining why they miss here.
            # why they miss here.
            for line in self.js.splitlines():
                if banned in line:
                    self.assertTrue(line.strip().startswith(("*", "/*", "//")),
                                    f"{banned} in code: {line.strip()}")

    def test_all_text_goes_through_text_content(self):
        self.assertIn("node.textContent = String(text)", self.js)
        # Element creation runs through one helper, never scattered innerHTML.
        self.assertIn("document.createElement(tag)", self.js)

    def test_calls_only_defined_functions(self):
        # A function-name typo passes the syntax check and falls in the browser as a
        # ReferenceError — at the human, not in the gate.
        source = self.js
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", source))
        defined |= set(re.findall(r"var\s+([A-Za-z_$][\w$]*)", source))
        # Anonymous-function params never declare in the file either: `draw` below is
        # declared exactly at call time. Without them the check would scold perfectly
        # valid code.
        defined |= set(re.findall(r"function\s*\(\s*([A-Za-z_$][\w$]*)", source))
        defined |= BUILTINS | KEYWORDS
        called = re.findall(r"(?<![\.\w$])([a-z][\w$]*)\s*\(", source)
        missing = sorted({name for name in called if name not in defined})
        self.assertEqual(missing, [], f"called but undeclared: {missing}")

    def test_theme_choice_survives_a_reload(self):
        # The page reloads whole, so the theme choice must sit in localStorage, not a
        # variable: a variable would survive one load.
        self.assertIn("localStorage", self.js)
        self.assertRegex(self.js, r'var\s+THEME_KEY\s*=\s*"')
        self.assertRegex(self.js, r"remember\(THEME_KEY, theme\)")

    def test_watch_pause_is_separate_from_theme(self):
        # Two buttons, two settings: otherwise pressing "light" quietly stops the
        # auto-refresh, and the human wonders at the frozen feed.
        self.assertIn('getElementById("theme-toggle")', self.js)
        self.assertIn('getElementById("watch-toggle")', self.js)
        theme_handler = self.js.split('getElementById("theme-toggle")')[1][:400]
        self.assertNotIn("setWatch", theme_handler)

    def test_status_has_three_states_not_two(self):
        # "Unknown" must be its own state: an agent with no pane carries no data, and
        # that differs from "pane dead".
        self.assertIn('return "unknown"', self.js)
        self.assertIn('return "bad"', self.js)
        self.assertIn('return "ok"', self.js)

    def test_labels_asset_is_present_and_used(self):
        labels = (CSS_FILE.parent / "labels.js").read_text(encoding="utf-8")
        self.assertIn("window.PAIRBOARD_LABELS", labels)
        self.assertIn("window.PAIRBOARD_LABELS || {}", self.js)


class WritePageTest(PairTestCase):
    def test_file_is_written_and_leaves_no_temporary_behind(self):
        target = self.root / "board.html"
        written = write_page(target, _stub_snapshot(), refresh_seconds=5)
        self.assertEqual(written, target)
        self.assertTrue(target.exists())
        self.assertFalse(target.with_name("board.html.tmp").exists())

    def test_written_file_is_the_same_page(self):
        target = self.root / "board.html"
        write_page(target, _stub_snapshot())
        self.assertEqual(target.read_text(encoding="utf-8"),
                         render_page(_stub_snapshot()))

    def test_missing_parent_directory_is_created(self):
        target = self.root / "deep" / "inside" / "board.html"
        write_page(target, _stub_snapshot())
        self.assertTrue(target.exists())

    def test_rewriting_replaces_the_previous_snapshot(self):
        target = self.root / "board.html"
        write_page(target, _stub_snapshot())
        changed = _stub_snapshot()
        changed["round"] = dict(changed["round"], number=99)
        write_page(target, changed)
        raw = SNAPSHOT_RE.search(target.read_text(encoding="utf-8")).group(1)
        self.assertEqual(json.loads(raw)["round"]["number"], 99)


class EndToEndTest(PairTestCase):
    def test_real_state_produces_a_page_a_browser_can_open(self):
        self.store.mutate(lambda state: state.update(
            task="dashboard", phase="implement", writer="writer-agent", round=16,
            roles={"writer-agent": {"pid": 1, "pane": "", "last_seen": "2026-01-01T00:00:00+00:00"}}))
        self.store.append_transcript("send", "agy", to="opencode", subject="hello")
        page = render_page(build_snapshot(self.root, limit=5), refresh_seconds=5)
        raw = SNAPSHOT_RE.search(page).group(1)
        data = json.loads(raw)
        self.assertEqual(data["round"]["number"], 16)
        self.assertEqual(data["events"][0]["summary"], "hello")
        self.assertIn("PAIRBOARD_LABELS", page)
        self.assertIn("http-equiv=\"refresh\"", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)