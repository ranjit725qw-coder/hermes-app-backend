import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

HERMES_URL = os.getenv("HERMES_LOCAL_URL", "http://127.0.0.1:8642")
HERMES_KEY = os.getenv("API_SERVER_KEY")

MODEL_NAME = "gemini-2.5-flash"

if not HERMES_KEY:
    print("WARNING: API_SERVER_KEY is not configured.")

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "backend": "Real Hermes Agent",
        "model": MODEL_NAME,
        "provider": "custom:google_ai_studio"
    }), 200

@app.route("/health", methods=["GET"])
def health():
    try:
        response = requests.get(f"{HERMES_URL}/health", timeout=5)
        return (
            response.text,
            response.status_code,
            {"Content-Type": "application/json"}
        )
    except Exception as exc:
        return jsonify({
            "status": "error",
            "detail": str(exc)
        }), 503

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

    # REST Payload strictly utilizing the explicit model string.
    # Provider mapping handles resolving to Google AI Studio seamlessly.
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": user_message
            }
        ]
    }

    try:
        response = requests.post(
            f"{HERMES_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=180
        )

        if response.status_code >= 400:
            return jsonify({
                "error": "Hermes Agent returned an error.",
                "status_code": response.status_code,
                "detail": response.text
            }), 502

        try:
            result = response.json()
        except ValueError:
            return jsonify({
                "error": "Hermes Agent returned invalid JSON.",
                "detail": response.text
            }), 502

        choices = result.get("choices")
        if not choices:
            return jsonify({
                "error": "Hermes Agent returned no choices.",
                "detail": result
            }), 502

        message = choices[0].get("message", {})
        reply = message.get("content")

        if reply is None:
            return jsonify({
                "error": "Hermes Agent returned no message content.",
                "detail": result
            }), 502

        return jsonify({"reply": reply}), 200

    except requests.Timeout:
        return jsonify({"error": "Hermes Agent request timed out."}), 504
    except requests.RequestException as exc:
        return jsonify({
            "error": "Could not connect to the local Hermes Agent.",
            "detail": str(exc)
        }), 503
    except Exception as exc:
        return jsonify({
            "error": "Unexpected server error.",
            "detail": str(exc)
        }), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
