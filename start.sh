#!/usr/bin/env bash
set -e

export HOME="$PWD"
export PATH="$HOME/.local/bin:$PATH"

echo "=== HERMES CONFIGURATION: GROQ ==="

# ---------------------------------------------------------
# 1. Required secret check
# ---------------------------------------------------------
if [ -z "${GROQ_API_KEY:-}" ]; then
    echo "ERROR: GROQ_API_KEY is not configured in Render."
    exit 1
fi

# ---------------------------------------------------------
# 2. Hermes config update
#    Preserve existing tools, skills, memory and other config.
#    Only update the provider/model/auxiliary settings we own.
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
# Custom Groq provider
# ---------------------------------------------------------
providers = config.setdefault("custom_providers", [])

groq_config = {
    "name": "groq",
    "base_url": "https://api.groq.com/openai/v1",
    "key_env": "GROQ_API_KEY",
    "models": {
        "openai/gpt-oss-120b": {
            "context_length": 131072
        }
    }
}

groq_index = next(
    (
        i for i, provider in enumerate(providers)
        if isinstance(provider, dict)
        and provider.get("name") == "groq"
    ),
    None
)

if groq_index is None:
    providers.append(groq_config)
else:
    providers[groq_index].update(groq_config)

# ---------------------------------------------------------
# Main model
# ---------------------------------------------------------
model_config = config.setdefault("model", {})

model_config.update({
    "default": "openai/gpt-oss-120b",
    "provider": "custom:groq",
    "context_length": 131072
})

# ---------------------------------------------------------
# Auxiliary tasks
#
# IMPORTANT:
# base_url makes these direct OpenAI-compatible endpoint
# calls. Hermes uses OPENAI_API_KEY for authentication.
# ---------------------------------------------------------
auxiliary = config.setdefault("auxiliary", {})

for task_name in ("title_generation", "compression"):
    task_config = auxiliary.setdefault(task_name, {})

    task_config.update({
        "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1",
        "fallback_chain": []
    })

# ---------------------------------------------------------
# Disable the main fallback chain.
# No OpenRouter fallback is configured here.
# ---------------------------------------------------------
config["fallback_providers"] = []
config["fallback_models"] = []

# ---------------------------------------------------------
# Remove only stale auxiliary keys that we previously created.
# Do NOT delete agent/memory/tools/profile/database configuration.
# ---------------------------------------------------------
for stale_key in ("title_model", "summarizer_model"):
    config.pop(stale_key, None)

# ---------------------------------------------------------
# Save config
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
# Safe verification output.
# Never print secrets or the complete config.
# ---------------------------------------------------------
print("=== EFFECTIVE GROQ CONFIGURATION ===")
print("main_model:", config.get("model", {}).get("default"))
print("main_provider:", config.get("model", {}).get("provider"))
print("main_context_length:", config.get("model", {}).get("context_length"))

groq = next(
    (
        p for p in config.get("custom_providers", [])
        if isinstance(p, dict) and p.get("name") == "groq"
    ),
    {}
)

print("groq_base_url:", groq.get("base_url"))
print("groq_key_env:", groq.get("key_env"))

for task in ("title_generation", "compression"):
    task_cfg = config.get("auxiliary", {}).get(task, {})
    print(f"auxiliary.{task}.model:", task_cfg.get("model"))
    print(f"auxiliary.{task}.base_url:", task_cfg.get("base_url"))
    print(f"auxiliary.{task}.fallback_chain:",
          task_cfg.get("fallback_chain"))

print("global_fallback_providers:",
      config.get("fallback_providers"))
print("global_fallback_models:",
      config.get("fallback_models"))
print("====================================")
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
# 5. Auxiliary direct endpoint authentication
#
# Hermes auxiliary custom base_url uses OPENAI_API_KEY
# when an explicit api_key is not configured.
#
# The actual secret still exists only in Render Environment.
# It is NOT written into config.yaml or GitHub.
# ---------------------------------------------------------
export OPENAI_API_KEY="$GROQ_API_KEY"

# ---------------------------------------------------------
# 6. Start Hermes Gateway
# ---------------------------------------------------------
echo "=== STARTING REAL HERMES GATEWAY ==="

hermes gateway run 2>&1 | tee /tmp/hermes-agent.log &
HERMES_PID=$!

# ---------------------------------------------------------
# 7. Wait for Hermes health endpoint
# ---------------------------------------------------------
for i in $(seq 1 60); do

    if curl -fsS \
        "http://127.0.0.1:${API_SERVER_PORT}/health" \
        >/dev/null 2>&1; then
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

# ---------------------------------------------------------
# 8. Final health check
# ---------------------------------------------------------
if ! curl -fsS \
    "http://127.0.0.1:${API_SERVER_PORT}/health" \
    >/dev/null 2>&1; then

    echo "ERROR: Hermes Agent did not become ready."
    echo "=== LAST HERMES LOGS ==="
    tail -n 50 /tmp/hermes-agent.log || true
    exit 1
fi

echo "=== REAL HERMES AGENT IS READY ==="

# ---------------------------------------------------------
# 9. Start public Flask/Gunicorn API
# ---------------------------------------------------------
exec gunicorn \
    --bind "0.0.0.0:${PORT:-10000}" \
    --workers 1 \
    --timeout 240 \
    app:app
