#!/usr/bin/env bash
set -e

export HOME="$PWD"
export PATH="$HOME/.local/bin:$PATH"

echo "=== HERMES CONFIGURATION: GEMINI 2.5 FLASH (TARGETED) ==="

# ---------------------------------------------------------
# 1. Required secret check
# ---------------------------------------------------------
if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "ERROR: GEMINI_API_KEY is not configured in Render Environment Variables."
    exit 1
fi

# ---------------------------------------------------------
# 2. Hermes config update (Strictly preserving existing state)
# ---------------------------------------------------------
python3 -c '
import os
import yaml

config_path = os.path.join(
    os.environ.get("HOME", "."),
    ".hermes",
    "config.yaml"
)

try:
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}
except FileNotFoundError:
    config = {}

# ---------------------------------------------------------
# Custom Gemini provider (Global definition for inheritance)
# ---------------------------------------------------------
providers = config.setdefault("custom_providers", [])

gemini_config = {
    "name": "google_ai_studio",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "key_env": "GEMINI_API_KEY",
    "models": {
        "gemini-2.5-flash": {
            "context_length": 1048576
        }
    }
}

gemini_idx = next(
    (
        i for i, provider in enumerate(providers)
        if isinstance(provider, dict)
        and provider.get("name") == "google_ai_studio"
    ),
    None
)

if gemini_idx is None:
    providers.append(gemini_config)
else:
    providers[gemini_idx].update(gemini_config)

# ---------------------------------------------------------
# Main model routing
# ---------------------------------------------------------
model_config = config.setdefault("model", {})

model_config.update({
    "default": "gemini-2.5-flash",
    "provider": "custom:google_ai_studio",
    "context_length": 1048576
})

# ---------------------------------------------------------
# Auxiliary tasks routing (TARGETED OVERRIDE ONLY)
# Preserving vision, mcp, approval, and other existing tasks.
# ---------------------------------------------------------
auxiliary = config.setdefault("auxiliary", {})

# Only specific LLM-backed text tasks that risk OpenRouter/auto fallback
target_llm_tasks = [
    "title_generation", 
    "compression", 
    "summarizer", 
    "web_extract"
]

for task_name in target_llm_tasks:
    task_config = auxiliary.setdefault(task_name, {})
    task_config.update({
        "model": "gemini-2.5-flash",
        "provider": "custom:google_ai_studio"
    })
    
    # Safely remove unverified/insecure keys if previously set
    for unsafe_key in ("fallback_chain", "api_key", "base_url"):
        task_config.pop(unsafe_key, None)

# ---------------------------------------------------------
# Disable the main fallback chains explicitly
# ---------------------------------------------------------
config["fallback_providers"] = []
config["fallback_models"] = []

# ---------------------------------------------------------
# Clean up stale legacy auxiliary keys
# ---------------------------------------------------------
for stale_key in ("title_model", "summarizer_model"):
    config.pop(stale_key, None)

# ---------------------------------------------------------
# Save config safely
# ---------------------------------------------------------
os.makedirs(os.path.dirname(config_path), exist_ok=True)

with open(config_path, "w") as f:
    yaml.safe_dump(
        config,
        f,
        sort_keys=False,
        default_flow_style=False
    )

# ---------------------------------------------------------
# Safe verification output
# ---------------------------------------------------------
print("=== EFFECTIVE GEMINI CONFIGURATION READY ===")
print("main_model:", config.get("model", {}).get("default"))
print("main_provider:", config.get("model", {}).get("provider"))
print("main_context_length:", config.get("model", {}).get("context_length"))

for task in target_llm_tasks:
    task_cfg = config.get("auxiliary", {}).get(task, {})
    print(f"auxiliary.{task}.model:", task_cfg.get("model"))
    print(f"auxiliary.{task}.provider:", task_cfg.get("provider"))

print("global_fallback_providers:", config.get("fallback_providers"))
print("global_fallback_models:", config.get("fallback_models"))
print("============================================")
'

# ---------------------------------------------------------
# 3. Hermes executable check
# ---------------------------------------------------------
if ! command -v hermes >/dev/null 2>&1; then
    echo "ERROR: hermes command not found."
    exit 1
fi

echo "=== HERMES VERSION ==="
hermes --version || true
echo "======================"

# ---------------------------------------------------------
# 4. API server configuration
# ---------------------------------------------------------
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export API_SERVER_KEY="${API_SERVER_KEY:?Set API_SERVER_KEY in Render Environment Variables}"
export API_SERVER_CORS_ORIGINS=""

# ---------------------------------------------------------
# 5. Start Hermes Gateway
# ---------------------------------------------------------
echo "=== STARTING REAL HERMES GATEWAY ==="

hermes gateway run 2>&1 | tee /tmp/hermes-agent.log &
HERMES_PID=$!

for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
        break
    fi

    if ! kill -0 "$HERMES_PID" >/dev/null 2>&1; then
        echo "ERROR: Hermes Agent stopped unexpectedly."
        echo "=== LAST HERMES LOGS ==="
        tail -n 50 /tmp/hermes-agent.log || true
        exit 1
    fi
    sleep 2
done

if ! curl -fsS "http://127.0.0.1:${API_SERVER_PORT}/health" >/dev/null 2>&1; then
    echo "ERROR: Hermes Agent did not become ready."
    echo "=== LAST HERMES LOGS ==="
    tail -n 50 /tmp/hermes-agent.log || true
    exit 1
fi

echo "=== REAL HERMES AGENT IS READY ==="

# ---------------------------------------------------------
# 6. Start public Flask/Gunicorn API
# ---------------------------------------------------------
exec gunicorn \
    --bind "0.0.0.0:${PORT:-10000}" \
    --workers 1 \
    --timeout 240 \
    app:app
