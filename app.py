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

# ১. অটো-সুইচিং এর জন্য ফ্রি মডেলের তালিকা (একটার পর একটা ট্রাই করবে)
FREE_MODELS = [
    "openrouter/auto:free",                           # OpenRouter এর নিজস্ব অটো-রাউটার 
    "google/gemini-2.0-flash-lite-preview-02-05:free",# Gemini-এর রিলায়েবল ফ্রি মডেল
    "meta-llama/llama-3-8b-instruct:free",            # Llama 3 এর ফ্রি ভার্সন
    "google/gemma-2-9b-it:free"                       # Google Gemma-এর ফ্রি ভার্সন
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

    # ২. কন্টিনিউ (Continue) ফিচারের জন্য মেমোরি সাজানো
    past_messages = []
    if supabase:
        try:
            # ডাটাবেজ থেকে শেষের ৫টি চ্যাট আনা হচ্ছে
            history = supabase.table("chat_memory").select("user_message, bot_reply").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
            if history.data:
                # মেসেজগুলো সঠিক সিরিয়ালে সাজানো (অ্যাসিস্ট্যান্টের আগের মেসেজ মনে রাখার জন্য)
                for item in reversed(history.data):
                    past_messages.append({"role": "user", "content": item["user_message"]})
                    past_messages.append({"role": "assistant", "content": item["bot_reply"]})
        except Exception as e:
            print(f"Error reading memory: {e}")

    # সিস্টেম প্রম্পটে কন্টিনিউ করার কমান্ড দেওয়া হলো
    system_prompt = (
        "You are Hermes Agent, a highly capable, autonomous AI assistant. "
        "You have perfect memory of our recent conversation. "
        "If the user asks you to 'continue' or 'go on', smoothly resume exactly from where your last message was cut off without apologizing or restarting."
    )

    # API এর জন্য ফাইনাল মেসেজ লিস্ট তৈরি
    messages_payload = [{"role": "system", "content": system_prompt}]
    messages_payload.extend(past_messages)
    messages_payload.append({"role": "user", "content": user_message})

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    # ৩. অটো-সুইচিং লুপ (Auto-Fallback Mechanism)
    bot_reply = None
    
    for model in FREE_MODELS:
        payload = {
            "model": model,
            "messages": messages_payload
        }
        
        try:
            print(f"Trying model: {model}...")
            # টাইমআউট ২০ সেকেন্ড দেওয়া হয়েছে, যাতে একটা আটকে গেলে দ্রুত অন্যটায় যায়
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            
            if response.status_code == 200:
                res_data = response.json()
                if "choices" in res_data and len(res_data["choices"]) > 0:
                    bot_reply = res_data["choices"][0]["message"]["content"]
                    print(f"Success! Model used: {model}")
                    break  # সফল হলে লুপ থেকে বেরিয়ে যাবে
            else:
                print(f"Model {model} failed with status {response.status_code}")
                
        except Exception as e:
            print(f"Model {model} error: {e}")
            continue  # এরর হলে পরের মডেলে চলে যাবে

    # ৪. রিপ্লাই পেলে তা সেভ করে ইউজারকে পাঠানো
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
        # যদি কোনো ফ্রি মডেলই কাজ না করে
        return jsonify({"error": "All AI models are currently busy. Please try again in a few moments."}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
