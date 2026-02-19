#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"

cd "$PROJECT_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed or not available in PATH." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is not installed." >&2
  exit 1
fi

if [ "$SKIP_GIT_PULL" != "1" ]; then
  if [ "$ALLOW_DIRTY" != "1" ] && [ -n "$(git status --porcelain)" ]; then
    echo "Working tree is not clean. Commit/stash changes or set ALLOW_DIRTY=1." >&2
    exit 1
  fi

  echo "Fetching updates from repository..."
  git fetch --all --prune
  git pull --ff-only
else
  echo "Skipping repository update (SKIP_GIT_PULL=1)."
fi

echo "Rebuilding and restarting containers..."
"${COMPOSE[@]}" down
"${COMPOSE[@]}" up -d --build --remove-orphans
"${COMPOSE[@]}" ps

echo "Docker services are rebuilt and running."
