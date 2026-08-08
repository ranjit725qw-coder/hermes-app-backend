#!/usr/bin/env bash
set -e

# Hermes-এর কথামতো .bashrc ফাইলটি লোড করা হচ্ছে যাতে সে hermes কমান্ড খুঁজে পায়
if [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc"
fi

# ব্যাকআপ হিসেবে অন্যান্য সম্ভাব্য ফোল্ডারগুলোকেও চেনার উপায় দেওয়া হলো
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"

# চেক করা হচ্ছে hermes কমান্ড এখন কাজ করছে কি না
if ! command -v hermes &> /dev/null; then
    echo "ERROR: hermes command still not found."
    echo "Checking ~/.hermes directory:"
    ls -la "$HOME/.hermes" || true
    exit 1
fi

export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export API_SERVER_KEY="${API_SERVER_KEY:?Set API_SERVER_KEY in Render Environment Variables}"
export API_SERVER_CORS_ORIGINS=""

echo "Starting Real Hermes Gateway..."
hermes gateway run > /tmp/hermes-agent.log 2>&1 &
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
