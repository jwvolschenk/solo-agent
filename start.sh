#!/usr/bin/env bash
# Solo Agent — start the dashboard server.
# Usage:  ./start.sh
set -euo pipefail

cd "$(dirname "$0")"

# --- config (override via env or .env) --------------------------------------
# llama-server API key (matches ~/services/llama-tq/start-server.sh default)
LLAMA_API_KEY="${LLAMA_API_KEY:-j9o5mbOYx7s1v4tc9IJQ7SOxK2I42uCtmiueQftAuLU}"
LLAMA_SERVER_URL="${LLAMA_SERVER_URL:-http://localhost:8080}"
PORT="${PORT:-8090}"

# --- venv setup -------------------------------------------------------------
if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi
if ! venv/bin/python -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  venv/bin/pip install -q -r requirements.txt
fi

# --- pre-flight checks ------------------------------------------------------
if ! curl -sf --max-time 3 "$LLAMA_SERVER_URL/health" >/dev/null 2>&1; then
  echo "⚠  llama-server not reachable at $LLAMA_SERVER_URL"
  echo "   Start it first:  systemctl --user start llama-server"
  echo "   The dashboard will still run but show OFFLINE."
fi

mkdir -p data

# --- launch -----------------------------------------------------------------
echo "Starting Solo Agent on http://localhost:$PORT"
exec env \
  LLAMA_API_KEY="$LLAMA_API_KEY" \
  LLAMA_SERVER_URL="$LLAMA_SERVER_URL" \
  PORT="$PORT" \
  venv/bin/uvicorn src.main:app --host 0.0.0.0 --port "$PORT"
