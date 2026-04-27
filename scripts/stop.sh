#!/usr/bin/env bash
set -euo pipefail

SERVER_HOME="${IMAGEGEN_SERVER_HOME:-$HOME/.imagegen-server}"
PID_PATH="$SERVER_HOME/server.pid"

if [[ ! -f "$PID_PATH" ]]; then
  echo "No pid file at $PID_PATH"
  exit 0
fi

PID="$(cat "$PID_PATH")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped imagegen-server pid=$PID"
else
  echo "Process $PID is not running"
fi

rm -f "$PID_PATH"
