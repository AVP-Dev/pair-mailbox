#!/usr/bin/env python3
"""Zero-touch: installs pair-mailbox into every detected CLI and verifies that
MCP actually comes up. Writing the config is not enough — we have twice caught
silent format refusals, so the check is real: launch exactly the command written
into the config and shake hands.

  ./install.sh              install everything found and verify
  ./install.sh codex        one profile only
  ./install.sh --check      verify only, change nothing
  ./install.sh --list       show profiles
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
SERVER = ROOT / "mcp/pair_mailbox/serve.sh"
SKILLS = ROOT / "skills"
MARK = "<!-- pair-mailbox -->"

NOTE = """
## Pair Work

A peer agent is available nearby via the `pair_*` tools.
Before taking on a task, call `pair_state`, then `pair_claim`.

- Claimed authority — you are the writer: edit files, run tests, commit, open PRs.
- Did not claim — you are the critic: read, argue, write findings to `pair_note`, never edit files.

The round can be reopened either way: `pair_advance(\"implement\")` rolls the phase back.
That needs no write authority. If the writer went silent — tell the user.

If the peer is silent — work alone, that is not a blocker.
Full rules: `docs/PROTOCOL.md` in the pair-mailbox repository.
"""


def expand(p):
    return Path(p).expanduser()


def binary(name):
    return shutil.which(name)


def present(profile):
    "A CLI counts as installed when its binary or config dir stands."
    spec = profile["mcp"]
    names = {"opencode": "opencode", "agy": "agy", "codex": "codex", "claude": "claude",
             "pi": "pi", "omp": "omp"}
    key = next((k for k, v in PROFILES.items() if v is profile), "")
    bin_name = profile.get("binary", names.get(key, key))
    if binary(bin_name):
        return True
    cfg = expand(spec["path"])
    return cfg.parent.exists()


def argv_from_toml(text):
    "The minimum needed from the [mcp_servers.pair] block: command and args."
    cmd, args, inside = None, [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            inside = s == "[mcp_servers.pair]"
            continue
        if not inside or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key, val = key.strip(), val.strip().strip('"')
        if key == "command":
            cmd = val
        elif key == "args":
            args = [a.strip().strip('"') for a in val.strip("[]").split(",") if a.strip()]
    if not cmd:
        raise KeyError("command")
    return [cmd] + args


def strip_jsonc(text):
    """Strip comments and trailing commas, never touching string literals.

    .jsonc is JSON with comments, while a brace or slash inside a string must stay
    a string. A regex with no quote parsing cuts "https://..." URLs in half, so a
    small state machine stands here, not a regex replace.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_jsonc(text):
    """Parse .jsonc: first trying plain JSON, then stripping comments. The second path
    serves files that hold comments."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r",(\s*[}\]])", r"\1", strip_jsonc(text))
    return json.loads(cleaned)

def _json_skip_value(text, i):
    """A JSON value end from position i: strings, comments and trailing commas count,
    or the block replace cuts a URL or a // inside a string."""
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if text.startswith("//", i):
        nl = text.find("\n", i)
        return _json_skip_value(text, n if nl == -1 else nl + 1)
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return _json_skip_value(text, n if end == -1 else end + 2)
    if i < n and text[i] in "{[":
        open_c, close_c = text[i], "}" if text[i] == "{" else "]"
        depth, j, instr, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif text.startswith("//", j):
                nl = text.find("\n", j)
                j = n if nl == -1 else nl
            elif text.startswith("/*", j):
                end = text.find("*/", j + 2)
                j = n - 1 if end == -1 else end + 1
            elif ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    if i < n and text[i] == '"':
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                return j + 1
            j += 1
        return n
    j = i
    while j < n and text[j] not in ",}]":
        j += 1
    return j


def _json_find_key(text, start, end, key):
    """A key value range inside an object [start, end): the key position outside
    strings and comments plus its value end. No key means None."""
    i = start
    while i < end:
        if text[i] == '"':
            j = i + 1
            while j < end:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            word = text[i + 1:j]
            k = j + 1
            while k < end and text[k] in " \t\r\n":
                k += 1
            if k < end and text[k] == ":" and word == key:
                v = _json_skip_value(text, k + 1)
                return (i, v)
            i = j + 1
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = end if nl == -1 or nl >= end else nl + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = end if close == -1 or close + 2 >= end else close + 2
            continue
        i += 1
    return None


def splice_json_block(text, root_key, server_key, entry_json):
    """Insert the entry into the raw config text, touching nothing else.

    json.dumps would rewrite the file whole wiping human comments, so only the
    mcp.<server> value range patches: replacing when present, inserting before the
    object closing brace when missing. Returns (new_text, action):
    replaced|inserted|created."""
    stripped = text.strip()
    if not stripped:
        return ('{\n  "%s": {\n    "%s": %s\n  }\n}\n' % (root_key, server_key, entry_json), "created")
    found = _json_find_key(text, 0, len(text), root_key)
    if found is None:
        # Inserting before the root closing brace in raw text: a trailing comma before it
        # drops, or ",," results with broken JSON.
        close = text.rfind("}")
        inner = text[:close].rstrip()
        if inner.endswith(","):
            inner = inner[:-1]
        sep = "," if inner.strip().strip("{}").strip() else ""
        tail = text[close:]
        return ("%s%s\n  \"%s\": {\n    \"%s\": %s\n  }\n%s" % (inner, sep, root_key, server_key, entry_json, tail), "inserted-root")
    _, root_end = found
    inner_start = text.find("{", found[0] + len(root_key) + 2)
    inner_end = root_end - 1
    pair = _json_find_key(text, inner_start + 1, inner_end, server_key)
    if pair is not None:
        return (text[:pair[0]] + '"%s": %s' % (server_key, entry_json) + text[pair[1]:], "replaced")
    before = text[inner_start + 1:inner_end]
    # A trailing comma before } drops, or inserting gives ",," with broken JSON.
    head = text[:inner_end].rstrip()
    if head.endswith(","):
        head = head[:-1]
    sep = "" if not head[inner_start + 1:].strip() else ","
    return (head + "%s\n    \"%s\": %s\n" % (sep, server_key, entry_json) + text[inner_end:], "inserted")

def write_mcp(profile, role):
    spec = profile["mcp"]
    path = expand(spec["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # No --state by design: pinning an absolute project path into shared configs
    # (~/.config) is banned — all projects would share one state. Resolving at
    # runtime: walk-up to .pair/state.json in serve.sh and extract_state.
    argv = ["sh", str(SERVER), role]
    path.parent.mkdir(parents=True, exist_ok=True)

    if spec["format"] == "toml":
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines, skip = [], False
        for line in text.splitlines():
            if line.strip().startswith("[mcp_servers.pair]"):
                skip = True
                continue
            if skip:
                if line.strip().startswith("["):
                    skip = False
                else:
                    continue
            lines.append(line)
        block = '[mcp_servers.pair]\ncommand = "sh"\nargs = ["%s", "%s"]\n' % (SERVER, role)
        path.write_text("\n".join(lines).rstrip() + "\n\n" + block, encoding="utf-8")
        return argv

    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    if raw.strip():
        try:
            parse_jsonc(raw)
        except Exception as exc:
            print("    !! unreadable: " + str(exc))
            print("       " + str(path) + " left untouched. Edit the config by hand or")
            print("       delete the mcp.pair block from it and run the install again.")
            return None
    entry = dict(spec["entry"])
    if isinstance(entry.get("command"), list):
        entry["command"] = argv
    else:
        entry["command"], entry["args"] = argv[0], argv[1:]
    entry_json = json.dumps(entry, indent=2, ensure_ascii=False)
    # Editing in place, never a whole-file json.dumps: or human comments and
    # formatting wipe on every install.
    backup = path.with_suffix(path.suffix + ".bak")
    if raw.strip():
        backup.write_text(raw, encoding="utf-8")
    new_text, _action = splice_json_block(raw, spec["root_key"], spec.get("server_key", "pair"), entry_json)
    path.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")
    return argv


def write_skills(profile):
    root = expand(profile["skills_dir"])
    out = []
    for d in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        t = root / d.name
        t.mkdir(parents=True, exist_ok=True)
        shutil.copy(d / "SKILL.md", t / "SKILL.md")
        out.append(d.name)
    return out


def replace_owned_block(text, body):
    """Replace ONLY our block, keeping all past the marker.

    The old code ran `text.split(MARK)[0]`, deleting all the human wrote past the
    marker — silent, every launch. Deleting foreign-file content is exactly what
    PROTOCOL §12 demands asking for, so the tail stays, plus a copy snapshots
    before overwriting.
    """
    head, _, tail = text.partition(MARK)
    kept = tail[len(NOTE):] if tail.startswith(NOTE) else tail
    return head.rstrip() + "\n" + MARK + NOTE + ("\n" + kept.lstrip("\n") if kept.strip() else "")


def write_instructions(profile):
    path = expand(profile["instructions"])
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK in text:
        updated = replace_owned_block(text, NOTE)
    else:
        # The same join as replace_owned_block, or the first install gives an empty line
        # before the marker, the second drops it — the file changing every launch while
        # promising idempotence in the header.
        head = text if not text.strip() else text.rstrip()
        updated = head + ("\n" if head else "") + MARK + NOTE
    if updated == text:
        return "unchanged"
    # A copy before overwriting: the file belongs to the human, not us.
    try:
        path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
    except OSError as error:
        print("    !! could not save a backup copy: " + str(error))
        return "NOT WRITTEN (no backup copy)"
    path.write_text(updated, encoding="utf-8")
    return "updated"


def verify(argv):
    """Raising the server exactly as the CLI will, shaking hands.

    The check isolates from the real roster — no tidiness but a demand. Found live:
    `verify` raised `serve.sh <role>` with an inherited env and no explicit state,
    writing into the REAL `.pair/state.json` of the project install.sh launched
    from, inheriting the launching-process `TMUX_PANE`.

    Past that it hit twice. The `opencode` pane rewrote to the `agy` pane address —
    the test run went from his window — and past the session-address move the
    rewrite turned canonical too: the role took a foreign pane firmly, not for one
    run. Plus the roster took the check-process pid, dying at once, so the role
    read "dead at a live window".

    A transport check holds no right to change state. So state is temporary, with
    all agent-making env cleaned out.
    """
    req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "pair-install"}},
    }) + "\n"
    scratch = tempfile.mkdtemp(prefix="pair-verify-")
    # --state in argv (the explicit project pin) beats PAIR_STATE, so for the check it
    # rewrites to a temp one: or the handshake writes into the live roster (the pin
    # regression, caught by TestVerifyDoesNotTouchTheRoster).
    clean = []
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a == "--state":
            skip_next = True
            continue
        if a.startswith("--state="):
            continue
        clean.append(a)
    argv = clean + ["--state", scratch]
    try:
        env = {k: v for k, v in os.environ.items()
               if k not in ("PAIR_STATE", "PAIR_ROLE", "PAIR_DISABLE_RING")}
        env["PAIR_STATE"] = scratch
        # TMUX_PANE means "who I am", and the check needs none of it: with no it the
        # server honestly never counts itself living in a pane. TMUX leaves with it, or
        # the server would decide it sits inside tmux by env vars alone.
        for name in ("TMUX_PANE", "TMUX"):
            env.pop(name, None)
        proc = subprocess.run(argv, input=req, capture_output=True, text=True,
                              timeout=15, env=env)
    except Exception as exc:
        return False, "launch failed: " + str(exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reply = json.loads(line)
        except Exception:
            continue
        info = reply.get("result", {}).get("serverInfo", {})
        if info.get("name") == "pair-mailbox":
            return True, info.get("version", "?")
    return False, "the server did not answer as pair-mailbox"


def write_native_permissions(name):
    """The CLI native permission layer: an allow-list instead of human clicking.

    codex/pi/omp own no native allow/deny lists (checked in round 54: codex owns only
    approval_policy, pi trusts files), so the installer never touches them: they stay
    on the pilot (section 13b). A decision, not an undone bit.
    """
    if name not in ("opencode", "agy", "claude"):
        return None
    sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
    try:
        import autoperms
        return autoperms.write_native_perms(name, ROOT, Path.home())
    except Exception as exc:  # noqa: BLE001 - install must never fall over rights
        print("    permissions: NOT WRITTEN (" + str(exc) + ") — the agent will ask")
        return None


def main():

    # The Python version checks BEFORE install, not as a test batch at the end.
    # Install on macOS gave 210 import errors, none of them saying the system
    # python3 there is 3.9 while the code targets 3.8+.
    if sys.version_info < (3, 8):
        print("need python3 3.8 or newer, but running " + sys.version.split()[0])
        print("install newer and retry: brew install python   (check: python3 -V)")
        return 1
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--list" in sys.argv:
        for k, v in PROFILES.items():
            if not k.startswith("_"):
                print("%-10s %-20s %s" % (k, v["label"], "found" if present(v) else "missing"))
        return 0

    print(r"""
  ____       _        __  __       _ _ _               
 |  _ \ __ _(_)_ __  |  \/  | __ _(_) | |__   _____  __
 | |_) / _` | | '__| | |\/| |/ _` | | | '_ \ / _ \ \/ /
 |  __/ (_| | | |    | |  | | (_| | | | |_) | (_) >  < 
 |_|   \__,_|_|_|    |_|  |_|\__,_|_|_|_.__/ \___/_/\_\

 Autonomous Two-Agent Pair Programming over MCP & tmux
 Created by Aliaksei Patskevich (AVP-Dev) • https://avpdev.com
 GitHub: https://github.com/AVP-Dev/pair-mailbox
""".strip())

    check_only = "--check" in sys.argv
    targets = args or [k for k, v in PROFILES.items() if not k.startswith("_") and present(v)]

    if not targets:
        print("no known CLI found")
        return 1

    failed = []
    for name in targets:
        profile = PROFILES.get(name)
        if not profile:
            print("no profile: " + name)
            failed.append(name)
            continue
        print("\n" + name + " — " + profile["label"])
        role = name
        if check_only:
            spec = profile["mcp"]
            try:
                text = expand(spec["path"]).read_text(encoding="utf-8")
                if spec.get("format") == "toml":
                    argv = argv_from_toml(text)
                else:
                    cfg = json.loads(text)
                    server_key = spec.get("server_key", "pair")
                    entry = cfg[spec["root_key"]][server_key]
                    argv = entry["command"] if isinstance(entry["command"], list) else [entry["command"]] + entry.get("args", [])
            except Exception:
                print("    config not found or unreadable")
                failed.append(name)
                continue
        else:
            argv = write_mcp(profile, role)
            if argv is None:
                failed.append(name)
                continue
            print("  MCP:      " + expand(profile["mcp"]["path"]).name)
            print("  skills:   " + ", ".join(write_skills(profile)))
            print("  instructions: " + write_instructions(profile))
            perms_path = write_native_permissions(name)
            if perms_path:
                print("  permissions: " + str(perms_path))

        ok, detail = verify(argv)
        print("  check: " + ("OK, pair-mailbox " + detail if ok else "NOT WORKING — " + detail))
        if not ok:
            failed.append(name)

    # The nested suite (~3 min) skips in installer tests over PAIR_INSTALL_SKIP_SUITE:
    # the verify layer runs it separately, while every test install would carry it
    # anew (698s on install_test.py whole).
    if not os.environ.get("PAIR_INSTALL_SKIP_SUITE"):
        print("\nserver tests")
        print("  checking core and protocol...", end="", flush=True)
        env = dict(os.environ, PAIR_DISABLE_RING="1")
        # The suite rides in a swapped HOME (install tests isolate), while PyNaCl lives
        # in the real-HOME user-site. With no inheriting 2 round-42 evidence tests fall
        # with ModuleNotFoundError: nacl — by env, not code.
        # _site inside this process also sees the swapped HOME, so the real user-site
        # computes over pwd, never over site.
        import site as _site
        import pwd as _pwd
        real_home = _pwd.getpwuid(os.getuid()).pw_dir
        real_site = os.path.join(real_home, ".local", "lib",
                                 f"python{sys.version_info.major}.{sys.version_info.minor}",
                                 "site-packages")
        candidates = set(_site.getsitepackages() + [_site.getusersitepackages()] + [real_site])
        live = [p for p in candidates if p and os.path.isdir(p)]
        if live:
            old = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(live + ([old] if old else []))
        t_res = subprocess.run([sys.executable, str(ROOT / "mcp/pair_mailbox/test_protocol.py")],
                               cwd=str(ROOT), env=env, capture_output=True, text=True)
        if t_res.returncode != 0:
            print(" FAILED")
            print("\nTESTS FAILED:")
            if t_res.stdout:
                print(t_res.stdout)
            if t_res.stderr:
                print(t_res.stderr)
            return 1
        print(" OK")

    if failed:
        print("\nNOT DONE for: " + ", ".join(failed))
        print("fix these by hand, the rest work")
        return 1

    print("\n" + "=" * 70)
    print("✓ pair-mailbox installed and ready to work!")
    print("\nQuick start:")
    print("  1. Start an agent pair in tmux:")
    print("     ./bin/pairup opencode agy")
    print("  2. In either agent window, set the goal:")
    print('     "Work together on task X"')
    print("  3. Round-monitoring web board:")
    print("     ./bin/pairboard")
    print("\nDocumentation: docs/PROTOCOL.md | docs/PROTOCOL.ru.md")
    print("Author: Aliaksei Patskevich (AVP-Dev) • https://avpdev.com • https://t.me/AVP_Dev")
    print("GitHub: https://github.com/AVP-Dev/pair-mailbox")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
