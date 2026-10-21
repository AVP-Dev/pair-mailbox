#!/usr/bin/env bash
# The changed-file gate must see the work that is already staged.
#
# Round 140. The gate found the changed files with `git diff --name-only`,
# which compares the WORKTREE WITH THE INDEX. A change that was already
# `git add`ed was in neither list: it is not unstaged, and it is tracked, so
# it is not "other" either. The gate printed "nothing to check, the tree is
# clean" and exited 0 - a green gate that ran nothing. The map's own test
# wants the files the map names to be tracked, so a new file the map names
# had to be staged before the map's test passed - and once it was staged, the
# gate could not see it. There was no state in which the per-commit gate
# covered a new file the map names.
#
# The proof runs the real script and the real map in a temp git repository.
# No network, nothing written outside the temp dir, nothing of the machine
# (panes, tokens, the pair's state) is touched.
#
# Run: bash scripts/verify-changed_test.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
GATE="$ROOT/scripts/verify-changed.sh"
MAP="$ROOT/scripts/verify-map.tsv"

fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }

MAPPED="mcp/pair_mailbox/test_composer.py"   # the map names it: the composer suite
STAGED_NEW="mcp/pair_mailbox/test_pointer.py"  # a file the map names, not yet committed
UNTRACKED="mcp/pair_mailbox/some_new_thing.py"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/scripts" "$tmp/mcp/pair_mailbox"
cp "$GATE" "$MAP" "$tmp/scripts/"
cd "$tmp"

git init -q .
git config user.email "tester@example.com"
git config user.name "tester"
printf 'seed\n' > "$MAPPED"
# The copies are committed too: an untracked file is a change the gate
# must report, so leaving them loose would make the tree dirty before the
# first case.
git add "$MAPPED" scripts/verify-changed.sh scripts/verify-map.tsv
git commit -qm "seed"

gate() { "$tmp/scripts/verify-changed.sh" --all 2>&1; }

# The empty case stays honest: a clean tree is not a failure.
out="$(gate)" || fail "the gate failed on a clean tree: $out"
case "$out" in
  *"nothing to check"*) ok "a clean tree is honestly empty" ;;
  *) fail "a clean tree must say 'nothing to check', got: $out" ;;
esac

# An unstaged change: seen (this worked before, and must keep working).
printf 'changed\n' >> "$MAPPED"
out="$(gate)" || fail "the gate failed on an unstaged change: $out"
case "$out" in
  *"$MAPPED"*) ok "an unstaged change is selected" ;;
  *) fail "an unstaged change must be selected, got: $out" ;;
esac
case "$out" in
  *"composer"*) ok "the unstaged change selects the suite the map names" ;;
  *) fail "the unstaged change must select the composer suite, got: $out" ;;
esac

# The same change, staged: this is the case the gate was blind to.
git add "$MAPPED"
out="$(gate)" || fail "the gate failed on a staged change: $out"
case "$out" in
  *"$MAPPED"*) ok "a staged change is selected" ;;
  *) fail "a staged change must be selected, got: $out" ;;
esac
case "$out" in
  *"composer"*) ok "the staged change selects the suite the map names" ;;
  *) fail "the staged change must select the composer suite, got: $out" ;;
esac

# A staged NEW file the map names: the state the card called impossible.
printf 'new\n' > "$STAGED_NEW"
git add "$STAGED_NEW"
out="$(gate)" || fail "the gate failed on a staged new file: $out"
case "$out" in
  *"$STAGED_NEW"*) ok "a staged new file is selected" ;;
  *) fail "a staged new file must be selected, got: $out" ;;
esac
case "$out" in
  *"pointer"*) ok "the staged new file selects the suite the map names" ;;
  *) fail "the staged new file must select the pointer suite, got: $out" ;;
esac

# An untracked file: still seen.
printf 'loose\n' > "$UNTRACKED"
out="$(gate)" || fail "the gate failed on an untracked file: $out"
case "$out" in
  *"$UNTRACKED"*) ok "an untracked file is selected" ;;
  *) fail "an untracked file must be selected, got: $out" ;;
esac

# The branch-level run: the files of a whole branch, the new ones included.
git add "$STAGED_NEW"
git commit -qm "a new file the map names"
out="$("$tmp/scripts/verify-changed.sh" --base HEAD~1 --all 2>&1)" || fail "the gate failed on a branch-level run: $out"
case "$out" in
  *"$STAGED_NEW"*) ok "the branch-level run covers the branch's new files" ;;
  *) fail "the branch-level run must cover the branch's new files, got: $out" ;;
esac

echo "verify-changed_test: all checks passed"
