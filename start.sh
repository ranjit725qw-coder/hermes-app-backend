#!/usr/bin/env bash
set -e

export HOME="$PWD"
export PATH="$HOME/.local/bin:$PATH"

echo "=== PURE DIAGNOSTIC MODE: READ-ONLY ==="

echo "=== 1. EXACT HERMES VERSION ==="
hermes --version || echo "Version command failed"
echo "==============================="

echo "=== 2. EXISTING CONFIG DUMP ==="
CONFIG_PATH="$HOME/.hermes/config.yaml"
if [ -f "$CONFIG_PATH" ]; then
    cat "$CONFIG_PATH"
else
    echo "WARNING: config.yaml not found at $CONFIG_PATH"
fi
echo "==============================="

if ! command -v hermes &> /dev/null; then
    echo "ERROR: hermes command not found."
    exit 1
fi

export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export API_SERVER_KEY="${API_SERVER_KEY:?Set API_SERVER_KEY in Render Environment Variables}"
export API_SERVER_CORS_ORIGINS=""

echo "=== 3. STARTING HERMES GATEWAY (STREAMING RAW LOGS) ==="
# tee ব্যবহার করে Hermes-এর stdout/stderr সরাসরি Render লগে স্ট্রিম করা হচ্ছে
hermes gateway run 2>&1 | tee /tmp/hermes-agent.log &
HERMES_PID=$!

for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$HERMES_PID" >/dev/null 2>&1; then
    echo "Hermes Agent stopped unexpectedly. Last 20 lines of log:"
    tail -n 20 /tmp/hermes-agent.log || true
    exit 1
  fi
  sleep 2
done

if ! curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
  echo "Hermes Agent did not become ready. Last 20 lines of log:"
  tail -n 20 /tmp/hermes-agent.log || true
  exit 1
fi

echo "Real Hermes Agent is ready."

# Gunicorn স্টার্ট করা হচ্ছে
exec gunicorn --bind "0.0.0.0:${PORT:-10000}" --workers 1 --timeout 240 app:app
