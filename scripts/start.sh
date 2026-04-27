#!/usr/bin/env bash
set -euo pipefail

SERVER_HOME="${IMAGEGEN_SERVER_HOME:-$HOME/.imagegen-server}"
VENV_PATH="$SERVER_HOME/venv"
PID_PATH="$SERVER_HOME/server.pid"
LOG_PATH="$SERVER_HOME/logs/server.log"
ENV_PATH="$SERVER_HOME/server.env"

if [[ -f "$ENV_PATH" ]]; then
  set -a
  source "$ENV_PATH"
  set +a
fi

if [[ -z "${IMAGE_API_KEYS_JSON:-}" ]]; then
  echo "IMAGE_API_KEYS_JSON is required in the environment." >&2
  exit 1
fi

if [[ -z "${OPENAI_BASE_URL:-}" ]]; then
  echo "OPENAI_BASE_URL is required in the environment." >&2
  exit 1
fi

mkdir -p "$SERVER_HOME/logs"

if [[ ! -x "$VENV_PATH/bin/python" ]]; then
  echo "Missing virtualenv at $VENV_PATH. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

if [[ -f "$PID_PATH" ]] && kill -0 "$(cat "$PID_PATH")" 2>/dev/null; then
  echo "imagegen-server is already running with pid $(cat "$PID_PATH")"
  exit 0
fi

source "$VENV_PATH/bin/activate"
nohup python -m imagegen_server > "$LOG_PATH" 2>&1 &
echo $! > "$PID_PATH"
echo "imagegen-server started on ${APP_HOST:-127.0.0.1}:${APP_PORT:-8000}"
echo "pid=$(cat "$PID_PATH")"
