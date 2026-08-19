import json
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from android_device_broker import AndroidDeviceBroker, AndroidDeviceBrokerError
from android_automation_broker import AndroidAutomationBroker, AndroidAutomationBrokerError
from auth import get_auth_header_claims
from tool_adapters import BrowserRunnerPendingAdapter
from tool_approval import ApprovalService
from tool_events import VerifiedEventGateway
from tool_executor import ToolExecutor
from tool_policy import ToolPermissionPolicy
from tool_registry import default_tool_registry

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

# Phase 1 live-progress state is deliberately short-lived and process-local.
# Gunicorn remains a single worker process so owner-scoped stream readers share
# this registry. Nothing here is durable user data or a substitute for History.
RUN_REGISTRY = {}
RUN_REGISTRY_LOCK = threading.RLock()
RUN_TTL_SECONDS = 15 * 60
RUN_EVENT_LIMIT = 80
PUBLIC_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)

# Generic Tool Architecture is intentionally local-only at this stage. The
# registry exposes only the unavailable Browser Runner boundary; it opens no
# browser, profile, remote connection, account, or third-party site.
TOOL_REGISTRY = default_tool_registry()
TOOL_POLICY = ToolPermissionPolicy(TOOL_REGISTRY)
TOOL_APPROVALS = ApprovalService()

# Phase 1 Android companion state is intentionally process-local. It cannot
# issue Android commands, create tool runs, emit Activity events, collect UI
# data, or replace a future approved durable device-registry design.
ANDROID_DEVICE_BROKER = AndroidDeviceBroker()
# Phase 2 remains default-deny because no package profile is configured here.
# It can queue only future explicitly allowlisted certificate-bound commands.
ANDROID_AUTOMATION_BROKER = AndroidAutomationBroker(ANDROID_DEVICE_BROKER)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _public_hostname(value):
    """Return a public hostname only; private, malformed, and path values drop."""
    if not isinstance(value, str) or len(value) > 512:
        return None
    candidate = value.strip()
    if not candidate or any(character in candidate for character in ("\r", "\n", "@", "?", "#")):
        return None
    parsed = urlparse(candidate if "://" in candidate else "//" + candidate)
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or not PUBLIC_HOST_RE.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def _safe_progress_event(run_id, sequence, state, kind, label):
    """Create the only event shape the public frontend is allowed to receive."""
    return {
        "run_id": run_id,
        "sequence": sequence,
        "state": state,
        "kind": kind,
        "label": label,
        "occurred_at": _utc_now(),
    }


def _map_gateway_progress_event(event_name, payload):
    """Deny by default: map known event categories to fixed, non-sensitive labels."""
    if not isinstance(payload, dict):
        return None
    normalized_event = str(event_name or payload.get("type") or "").lower()
    raw_kind = " ".join(
        str(payload.get(field) or "")
        for field in ("type", "event", "tool", "tool_name", "name", "phase", "status")
    ).lower()
    combined = normalized_event + " " + raw_kind

    if any(marker in combined for marker in ("approval_required", "input_required", "requires_action")):
        return ("waiting", "approval", "Waiting for your approval")
    if any(marker in combined for marker in ("cancelled", "canceled", "failed", "error")):
        return ("failed", "failed", "Task could not be completed")
    if "completed" in combined or "run.completed" in combined:
        return None

    source_value = next((payload.get(field) for field in ("hostname", "host", "domain", "url") if payload.get(field)), None)
    hostname = _public_hostname(source_value)
    if any(marker in combined for marker in ("web_search", "search", "browser.search")):
        return ("active", "web_search", "Searching: " + hostname if hostname else "Searching public sources")
    if any(marker in combined for marker in ("fetch", "browse", "read_page", "web_read", "review")):
        return ("active", "source_review", "Reviewing source: " + hostname if hostname else "Reviewing a public source")
    if any(marker in combined for marker in ("compare", "comparison")):
        return ("active", "comparison", "Comparing findings")
    if any(marker in combined for marker in ("test", "pytest", "vitest", "unittest")):
        return ("active", "tests", "Running tests…")
    if any(marker in combined for marker in ("code", "edit", "write_file", "patch", "build")):
        return ("active", "code", "Working on the requested code")
    return None


def _registry_run(run_id, owner_id):
    with RUN_REGISTRY_LOCK:
        run = RUN_REGISTRY.get(run_id)
        if not run or run.get("owner_id") != owner_id:
            return None
        return run


def _append_run_event(run_id, state, kind, label):
    with RUN_REGISTRY_LOCK:
        run = RUN_REGISTRY.get(run_id)
        if not run:
            return None
        run["sequence"] += 1
        event = _safe_progress_event(run_id, run["sequence"], state, kind, label)
        run["events"].append(event)
        del run["events"][:-RUN_EVENT_LIMIT]
        run["status"] = state
        run["updated_at"] = event["occurred_at"]
        return event


def _append_verified_tool_event(run_id, owner_id, safe_event):
    """Bridge executor-owned safe facts into an existing owner-scoped run only."""
    with RUN_REGISTRY_LOCK:
        run = RUN_REGISTRY.get(run_id)
        if not run or run.get("owner_id") != owner_id:
            return None
        if run.get("status") in ("completed", "failed"):
            return None
        return _append_run_event(run_id, safe_event.state, safe_event.kind, safe_event.label)


TOOL_EVENT_GATEWAY = VerifiedEventGateway(_append_verified_tool_event)
TOOL_EXECUTOR = ToolExecutor(
    policy=TOOL_POLICY,
    approvals=TOOL_APPROVALS,
    events=TOOL_EVENT_GATEWAY,
    adapters={"browser_runner": BrowserRunnerPendingAdapter()},
)


def _create_progress_run(owner_id, conversation_id, user_message, conversation_context):
    run_id = str(uuid.uuid4())
    with RUN_REGISTRY_LOCK:
        RUN_REGISTRY[run_id] = {
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "user_message": user_message,
            "conversation_context": conversation_context,
            "upstream_run_id": None,
            "status": "queued",
            "sequence": 0,
            "events": [],
            "reply": None,
            "persisted": False,
            "expires_at": time.time() + RUN_TTL_SECONDS,
            "updated_at": _utc_now(),
        }
    return run_id


def _create_tool_run(owner_id, tool_id, command_name, site_id, permitted_artifact_reference=None):
    """Internal future integration hook; no client route creates arbitrary tools."""
    run_id = _create_progress_run(owner_id, None, "", [])
    command = TOOL_EXECUTOR.create_server_command(
        owner_id=owner_id,
        run_id=run_id,
        tool_id=tool_id,
        command_name=command_name,
        site_id=site_id,
        permitted_artifact_reference=permitted_artifact_reference,
    )
    with RUN_REGISTRY_LOCK:
        if run_id not in RUN_REGISTRY:
            return None, None
        RUN_REGISTRY[run_id]["tool_command"] = command
        RUN_REGISTRY[run_id]["tool_approval_submitted"] = False
    return run_id, TOOL_EXECUTOR.execute(command)


def _gateway_headers():
    return {"Authorization": f"Bearer {HERMES_KEY}", "Content-Type": "application/json"}


def _gateway_supports_runs():
    response = requests.get(f"{HERMES_URL}/v1/capabilities", headers=_gateway_headers(), timeout=8)
    if response.status_code >= 400:
        return False
    capabilities = response.json()
    features = capabilities.get("features") or {}
    return bool(features.get("run_submission") and features.get("run_events_sse"))


def _parse_sse_lines(lines):
    event_name = "message"
    data_lines = []
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = "message", []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _extract_run_reply(status_payload):
    candidate = status_payload.get("output") or status_payload.get("response")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    if isinstance(candidate, list):
        parts = []
        for item in candidate:
            if isinstance(item, dict):
                content = item.get("text") or item.get("content")
                if isinstance(content, str):
                    parts.append(content)
        reply = "\n".join(parts).strip()
        return reply or None
    return None


def _sync_upstream_run_status(run_id):
    run = _registry_run(run_id, RUN_REGISTRY.get(run_id, {}).get("owner_id"))
    if not run or not run.get("upstream_run_id") or run.get("status") in ("completed", "failed", "waiting"):
        return run
    try:
        response = requests.get(
            f"{HERMES_URL}/v1/runs/{run['upstream_run_id']}", headers=_gateway_headers(), timeout=8
        )
        if response.status_code >= 400:
            return run
        payload = response.json()
        upstream_status = str(payload.get("status") or "").lower()
        mapped = _map_gateway_progress_event("run." + upstream_status, payload)
        if mapped:
            _append_run_event(run_id, *mapped)
        return _registry_run(run_id, run["owner_id"])
    except (requests.RequestException, ValueError):
        return run


def _execute_progress_run(run_id):
    """Run the trusted loopback gateway adapter without exposing it to clients."""
    with RUN_REGISTRY_LOCK:
        run = RUN_REGISTRY.get(run_id)
        if not run:
            return
        owner_id = run["owner_id"]
        conversation_id = run["conversation_id"]
        user_message = run["user_message"]
        context = run["conversation_context"]
    try:
        upstream_payload = {
            "model": MODEL_NAME,
            "input": user_message,
            "conversation_history": [
                {"role": item["role"], "content": item["content"]}
                for item in context
                if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)
            ],
        }
        if conversation_id:
            upstream_payload["session_id"] = conversation_id
        create_response = requests.post(
            f"{HERMES_URL}/v1/runs", headers=_gateway_headers(), json=upstream_payload, timeout=20
        )
        if create_response.status_code >= 400:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        created = create_response.json()
        upstream_run_id = created.get("run_id")
        if not isinstance(upstream_run_id, str) or not upstream_run_id:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        with RUN_REGISTRY_LOCK:
            if run_id not in RUN_REGISTRY:
                return
            RUN_REGISTRY[run_id]["upstream_run_id"] = upstream_run_id
            RUN_REGISTRY[run_id]["status"] = "active"

        stream_response = requests.get(
            f"{HERMES_URL}/v1/runs/{upstream_run_id}/events",
            headers=_gateway_headers(),
            stream=True,
            timeout=(10, 210),
        )
        if stream_response.status_code >= 400:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        for event_name, raw_data in _parse_sse_lines(stream_response.iter_lines(decode_unicode=False)):
            if raw_data == "[DONE]":
                continue
            try:
                mapped = _map_gateway_progress_event(event_name, json.loads(raw_data))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                _append_run_event(run_id, *mapped)

        status_response = requests.get(
            f"{HERMES_URL}/v1/runs/{upstream_run_id}", headers=_gateway_headers(), timeout=20
        )
        if status_response.status_code >= 400:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        status_payload = status_response.json()
        upstream_status = str(status_payload.get("status") or "").lower()
        terminal_event = _map_gateway_progress_event("run." + upstream_status, status_payload)
        if upstream_status in ("approval_required", "input_required", "requires_action"):
            _append_run_event(run_id, *(terminal_event or ("waiting", "approval", "Waiting for your approval")))
            return
        if upstream_status != "completed":
            _append_run_event(run_id, *(terminal_event or ("failed", "failed", "Task could not be completed")))
            return
        reply = _extract_run_reply(status_payload)
        if not reply:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        try:
            if owner_id:
                _record_authenticated_chat(owner_id, user_message, reply)
                if conversation_id:
                    _record_conversation_turn(owner_id, conversation_id, user_message, reply)
        except Exception:
            _append_run_event(run_id, "failed", "failed", "Task could not be completed")
            return
        with RUN_REGISTRY_LOCK:
            if run_id not in RUN_REGISTRY:
                return
            RUN_REGISTRY[run_id]["reply"] = reply
            RUN_REGISTRY[run_id]["persisted"] = True
        _append_run_event(run_id, "completed", "complete", "Task complete")
    except (requests.RequestException, ValueError):
        _append_run_event(run_id, "failed", "failed", "Task could not be completed")


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
            # A bulk insert gives each request/reply row the same database
            # timestamp. UUID values are not creation order, so explicitly
            # place the user request before its assistant reply on a tie.
            "order": "created_at.asc,role.desc,id.asc",
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
            # Fetch newest-first efficiently, then reverse below. The inverse
            # role ordering ensures reversal yields user-before-assistant when
            # a persisted request/reply pair shares created_at.
            "order": "created_at.desc,role.asc,id.desc",
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


def _android_pairing_failure(status=400):
    """Keep pairing failures generic and free of key/signature details."""
    return jsonify({"error": "Android companion request could not be verified."}), status


@app.route("/android/devices/pairing-challenge", methods=["POST"])
def create_android_pairing_challenge():
    """Create an owner-bound, short-lived Phase 1 registration challenge only."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    try:
        challenge = ANDROID_DEVICE_BROKER.issue_pairing_challenge(user_id)
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify(
        {
            "pairing_id": challenge.pairing_id,
            "challenge": challenge.challenge,
            "expires_at": challenge.expires_at,
            "phase": "identity_only",
        }
    ), 201


@app.route("/android/devices/pairing-session", methods=["POST"])
def create_android_pairing_session():
    """Display a short-lived, owner-bound pairing code only to the signed-in owner."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    try:
        session = ANDROID_DEVICE_BROKER.create_pairing_session(user_id)
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"pairing_code": session.code, "expires_at": session.expires_at, "phase": "identity_only"}), 201


@app.route("/android/devices/pairing-session/claim", methods=["POST"])
def claim_android_pairing_session():
    """Redeem one code from the native app without accepting a web credential."""
    data = request.get_json(silent=True) or {}
    try:
        challenge = ANDROID_DEVICE_BROKER.claim_pairing_session(data.get("pairing_code"))
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"pairing_id": challenge.pairing_id, "challenge": challenge.challenge, "expires_at": challenge.expires_at, "phase": "identity_only"})


@app.route("/android/devices/register", methods=["POST"])
def register_android_device():
    """Register a signed Android public key; no capability or command is granted."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    data = request.get_json(silent=True) or {}
    try:
        device = ANDROID_DEVICE_BROKER.register_device(
            user_id,
            data.get("pairing_id"),
            data.get("label"),
            data.get("public_key"),
            data.get("registration_nonce"),
            data.get("signature"),
        )
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"device": ANDROID_DEVICE_BROKER.public_device_view(device), "phase": "identity_only"}), 201


@app.route("/android/devices/pairing-register", methods=["POST"])
def register_claimed_android_device():
    """Register only a device that already redeemed one owner-issued pairing code."""
    data = request.get_json(silent=True) or {}
    try:
        device = ANDROID_DEVICE_BROKER.register_paired_device(
            data.get("pairing_id"), data.get("label"), data.get("public_key"),
            data.get("registration_nonce"), data.get("signature"),
        )
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"device": ANDROID_DEVICE_BROKER.public_device_view(device), "phase": "identity_only"}), 201


@app.route("/android/devices", methods=["GET"])
def list_android_devices():
    """Return only the caller's non-sensitive device records."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    try:
        devices = ANDROID_DEVICE_BROKER.list_devices(user_id)
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"devices": devices, "phase": "identity_only"})


@app.route("/android/devices/<device_id>/revoke", methods=["POST"])
def revoke_android_device(device_id):
    """Owner-scoped revocation; it does not alter chat, history, or tool state."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    try:
        revoked = ANDROID_DEVICE_BROKER.revoke_device(user_id, device_id)
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    if not revoked:
        return jsonify({"error": "Android companion was not found."}), 404
    return jsonify({"status": "revoked", "phase": "identity_only"})


@app.route("/android/devices/<device_id>/identity-receipt", methods=["POST"])
def verify_android_identity_receipt(device_id):
    """Verify only a signed no-action identity receipt; it never emits Activity."""
    data = request.get_json(silent=True) or {}
    try:
        ANDROID_DEVICE_BROKER.verify_identity_receipt(
            device_id,
            data.get("receipt_nonce"),
            data.get("sequence"),
            data.get("expires_at"),
            data.get("safe_event_code"),
            data.get("signature"),
        )
    except AndroidDeviceBrokerError:
        return _android_pairing_failure()
    return jsonify({"status": "identity_verified"})


def _android_automation_failure():
    return jsonify({"error": "Android automation request could not be verified."}), 400


@app.route("/android/devices/<device_id>/commands", methods=["POST"])
def queue_android_command(device_id):
    """Queue only a policy-authorized, device-bound command; chat cannot call this route."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").upper()
    package_id = str(data.get("package_id") or "")
    try:
        run_id = _create_progress_run(user_id, None, "", [])
        command = TOOL_EXECUTOR.create_server_command(
            owner_id=user_id,
            run_id=run_id,
            tool_id="android_companion",
            command_name=action,
            site_id=package_id,
            permitted_artifact_reference=None,
        )
        decision = TOOL_EXECUTOR.authorize_deferred(command)
        if decision.status != "dispatched":
            return jsonify({"error": "Android command is not authorized.", "code": decision.code}), 403
        pending = ANDROID_AUTOMATION_BROKER.request_command(
            user_id, device_id, command, package_id, data.get("certificate_sha256"),
            action, data.get("selector_id"), data.get("text"),
        )
    except (AndroidAutomationBrokerError, AndroidDeviceBrokerError, ValueError):
        return _android_automation_failure()
    with RUN_REGISTRY_LOCK:
        RUN_REGISTRY[run_id]["tool_command"] = command
    return jsonify({"command_id": pending.command.command_id, "run_id": run_id, "status": "queued"}), 202


@app.route("/android/devices/<device_id>/commands/poll", methods=["POST"])
def poll_android_command(device_id):
    """Accept a signed device poll and return at most one owner-bound command."""
    data = request.get_json(silent=True) or {}
    try:
        ANDROID_DEVICE_BROKER.verify_device_poll(
            device_id, data.get("poll_nonce"), data.get("sequence"), data.get("expires_at"), data.get("signature")
        )
        command = ANDROID_AUTOMATION_BROKER.next_for_device(device_id)
    except AndroidDeviceBrokerError:
        return _android_automation_failure()
    return jsonify({"command": command})


@app.route("/android/devices/<device_id>/commands/<command_id>/cancel", methods=["POST"])
def cancel_android_command(device_id, command_id):
    """Permit only the command owner to cancel an undelivered device action."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    return jsonify({"status": "cancelled"}) if ANDROID_AUTOMATION_BROKER.cancel(user_id, device_id, command_id) else (jsonify({"error": "Android command was not found."}), 404)


def _accept_android_command_receipt(owner_id, device_id, command_id, data):
    """Accept only a receipt signed by the registered device for its recorded owner."""
    try:
        receipt = ANDROID_AUTOMATION_BROKER.verify_device_receipt(
            owner_id, device_id, command_id, data.get("receipt_nonce"), data.get("sequence"),
            data.get("expires_at"), data.get("outcome"), data.get("signature"),
        )
        with RUN_REGISTRY_LOCK:
            run = RUN_REGISTRY.get(receipt.run_id)
            command = run.get("tool_command") if run and run.get("owner_id") == owner_id else None
        if not command:
            return _android_automation_failure()
        result = TOOL_EXECUTOR.accept_verified_deferred_receipt(command, receipt, ANDROID_AUTOMATION_BROKER)
        if result.code != "verified_device_receipt":
            return _android_automation_failure()
    except (AndroidAutomationBrokerError, AndroidDeviceBrokerError, ValueError):
        return _android_automation_failure()
    return jsonify({"status": "verified_receipt"})


@app.route("/android/devices/<device_id>/commands/<command_id>/receipt", methods=["POST"])
def submit_android_command_receipt(device_id, command_id):
    """Owner-authenticated receipt submission retained for signed-in management UI use."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    return _accept_android_command_receipt(user_id, device_id, command_id, request.get_json(silent=True) or {})


@app.route("/android/devices/<device_id>/commands/<command_id>/device-receipt", methods=["POST"])
def submit_android_device_command_receipt(device_id, command_id):
    """Accept a phone receipt only after device-signature, owner, and run binding verification."""
    try:
        owner_id = ANDROID_DEVICE_BROKER.active_owner_for_device(device_id)
    except AndroidDeviceBrokerError:
        return _android_automation_failure()
    return _accept_android_command_receipt(owner_id, device_id, command_id, request.get_json(silent=True) or {})


@app.route("/chat/runs", methods=["POST"])
def create_chat_run():
    """Refuse public generic chat runs; Activity is reserved for verified tools."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    data = request.get_json(silent=True) or {}
    user_message = str(data.get("message", "")).strip()
    if not user_message:
        return jsonify({"error": "No message provided"}), 400
    if _is_sensitive_cookie_like_message(user_message):
        return jsonify({"error": "This message looks like a browser credential and was not sent or saved."}), 400
    # Normal chat must use /chat. A future server-side tool workflow creates an
    # owner-bound ToolCommand via _create_tool_run and emits Activity only via
    # TOOL_EXECUTOR -> TOOL_EVENT_GATEWAY after verified execution facts.
    return jsonify({"error": "Live Agent Activity is available only for verified tool runs."}), 404


def _run_status_response(run_id, user_id):
    run = _registry_run(run_id, user_id)
    if not run:
        return None
    _sync_upstream_run_status(run_id)
    run = _registry_run(run_id, user_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "last_sequence": run["sequence"],
        "result_ready": bool(run["persisted"] and run["reply"]),
    }


@app.route("/chat/progress/<run_id>", methods=["GET"])
def stream_chat_progress(run_id):
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    if not _registry_run(run_id, user_id):
        return jsonify({"error": "Run was not found."}), 404

    def generate():
        sent_sequence = 0
        idle_ticks = 0
        while idle_ticks < 240:
            run = _registry_run(run_id, user_id)
            if not run:
                return
            pending = [event for event in run["events"] if event["sequence"] > sent_sequence]
            for event in pending:
                sent_sequence = event["sequence"]
                yield "event: progress\ndata: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                if (
                    (event["state"] in ("completed", "failed") and event["kind"] in ("complete", "failed"))
                    or (event["state"] == "waiting" and event["kind"] == "approval")
                ):
                    return
            yield ": keep-alive\n\n"
            time.sleep(1)
            idle_ticks += 1

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/chat/runs/<run_id>", methods=["GET"])
def get_chat_run_status(run_id):
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    status = _run_status_response(run_id, user_id)
    if not status:
        return jsonify({"error": "Run was not found."}), 404
    return jsonify(status), 200


@app.route("/chat/runs/<run_id>/result", methods=["GET"])
def get_chat_run_result(run_id):
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    run = _registry_run(run_id, user_id)
    if not run:
        return jsonify({"error": "Run was not found."}), 404
    if run["status"] != "completed" or not run["persisted"] or not run["reply"]:
        return jsonify({"error": "Run result is not ready."}), 409
    return jsonify({"reply": run["reply"]}), 200


@app.route("/tools", methods=["GET"])
def list_tools():
    """Return a safe registry catalog; availability is not a capability grant."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    return jsonify({"tools": TOOL_REGISTRY.safe_catalog()}), 200


@app.route("/tools/runs/<run_id>/approval", methods=["GET"])
def get_tool_run_approval(run_id):
    """Expose only the caller's pending one-time approval metadata."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    run = _registry_run(run_id, user_id)
    if not run or not run.get("tool_command"):
        return jsonify({"error": "Tool run was not found."}), 404
    ticket = TOOL_EXECUTOR.approval_for_owner_run(user_id, run_id)
    if not ticket:
        return jsonify({"error": "Approval is not available."}), 409
    return jsonify({
        "approval_id": ticket.approval_id,
        "run_id": ticket.run_id,
        "status": "waiting",
        "expires_at": ticket.expires_at,
    }), 200


def _resume_tool_run_after_approval(run_id, owner_id, approval_id):
    """Resume one owner-bound tool run after an authenticated approval submission."""
    with RUN_REGISTRY_LOCK:
        run = RUN_REGISTRY.get(run_id)
        if not run or run.get("owner_id") != owner_id:
            return
        command = run.get("tool_command")
    if command:
        TOOL_EXECUTOR.resume_after_approval(command, approval_id)


@app.route("/tools/runs/<run_id>/approval", methods=["POST"])
def submit_tool_run_approval(run_id):
    """Accept a consent decision only for a verified, owner-scoped pending ticket."""
    user_id, auth_failure = _require_authenticated_user()
    if auth_failure:
        return auth_failure
    data = request.get_json(silent=True) or {}
    approval_id = str(data.get("approval_id") or "")
    run = _registry_run(run_id, user_id)
    if not run or not run.get("tool_command"):
        return jsonify({"error": "Tool run was not found."}), 404
    ticket = TOOL_EXECUTOR.approval_for_owner_run(user_id, run_id)
    if not approval_id or not ticket or ticket.approval_id != approval_id:
        return jsonify({"error": "Approval is not available."}), 409
    with RUN_REGISTRY_LOCK:
        if run.get("tool_approval_submitted"):
            return jsonify({"error": "Approval is not available."}), 409
        run["tool_approval_submitted"] = True
    threading.Thread(
        target=_resume_tool_run_after_approval,
        args=(run_id, user_id, approval_id),
        daemon=True,
    ).start()
    return jsonify({"run_id": run_id, "status": "approval_submitted"}), 202

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
