#!/usr/bin/env bash
set -e

export PATH="$HOME/.local/bin:$PATH"

# Hermes Agent uses these variables for its native API server.
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export API_SERVER_KEY="${API_SERVER_KEY:?Set API_SERVER_KEY in Render Environment Variables}"

# Keep browser CORS disabled on Hermes itself because the public browser
# talks to the Flask proxy. The proxy handles CORS.
export API_SERVER_CORS_ORIGINS=""

# Start the REAL Hermes Agent gateway in the background.
hermes gateway > /tmp/hermes-agent.log 2>&1 &
HERMES_PID=$!

# Wait until Hermes is actually ready.
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$HERMES_PID" >/dev/null 2>&1; then
    echo "Hermes Agent stopped unexpectedly:"
    cat /tmp/hermes-agent.log || true
    exit 1
  fi
  sleep 2
done

if ! curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
  echo "Hermes Agent did not become ready:"
  cat /tmp/hermes-agent.log || true
  exit 1
fi

echo "Real Hermes Agent is ready."

exec gunicorn --bind "0.0.0.0:${PORT:-10000}" --workers 1 --timeout 240 app:app
