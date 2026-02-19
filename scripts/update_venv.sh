#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

cd "$PROJECT_ROOT"

if [ "$ALLOW_DIRTY" != "1" ] && [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is not clean. Commit/stash changes or set ALLOW_DIRTY=1." >&2
  exit 1
fi

echo "Fetching updates from repository..."
git fetch --all --prune
git pull --ff-only

if [ ! -d "$VENV_DIR" ]; then
  echo "Virtual environment not found. Creating '$VENV_DIR'..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

echo "Updating Python dependencies..."
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

echo "Repository and virtual environment are updated."
