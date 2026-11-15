#!/usr/bin/env bash
# safe-merge.sh — Safe sequential branch integration with pre-merge conflict detection and post-merge test verification.
set -euo pipefail

usage() {
    echo "Usage: $0 <target_branch> <feature_branch> [--ff-only]"
    echo "  Dry-run conflict check via git merge-tree -> merge -> verify -> rollback on failure."
    exit 2
}

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
    usage
fi

TARGET="$1"
FEATURE="$2"
FF_FLAG="${3:-}"

if [ -n "$FF_FLAG" ] && [ "$FF_FLAG" != "--ff-only" ]; then
    echo "ERROR: Invalid option '$FF_FLAG'. Supported flags: --ff-only" >&2
    usage
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    echo "ERROR: Not inside a git repository." >&2
    exit 1
fi

# 1. Clean working directory check
if [ -n "$(git status --porcelain -uno)" ]; then
    echo "ERROR: Working directory has uncommitted tracked changes. Stash or commit before merging." >&2
    exit 1
fi

# 2. Check branches exist and save original heads
if ! git rev-parse --verify "$TARGET" >/dev/null 2>&1; then
    echo "ERROR: Target branch '$TARGET' does not exist." >&2
    exit 1
fi
if ! git rev-parse --verify "$FEATURE" >/dev/null 2>&1; then
    echo "ERROR: Feature branch '$FEATURE' does not exist." >&2
    exit 1
fi

TARGET_ORIG_SHA="$(git rev-parse "$TARGET")"
FEATURE_ORIG_SHA="$(git rev-parse "$FEATURE")"

BASE_COMMIT="$(git merge-base "$TARGET" "$FEATURE" 2>/dev/null || true)"
if [ -z "$BASE_COMMIT" ]; then
    echo "ERROR: No common ancestor between '$TARGET' and '$FEATURE'." >&2
    exit 1
fi

echo "=== 1. Dry-run conflict check ($TARGET <-> $FEATURE) ==="
# git merge-tree performs an in-memory 3-way merge without touching the index or working tree.
# The legacy three-tree form returns 1 for a merge conflict. Distinguish that
# expected result from an unknown/error response using its output; every other
# nonzero result remains a fail-closed preflight error.
MERGE_TREE_EXIT=0
MERGE_TREE_OUT="$(git merge-tree "$BASE_COMMIT" "$TARGET" "$FEATURE" 2>&1)" || MERGE_TREE_EXIT=$?
case "$MERGE_TREE_OUT" in
    *'<<<<<<<'*)
    if [ "$MERGE_TREE_EXIT" -eq 0 ] || [ "$MERGE_TREE_EXIT" -eq 1 ]; then
        echo "CONFLICT: Branch '$FEATURE' cannot be merged cleanly into '$TARGET'." >&2
        echo "Conflicting files detected by git merge-tree. Both branches left intact." >&2
        exit 1
    fi
    ;;
esac
if [ "$MERGE_TREE_EXIT" -ne 0 ]; then
    echo "ERROR: git merge-tree preflight failed (exit code $MERGE_TREE_EXIT)." >&2
    echo "$MERGE_TREE_OUT" >&2
    echo "Aborting preflight safely without touching branches or index." >&2
    exit 1
fi
echo "Conflict check PASSED (clean 3-way merge guaranteed)."

# 3. Save current branch for clean restoration
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

rollback() {
    echo "WARNING: Merge verification failed. Rolling back '$TARGET' to $TARGET_ORIG_SHA..." >&2
    git checkout "$TARGET" >/dev/null 2>&1 || true
    git reset --hard "$TARGET_ORIG_SHA" >/dev/null 2>&1 || true
    if [ "$CURRENT_BRANCH" != "$TARGET" ]; then
        git checkout "$CURRENT_BRANCH" >/dev/null 2>&1 || true
    fi
    echo "Rollback complete. Branches preserved with no data loss." >&2
}

echo "=== 2. Performing merge ==="
git checkout "$TARGET" >/dev/null 2>&1

MERGE_FAILED=0
if [ "$FF_FLAG" = "--ff-only" ]; then
    git merge --ff-only "$FEATURE" || MERGE_FAILED=1
else
    git merge --no-ff "$FEATURE" -m "merge: integrate $FEATURE into $TARGET after verification" || MERGE_FAILED=1
fi

if [ "$MERGE_FAILED" -ne 0 ]; then
    rollback
    echo "ERROR: Merge execution failed." >&2
    exit 1
fi

echo "=== 3. Running post-merge verification suite ==="
VERIFY_SCRIPT="$REPO_ROOT/scripts/verify-changed.sh"
if [ -f "$VERIFY_SCRIPT" ]; then
    echo "Running verify-changed.sh against pre-merge target SHA $TARGET_ORIG_SHA..."
    if ! bash "$VERIFY_SCRIPT" --base "$TARGET_ORIG_SHA"; then
        echo "ERROR: Post-merge verification checks failed." >&2
        rollback
        exit 1
    fi
else
    echo "No verify-changed.sh found; checking git diff --check..."
    if ! git diff --check "$TARGET_ORIG_SHA"..HEAD; then
        echo "ERROR: Post-merge git diff check failed." >&2
        rollback
        exit 1
    fi
fi

echo "=== Integration complete! '$FEATURE' safely integrated into '$TARGET' ==="
if [ "$CURRENT_BRANCH" != "$TARGET" ]; then
    git checkout "$CURRENT_BRANCH" >/dev/null 2>&1 || true
fi
exit 0
