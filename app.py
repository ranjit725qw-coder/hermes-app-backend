import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Hermes Agent Backend is Live & Running!"}), 200

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not OPENROUTER_API_KEY:
        return jsonify({"error": "OpenRouter API Key not configured in Render"}), 500

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "google/gemini-2.0-flash-lite-001",
        "messages": [
            {"role": "system", "content": "You are Hermes Agent, a helpful, smart, and autonomous AI assistant for mobile users."},
            {"role": "user", "content": user_message}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        
        if "choices" in res_data and len(res_data["choices"]) > 0:
            bot_reply = res_data["choices"][0]["message"]["content"]
            return jsonify({"reply": bot_reply}), 200
        else:
            return jsonify({"error": "Failed to get response from AI model", "details": res_data}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
