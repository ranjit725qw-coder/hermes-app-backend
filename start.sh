#!/usr/bin/env bash
set -e

export HOME="$PWD"
export PATH="$HOME/.local/bin:$PATH"

echo "=== HERMES CONFIGURATION: BEDROCK DEEPSEEK V3.2 (ACTIVE) / GEMINI 3.5 FLASH (PRESERVED, DEACTIVATED) ==="

# ---------------------------------------------------------
# 1. Required secret check
# ---------------------------------------------------------
if [ -z "${BEDROCK_API_KEY:-}" ]; then
    echo "ERROR: BEDROCK_API_KEY is not configured in Render Environment Variables."
    exit 1
fi

# ---------------------------------------------------------
# 2. Hermes config update (Strictly preserving existing state)
#
# Root-cause notes (Jul-Aug 2026):
#  * gemini-2.5-flash now returns HTTP 404 "This model ... is no longer
#    available to new users" on the v1beta OpenAI-compatible endpoint
#    (https://generativelanguage.googleapis.com/v1beta/openai/) for
#    newly issued Google AI Studio keys. The live service therefore
#    fails every LLM call for new-key users.
#  * The Gemini free tier enforces GenerateRequestsPerMinutePerProjectPerModel
#    with quotaValue: 5 (5 requests per minute, per model, per project),
#    plus a 20 requests/day cap, which is insufficient for agent
#    workloads (Hermes retries up to 3x per LLM call).
#  * Fix (Aug 2026): switch active routing to Amazon Bedrock
#    (deepseek.v3.2 via the bedrock-mantle Chat Completions API) which
#    has token-rate quotas only (no daily request cap) and was verified
#    end-to-end (auth / basic chat / function calling: all PASS).
#  * Gemini is NOT removed: the google_ai_studio provider block and
#    GEMINI_API_KEY configuration are preserved below, deactivated only.
#    To re-activate Gemini: swap the default model/provider strings and
#    the BEDROCK_API_KEY check back to GEMINI_API_KEY.
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
# Custom providers
# ---------------------------------------------------------
providers = config.setdefault("custom_providers", [])

# ---------------------------------------------------------
# Amazon Bedrock (bedrock-mantle) provider — ACTIVE
# OpenAI-compatible Chat Completions API on the bedrock-mantle endpoint
# in us-east-1. Verified isolated end-to-end on Aug 13, 2026.
# Auth: Bearer token from BEDROCK_API_KEY (Render env var).
# Models available on this endpoint (55 total); deepseek.v3.2 selected
# after verified chat + function-calling tests.
# ---------------------------------------------------------
bedrock_config = {
    "name": "bedrock_mantle",
    "base_url": "https://bedrock-mantle.us-east-1.api.aws/v1/",
    "key_env": "BEDROCK_API_KEY",
    "models": {
        "deepseek.v3.2": {
            "context_length": 131072
        },
        "deepseek.v3.1": {
            "context_length": 131072
        }
    }
}

bedrock_idx = next(
    (
        i for i, provider in enumerate(providers)
        if isinstance(provider, dict)
        and provider.get("name") == "bedrock_mantle"
    ),
    None
)

if bedrock_idx is None:
    providers.append(bedrock_config)
else:
    providers[bedrock_idx].update(bedrock_config)

# ---------------------------------------------------------
# Gemini provider (Google AI Studio) — PRESERVED, DEACTIVATED
# Kept intact exactly as before so Gemini can be re-activated
# immediately after quota reset by swapping the default
# model/provider strings. NOT DELETE THIS BLOCK.
# ---------------------------------------------------------
gemini_config = {
    "name": "google_ai_studio",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "key_env": "GEMINI_API_KEY",
    "models": {
        "gemini-3.5-flash": {
            "context_length": 1048576
        },
        "gemini-3.5-flash-lite": {
            "context_length": 1048576
        },
        "gemini-3.6-flash": {
            "context_length": 1048576
        },
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
# Main model routing — ACTIVE: Amazon Bedrock deepseek.v3.2
# (Gemini preserved above: model=gemini-3.5-flash,
#  provider=custom:google_ai_studio — re-enable by swapping)
# ---------------------------------------------------------
model_config = config.setdefault("model", {})

model_config.update({
    "default": "deepseek.v3.2",
    "provider": "custom:bedrock_mantle",
    "context_length": 131072
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
        "model": "deepseek.v3.2",
        "provider": "custom:bedrock_mantle"
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
print("=== EFFECTIVE BEDROCK CONFIGURATION READY ===")
print("main_model:", config.get("model", {}).get("default"))
print("main_provider:", config.get("model", {}).get("provider"))
print("main_context_length:", config.get("model", {}).get("context_length"))

for task in target_llm_tasks:
    task_cfg = config.get("auxiliary", {}).get(task, {})
    print(f"auxiliary.{task}.model:", task_cfg.get("model"))
    print(f"auxiliary.{task}.provider:", task_cfg.get("provider"))

print("global_fallback_providers:", config.get("fallback_providers"))
print("global_fallback_models:", config.get("fallback_models"))
print("registered_custom_providers:", [p.get("name") for p in config.get("custom_providers", []) if isinstance(p, dict)])
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
