import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Supabase Connection Error: {e}")

# একদম সঠিক ফ্রি মডেলের তালিকা (openrouter/free হলো তাদের ডিফল্ট অটো-রাউটার)
FREE_MODELS = [
    "openrouter/free", 
    "huggingfaceh4/zephyr-7b-beta:free",
    "mistralai/mistral-7b-instruct:free"
]

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Hermes Agent with Fallback & Memory is Live!"}), 200

@app.route("/chat", methods=["POST"])
def chat():
    print("--- New Chat Request Received ---")
    data = request.get_json()
    user_id = data.get("user_id", "default_user")
    user_message = data.get("message", "")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not OPENROUTER_API_KEY:
        return jsonify({"error": "OpenRouter API Key not configured"}), 500

    past_messages = []
    if supabase:
        try:
            history = supabase.table("chat_memory").select("user_message, bot_reply").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
            if history.data:
                for item in reversed(history.data):
                    past_messages.append({"role": "user", "content": item["user_message"]})
                    past_messages.append({"role": "assistant", "content": item["bot_reply"]})
        except Exception as e:
            print(f"Error reading memory: {e}")

    system_prompt = (
        "You are Hermes Agent, a highly capable, autonomous AI assistant. "
        "You have perfect memory of our recent conversation. "
        "If the user asks you to 'continue' or 'go on', smoothly resume exactly from where your last message was cut off without apologizing or restarting."
    )

    messages_payload = [{"role": "system", "content": system_prompt}]
    messages_payload.extend(past_messages)
    messages_payload.append({"role": "user", "content": user_message})

    url = "https://openrouter.ai/api/v1/chat/completions"
    
    # OpenRouter-এর নিয়ম অনুযায়ী Header-এ অ্যাপের নাম ও লিংক যুক্ত করা হলো
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hermes-app-backend-7cso.onrender.com",
        "X-Title": "Hermes AI Application"
    }

    bot_reply = None
    
    for model in FREE_MODELS:
        payload = {
            "model": model,
            "messages": messages_payload
        }
        
        try:
            print(f"Trying model: {model}...")
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            
            if response.status_code == 200:
                res_data = response.json()
                if "choices" in res_data and len(res_data["choices"]) > 0:
                    bot_reply = res_data["choices"][0]["message"]["content"]
                    print(f"Success! Model used: {model}")
                    break
            else:
                print(f"Model {model} failed with status {response.status_code}. Details: {response.text}")
                
        except Exception as e:
            print(f"Model {model} error: {e}")
            continue

    if bot_reply:
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
        return jsonify({"error": "All AI models are currently busy. Please try again in a few moments."}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
