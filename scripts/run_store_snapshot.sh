#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${REPO_ROOT}/.venv/bin/activate" ]]; then
  # POSIX virtualenv layout
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.venv/bin/activate"
elif [[ -f "${REPO_ROOT}/.venv/Scripts/activate" ]]; then
  # Windows virtualenv layout for Git Bash/MSYS
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.venv/Scripts/activate"
else
  echo "Virtual environment activation script not found under ${REPO_ROOT}/.venv." >&2
  exit 1
fi

cd "${SCRIPT_DIR}"
python store_snapshot.py "$@"