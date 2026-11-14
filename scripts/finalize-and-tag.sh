#!/usr/bin/env bash
# finalize-and-tag.sh — Finalize session, sync to GitHub, and create release tag.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN_REPO="/Users/AVP-Dev/Downloads/Github/My/pair-mailbox"
BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"

echo "=== 1. Checking git status ==="
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain -uno)" ]; then
    echo "ERROR: Worktree working directory is dirty. Commit or stash changes first." >&2
    exit 1
fi
echo "Worktree ($BRANCH) is clean at $HEAD_SHA."

echo "=== 2. Pushing branch $BRANCH to origin (GitHub) ==="
git -C "$PROJECT_ROOT" push -u origin "$BRANCH"

echo "=== 3. Synchronizing main repository (master) ==="
if [ -d "$MAIN_REPO" ]; then
    echo "Updating master in $MAIN_REPO..."
    git -C "$MAIN_REPO" checkout master
    git -C "$MAIN_REPO" merge --ff-only "$HEAD_SHA"
    echo "Pushing master to origin (GitHub)..."
    PAIR_ALLOW_TRUNK_PUSH=1 git -C "$MAIN_REPO" push origin master
else
    echo "Main repo directory $MAIN_REPO not found; skipping local master sync."
fi

echo "=== 4. Determining next release tag ==="
LATEST_TAG="$(git -C "$PROJECT_ROOT" tag -l 'v*' | sort -V | tail -n 1)"
echo "Latest existing tag: ${LATEST_TAG:-none}"

# Default next version
NEXT_TAG="v1.0.8"
if git -C "$PROJECT_ROOT" rev-parse "$NEXT_TAG" >/dev/null 2>&1; then
    echo "Tag $NEXT_TAG already exists."
else
    echo "Creating annotated tag $NEXT_TAG on $HEAD_SHA..."
    git -C "$PROJECT_ROOT" tag -a "$NEXT_TAG" "$HEAD_SHA" -m "Release $NEXT_TAG: Two-tier Guild Architecture and Codex permission supervision fix"
fi

echo "=== 5. Pushing tag $NEXT_TAG to origin (GitHub) ==="
git -C "$PROJECT_ROOT" push origin "$NEXT_TAG"

echo "=== 6. Verifying remote refs ==="
git -C "$PROJECT_ROOT" ls-remote origin refs/heads/master refs/heads/"$BRANCH" refs/tags/"$NEXT_TAG"

echo "=== Finalization and synchronization complete! ==="
