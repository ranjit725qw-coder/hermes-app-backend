import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

HERMES_URL = os.getenv("HERMES_LOCAL_URL", "http://127.0.0.1:8642")
HERMES_KEY = os.getenv("API_SERVER_KEY")

if not HERMES_KEY:
    print("WARNING: API_SERVER_KEY is not configured.")

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "backend": "Real Hermes Agent",
        "model": "openai/gpt-oss-120b",
        "provider": "custom:groq",
        "base_url": "https://api.groq.com/openai/v1"
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
    user_message = str(data.get("message", "")).strip()

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not HERMES_KEY:
        return jsonify({"error": "Hermes API server key is not configured"}), 500

    headers = {
        "Authorization": f"Bearer {HERMES_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "user", "content": user_message}
        ]
    }

    try:
        response = requests.post(
            f"{HERMES_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=180,
        )
        
        if response.status_code >= 400:
            return jsonify({
                "error": "Hermes Agent returned an error.",
                "detail": response.text
            }), 502
            
        result = response.json()
        reply = result["choices"][0]["message"]["content"]
        
        return jsonify({"reply": reply}), 200

    except Exception as e:
        return jsonify({
            "error": "Could not connect to the local Hermes Agent.",
            "detail": str(e)
        }), 503

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
