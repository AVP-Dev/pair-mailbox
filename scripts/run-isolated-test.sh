#!/bin/sh
# Running commands in a container with no network and no $HOME access (P0, round 75).
#
# Problem: agent tests run on the host with operator rights — any `pytest` in the
# prompt is arbitrary code (THREAT_MODEL.md: violator #1, confused deputy). The
# script gives an isolated alternative: the same command but in a container with
# --network none, mounting only the project.
#
# Fallback: no runtime — the script NEVER runs the command, returning 2, demanding
# the human. Silently launching on host reads worse than stopping.
#
# Usage: scripts/run-isolated-test.sh [--image IMG] -- <command...>
#   scripts/run-isolated-test.sh -- pytest -q
#   scripts/run-isolated-test.sh --image python:3.10-slim -- pytest -q
set -eu

IMAGE="python:3.10-slim"
if [ "${1:-}" = "--image" ]; then
    IMAGE="${2:?an image is needed past --image}"
    shift 2
fi
if [ "${1:-}" = "--" ]; then
    shift
fi
if [ "$#" -eq 0 ]; then
    echo "usage: $0 [--image IMG] -- <command...>" >&2
    exit 2
fi

RUNTIME=""
if command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
fi
if [ -z "$RUNTIME" ]; then
    echo "no container runtime (docker/podman): running on host " >&2
    echo "only with human confirm; self-confirm banned (P0)" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$RUNTIME" run --rm \
    --network none \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp \
    -v "$PROJECT_ROOT:/work:rw" \
    -w /work \
    "$IMAGE" "$@"
