#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_HOME="${IMAGEGEN_SERVER_HOME:-$HOME/.imagegen-server}"
VENV_PATH="$SERVER_HOME/venv"

mkdir -p "$SERVER_HOME"
python3 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"
"$VENV_PATH/bin/python" -m pip install --upgrade pip setuptools wheel
pip install -e "$REPO_ROOT"

echo "Bootstrapped imagegen-server in $VENV_PATH"
