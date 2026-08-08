#!/usr/bin/env bash
set -e

# ১. ~/.hermes ফোল্ডার রিস্টোর করা হচ্ছে
if [ ! -d "$HOME/.hermes" ] && [ -d "$PWD/hermes_backup" ]; then
    echo "Restoring .hermes from backup..."
    cp -a "$PWD/hermes_backup" "$HOME/.hermes"
fi

# ২. ~/.local/bin (যেখানে আসল hermes কমান্ড থাকে) রিস্টোর করা হচ্ছে
if [ ! -f "$HOME/.local/bin/hermes" ] && [ -d "$PWD/local_bin_backup" ]; then
    echo "Restoring .local/bin from backup..."
    mkdir -p "$HOME/.local/bin"
    cp -a "$PWD/local_bin_backup"/* "$HOME/.local/bin/"
    # এক্সিকিউটেবল পারমিশন দেওয়া হচ্ছে যাতে রান করতে পারে
    chmod +x "$HOME/.local/bin/hermes" || true
fi

# কমান্ডটি যাতে কাজ করে তার জন্য PATH সেট করা
export PATH="$HOME/.local/bin:$PATH"

if ! command -v hermes &> /dev/null; then
    echo "ERROR: hermes command still not found."
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
