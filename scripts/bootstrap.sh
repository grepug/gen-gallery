#!/usr/bin/env bash
set -euo pipefail

SERVER_HOME="${IMAGEGEN_SERVER_HOME:-$HOME/.imagegen-server}"
VENV_PATH="$SERVER_HOME/venv"

mkdir -p "$SERVER_HOME"
python3 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"
pip install -e .

echo "Bootstrapped imagegen-server in $VENV_PATH"
