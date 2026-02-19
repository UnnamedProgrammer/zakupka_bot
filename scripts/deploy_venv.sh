#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"

cd "$PROJECT_ROOT"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python interpreter '$PYTHON_BIN' not found." >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in '$VENV_DIR'..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  echo "Using existing virtual environment in '$VENV_DIR'."
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

echo "Installing dependencies..."
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

if [ ! -f "$PROJECT_ROOT/.env" ] && [ -f "$PROJECT_ROOT/.env.example" ]; then
  cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
  echo "Created '.env' from '.env.example'. Fill in real values before start."
fi

echo
echo "Virtual environment deployment is complete."
echo "Activate manually: source \"$VENV_DIR/bin/activate\""
echo "Run bot: python -m app.main"
