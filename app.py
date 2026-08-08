import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# Browser access is controlled by the Render environment variable.
# Example:
# API_SERVER_CORS_ORIGINS=https://your-frontend.example.com
CORS(app, origins=os.getenv("API_SERVER_CORS_ORIGINS", "*").split(","))

HERMES_URL = os.getenv("HERMES_LOCAL_URL", "http://127.0.0.1:8642")
HERMES_KEY = os.getenv("API_SERVER_KEY")

if not HERMES_KEY:
    print("WARNING: API_SERVER_KEY is not configured.")

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "backend": "Hermes Agent",
        "mode": "real-hermes-agent"
    }), 200

@app.route("/health", methods=["GET"])
def health():
    try:
        r = requests.get(f"{HERMES_URL}/health", timeout=5)
        return (r.text, r.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_id = str(data.get("user_id", "default_user"))
    user_message = str(data.get("message", "")).strip()

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not HERMES_KEY:
        return jsonify({"error": "Hermes API server key is not configured"}), 500

    # The browser frontend can keep using its existing /chat contract.
    # This proxy forwards the request to the REAL Hermes Agent API.
    headers = {
        "Authorization": f"Bearer {HERMES_KEY}",
        "Content-Type": "application/json",
        "X-Hermes-Session-Key": f"web:{user_id}",
    }

    payload = {
        "model": "hermes-agent",
        "input": user_message,
        "conversation": f"web:{user_id}",
        "store": True,
    }

    try:
        response = requests.post(
            f"{HERMES_URL}/v1/responses",
            headers=headers,
            json=payload,
            timeout=180,
        )
    except requests.RequestException as e:
        return jsonify({
            "error": "Could not connect to the Hermes Agent runtime.",
            "detail": str(e)
        }), 503

    if response.status_code >= 400:
        return jsonify({
            "error": "Hermes Agent returned an error.",
            "status": response.status_code,
            "detail": response.text[:4000],
        }), 502

    try:
        result = response.json()
    except ValueError:
        return jsonify({"error": "Invalid response from Hermes Agent"}), 502

    # Extract the final assistant text from the Responses API result.
    reply_parts = []
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                text = content.get("text")
                if text:
                    reply_parts.append(text)

    reply = "\n".join(reply_parts).strip()

    if not reply:
        return jsonify({
            "error": "Hermes Agent completed without returning assistant text.",
            "response_id": result.get("id")
        }), 502

    return jsonify({
        "reply": reply,
        "response_id": result.get("id"),
        "model": result.get("model", "hermes-agent")
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
