#!/bin/sh
# Запуск команд в контейнере без сети и без доступа к $HOME (P0, раунд 75).
#
# Проблема: агентские тесты выполняются на хосте от прав оператора — любой
# `pytest` в промпте это произвольный код (THREAT_MODEL.md: нарушитель №1,
# confused deputy). Скрипт даёт изолированную альтернативу: та же команда,
# но в контейнере с --network none, примонтированным только проектом.
#
# Fallback: рантайма нет — скрипт НЕ выполняет команду, а возвращает 2
# и требует человека. Молча запускать на хосте хуже, чем остановиться.
#
# Использование: scripts/run-isolated-test.sh [--image IMG] -- <команда...>
#   scripts/run-isolated-test.sh -- pytest -q
#   scripts/run-isolated-test.sh --image python:3.10-slim -- pytest -q
set -eu

IMAGE="python:3.10-slim"
if [ "${1:-}" = "--image" ]; then
    IMAGE="${2:?нужен образ после --image}"
    shift 2
fi
if [ "${1:-}" = "--" ]; then
    shift
fi
if [ "$#" -eq 0 ]; then
    echo "использование: $0 [--image IMG] -- <команда...>" >&2
    exit 2
fi

RUNTIME=""
if command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
fi
if [ -z "$RUNTIME" ]; then
    echo "нет контейнерного рантайма (docker/podman): выполняю на хосте " >&2
    echo "только с подтверждения человека; автоподтверждение запрещено (P0)" >&2
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
