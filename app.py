import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client

app = Flask(__name__)
# CORS যুক্ত করার ফলে অ্যাপ যেকোনো জায়গা থেকে API কল রিসিভ করতে পারবে
CORS(app)

# Environment Variables
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialize Supabase Client
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Supabase Connection Error: {e}")

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Hermes Agent with Supabase Memory is Live!"}), 200

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_id = data.get("user_id", "default_user")
    user_message = data.get("message", "")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not OPENROUTER_API_KEY:
        return jsonify({"error": "OpenRouter API Key not configured"}), 500

    # Fetch past memory from Supabase
    memory_context = ""
    if supabase:
        try:
            history = supabase.table("chat_memory") \
                .select("user_message, bot_reply") \
                .eq("user_id", user_id) \
                .order("created_at", desc=True) \
                .limit(5) \
                .execute()
            
            if history.data:
                past_chats = []
                for item in reversed(history.data):
                    past_chats.append(f"User: {item['user_message']}\nAgent: {item['bot_reply']}")
                memory_context = "\n---\nPast Conversation Memory:\n" + "\n".join(past_chats) + "\n---"
        except Exception as e:
            print(f"Error reading memory: {e}")

    # System Instructions with Memory
    system_prompt = (
        "You are Hermes Agent, a highly capable, autonomous AI assistant. "
        "You have access to long-term memory of previous user conversations. "
        "Use past context seamlessly without asking repetitive questions."
        f"{memory_context}"
    )

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "google/gemini-2.0-flash-lite-001",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        
        if "choices" in res_data and len(res_data["choices"]) > 0:
            bot_reply = res_data["choices"][0]["message"]["content"]

            # Save current conversation to Supabase Memory
            if supabase:
                try:
                    supabase.table("chat_memory").insert({
                        "user_id": user_id,
                        "user_message": user_message,
                        "bot_reply": bot_reply
                    }).execute()
                except Exception as e:
                    print(f"Error saving memory: {e}")

            return jsonify({"reply": bot_reply}), 200
        else:
            return jsonify({"error": "Failed to get AI response", "details": res_data}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
