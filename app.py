import json
import os
import uuid
from datetime import datetime, timezone
import requests
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from auth import get_auth_header_claims

app = Flask(__name__)
CORS(app)

HERMES_URL = os.getenv("HERMES_LOCAL_URL", "http://127.0.0.1:8642")
HERMES_KEY = os.getenv("API_SERVER_KEY")

# ACTIVE model (Amazon Bedrock, verified Aug 13 2026):
MODEL_NAME = "deepseek.v3.2"
# GEMINI (preserved, deactivated — re-enable by swapping):
# MODEL_NAME = "gemini-3.5-flash"

if not HERMES_KEY:
    print("WARNING: API_SERVER_KEY is not configured.")

SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL") or "https://bjoljeysryycwflhcnha.supabase.co"


def _record_authenticated_chat(user_id, user_message, bot_reply):
    """
    Optionally record an authenticated chat turn in the existing
    chat_memory table (Supabase `hermes-memory-db`).

    Anonymous (no token) requests never reach this function, so legacy
    anonymous behavior is unchanged. Failures here are logged but never
    break the chat response.
    """
    if not SUPABASE_SERVICE_ROLE_KEY or not user_id:
        return
    try:
        requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_memory",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "apikey": SUPABASE_ANON_KEY or "",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "user_id": user_id,
                "user_message": user_message,
                "bot_reply": bot_reply,
            },
            timeout=20,
        )
    except Exception:
        # Recording is best-effort; the chat reply already succeeded.
        pass


def _supabase_headers(prefer=None):
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_ANON_KEY or "",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _require_authenticated_user():
    claims, auth_err = get_auth_header_claims(request)
    if auth_err:
        return None, (jsonify({"error": "Invalid authentication token."}), 401)
    if not claims:
        return None, (jsonify({"error": "Authentication is required."}), 401)
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        return None, (jsonify({"error": "Authenticated user identity is missing."}), 401)
    return user_id, None


def _conversation_title(message):
    normalized = " ".join(str(message or "").split())
    return (normalized[:72] or "New conversation")


def _valid_uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _get_owned_conversation(user_id, conversation_id):
    response = requests.get(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_conversations",
        headers=_supabase_headers(),
        params={
            "select": "id,title,created_at,updated_at",
            "id": f"eq.{conversation_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError("Conversation store is unavailable.")
    rows = response.json()
    return rows[0] if rows else None


def _get_conversation_messages(conversation_id, limit=40):
    response = requests.get(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_messages",
        headers=_supabase_headers(),
        params={
            "select": "id,role,content,created_at",
            "conversation_id": f"eq.{conversation_id}",
            "order": "created_at.asc,id.asc",
            "limit": str(limit),
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError("Conversation messages are unavailable.")
    return response.json()


def _get_recent_conversation_context(conversation_id, limit=40):
    """Return the most recent persisted turns in chronological model order."""
    response = requests.get(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_messages",
        headers=_supabase_headers(),
        params={
            "select": "role,content,created_at,id",
            "conversation_id": f"eq.{conversation_id}",
            "order": "created_at.desc,id.desc",
            "limit": str(limit),
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError("Conversation messages are unavailable.")
    rows = response.json()
    return list(reversed(rows))


def _is_sensitive_cookie_like_message(message):
    """Reject a narrowly identified cookie-shaped credential prefix prospectively."""
    normalized = str(message or "").lstrip().lower()
    return normalized.startswith("__secure-1psidts") or "__secure-1psidts=" in normalized[:256]


def _record_conversation_turn(user_id, conversation_id, user_message, bot_reply):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Conversation storage is not configured.")

    now = datetime.now(timezone.utc).isoformat()
    response = requests.post(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_messages",
        headers=_supabase_headers("return=minimal"),
        json=[
            {"conversation_id": conversation_id, "user_id": user_id, "role": "user", "content": user_message},
            {"conversation_id": conversation_id, "user_id": user_id, "role": "assistant", "content": bot_reply},
        ],
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError("Conversation message persistence failed.")

    update = {"updated_at": now}
    conversation = _get_owned_conversation(user_id, conversation_id)
    if conversation and conversation.get("title") == "New conversation":
        update["title"] = _conversation_title(user_message)
    response = requests.patch(
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_conversations",
        headers=_supabase_headers("return=minimal"),
        params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
        json=update,
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError("Conversation update failed.")


@app.route("/conversations", methods=["GET"])
def list_conversations():
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"error": "Conversation storage is not configured."}), 503
    try:
        response = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_conversations",
            headers=_supabase_headers(),
            params={
                "select": "id,title,created_at,updated_at",
                "user_id": f"eq.{user_id}",
                "order": "updated_at.desc",
                "limit": "100",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError("Conversation store is unavailable.")
        return jsonify({"conversations": response.json()}), 200
    except (requests.RequestException, ValueError, RuntimeError):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503


@app.route("/conversations", methods=["POST"])
def create_conversation():
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"error": "Conversation storage is not configured."}), 503
    conversation = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": "New conversation",
    }
    try:
        response = requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_conversations",
            headers=_supabase_headers("return=representation"),
            json=conversation,
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError("Conversation creation failed.")
        rows = response.json()
        return jsonify({"conversation": rows[0] if rows else conversation}), 201
    except (requests.RequestException, ValueError, RuntimeError):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503


@app.route("/conversations/<conversation_id>/messages", methods=["GET"])
def get_conversation_messages(conversation_id):
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    normalized_id = _valid_uuid(conversation_id)
    if not normalized_id:
        return jsonify({"error": "Conversation was not found."}), 404
    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"error": "Conversation storage is not configured."}), 503
    try:
        conversation = _get_owned_conversation(user_id, normalized_id)
        if not conversation:
            return jsonify({"error": "Conversation was not found."}), 404
        return jsonify({"conversation": conversation, "messages": _get_conversation_messages(normalized_id)}), 200
    except (requests.RequestException, ValueError, RuntimeError):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503


@app.route("/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    normalized_id = _valid_uuid(conversation_id)
    if not normalized_id:
        return jsonify({"error": "Conversation was not found."}), 404
    if not SUPABASE_SERVICE_ROLE_KEY:
        return jsonify({"error": "Conversation storage is not configured."}), 503
    try:
        conversation = _get_owned_conversation(user_id, normalized_id)
        if not conversation:
            return jsonify({"error": "Conversation was not found."}), 404
        response = requests.delete(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/chat_conversations",
            headers=_supabase_headers("return=minimal"),
            params={"id": f"eq.{normalized_id}", "user_id": f"eq.{user_id}"},
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError("Conversation deletion failed.")
        return "", 204
    except (requests.RequestException, ValueError, RuntimeError):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "backend": "Real Hermes Agent",
        "model": MODEL_NAME,
        "provider": "custom:bedrock_mantle"
    }), 200


@app.route("/app", methods=["GET"])
def app_page():
    # Serves the auth-enabled frontend (Phase 3-A: Google Sign-In)
    # from the registered HTTPS origin so Google Identity Services
    # (Sign-In button iframe + popup) works. Additive only: existing
    # GET / JSON identity endpoint is untouched.
    frontend_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "frontend", "index.html"
    )
    with open(frontend_path, "r", encoding="utf-8") as frontend_file:
        html = frontend_file.read()
    runtime_config = json.dumps({"supabaseAnonKey": SUPABASE_ANON_KEY or ""})
    runtime_config = runtime_config.replace("</", "<\\/")
    return Response(
        html.replace("__HERMES_RUNTIME_CONFIG__", runtime_config),
        mimetype="text/html",
    )

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
    requested_conversation_id = data.get("conversation_id")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not HERMES_KEY:
        return jsonify({"error": "Hermes API server key is not configured"}), 500

    # Keep anonymous chat exactly single-turn. For an authenticated selected
    # conversation, validate ownership and assemble durable prior turns before
    # calling the model so an old/reopened chat can continue coherently.
    auth_mode = "anonymous"
    record_uid = None
    conversation_id = None
    conversation_context = []
    try:
        claims, auth_err = get_auth_header_claims(request)
        if auth_err:
            return jsonify({"error": "Invalid authentication token.", "detail": auth_err}), 401
        if claims:
            auth_mode = "authenticated"
            if _is_sensitive_cookie_like_message(user_message):
                return jsonify({
                    "error": "This message looks like a browser credential and was not sent or saved."
                }), 400
            record_uid = claims.get("sub") or claims.get("user_id")
            if requested_conversation_id:
                conversation_id = _valid_uuid(requested_conversation_id)
                if not conversation_id:
                    return jsonify({"error": "Conversation was not found."}), 404
                if not record_uid or not _get_owned_conversation(record_uid, conversation_id):
                    return jsonify({"error": "Conversation was not found."}), 404
                conversation_context = _get_recent_conversation_context(conversation_id)
    except (requests.RequestException, ValueError, RuntimeError):
        return jsonify({"error": "Conversation history is temporarily unavailable."}), 503

    headers = {
        "Authorization": f"Bearer {HERMES_KEY}",
        "Content-Type": "application/json"
    }

    # REST Payload strictly utilizing the explicit model string.
    # Provider mapping handles resolving to Amazon Bedrock
    # (bedrock-mantle Chat Completions) seamlessly.
    messages = [
        {"role": row["role"], "content": row["content"]}
        for row in conversation_context
        if row.get("role") in ("user", "assistant") and isinstance(row.get("content"), str) and row["content"]
    ]
    messages.append({"role": "user", "content": user_message})
    payload = {"model": MODEL_NAME, "messages": messages}

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

        # Phase 3-A: optional identity layer. Anonymous requests continue to
        # bypass all persistence. Authenticated requests retain legacy
        # chat_memory recording and selected-conversation persistence.
        try:
            if record_uid:
                _record_authenticated_chat(record_uid, user_message, reply)
                if conversation_id:
                    _record_conversation_turn(
                        record_uid,
                        conversation_id,
                        user_message,
                        reply,
                    )
        except Exception:
            # Never break chat on identity-layer exceptions.
            pass

        return jsonify({"reply": reply, "auth_mode": auth_mode}), 200

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


@app.route("/auth/verify", methods=["GET"])
def auth_verify():
    """
    Validate the caller's Supabase access token and return identity claims.

    * Valid Bearer token    -> 200 {uid, email, role, expires_at}
    * No token (anonymous)  -> 200 {auth_mode: "anonymous"}
    * Invalid/expired token -> 401 {error}

    The token value is never logged or echoed.
    """
    claims, err = get_auth_header_claims(request)
    if err:
        return jsonify({"error": err}), 401
    if not claims:
        return jsonify({"auth_mode": "anonymous"}), 200
    return jsonify({
        "auth_mode": "authenticated",
        "uid": claims.get("sub"),
        "email": claims.get("email"),
        "role": claims.get("role"),
        "expires_at": claims.get("exp"),
    }), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
