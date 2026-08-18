import json
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_KEY", "local-test-service-role")
os.environ.setdefault("SUPABASE_ANON_KEY", "local-test-anon")

import app as hermes_app


class FakeResponse:
    def __init__(self, status_code=200, payload=None, lines=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._lines = lines or []

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class LiveAgentProgressTest(unittest.TestCase):
    def setUp(self):
        hermes_app.app.config.update(TESTING=True)
        hermes_app.HERMES_KEY = "local-test-hermes-key"
        hermes_app.SUPABASE_SERVICE_ROLE_KEY = "local-test-service-role"
        hermes_app.SUPABASE_ANON_KEY = "local-test-anon"
        hermes_app.RUN_REGISTRY.clear()
        self.client = hermes_app.app.test_client()
        self.owner_a = {"sub": "11111111-1111-1111-1111-111111111111"}
        self.owner_b = {"sub": "22222222-2222-2222-2222-222222222222"}

    def test_public_hostname_allows_hostname_but_drops_private_or_full_url(self):
        self.assertEqual(hermes_app._public_hostname("example.com"), "example.com")
        self.assertEqual(hermes_app._public_hostname("docs.example.com"), "docs.example.com")
        self.assertIsNone(hermes_app._public_hostname("localhost"))
        self.assertIsNone(hermes_app._public_hostname("192.168.1.4"))
        self.assertIsNone(hermes_app._public_hostname("https://example.com/private?token=secret"))

    def test_safe_mapper_is_deny_by_default_and_never_echoes_raw_tool_data(self):
        unknown = hermes_app._map_gateway_progress_event(
            "tool.started", {"tool_name": "terminal", "arguments": "cat /srv/private/API_KEY"}
        )
        self.assertIsNone(unknown)
        mapped = hermes_app._map_gateway_progress_event(
            "tool.started",
            {"tool_name": "web_search", "domain": "example.com", "query": "Bearer secret-token"},
        )
        self.assertEqual(mapped, ("active", "web_search", "Searching: example.com"))
        self.assertNotIn("secret", mapped[2].lower())

    def test_generic_model_analysis_and_reasoning_are_not_activity_events(self):
        self.assertIsNone(hermes_app._map_gateway_progress_event("model.analysis", {"status": "analysis"}))
        self.assertIsNone(hermes_app._map_gateway_progress_event("model.reasoning", {"phase": "reasoning"}))

    def test_waiting_and_failure_labels_require_matching_verified_events(self):
        self.assertEqual(
            hermes_app._map_gateway_progress_event("run.input_required", {}),
            ("waiting", "approval", "Waiting for your approval"),
        )
        self.assertEqual(
            hermes_app._map_gateway_progress_event("run.failed", {}),
            ("failed", "failed", "Task could not be completed"),
        )
        self.assertIsNone(hermes_app._map_gateway_progress_event("run.running", {"status": "running"}))

    def test_waiting_event_closes_sse_without_claiming_completion(self):
        run_id = hermes_app._create_progress_run(self.owner_a["sub"], None, "hello", [])
        hermes_app._append_run_event(run_id, "waiting", "approval", "Waiting for your approval")
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)):
            response = self.client.get(f"/chat/progress/{run_id}")
        body = response.get_data(as_text=True)
        self.assertIn("Waiting for your approval", body)
        self.assertNotIn("Task complete", body)

    def test_registry_creates_no_synthetic_activity_before_verified_tool_event(self):
        run_id = hermes_app._create_progress_run(self.owner_a["sub"], None, "private request", [])
        run = hermes_app.RUN_REGISTRY[run_id]
        self.assertEqual(run["events"], [])
        self.assertNotIn("private request", json.dumps(run["events"]))

    def test_owner_b_cannot_read_owner_a_status_progress_or_result(self):
        run_id = hermes_app._create_progress_run(self.owner_a["sub"], None, "hello", [])
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_b, None)):
            self.assertEqual(self.client.get(f"/chat/runs/{run_id}").status_code, 404)
            self.assertEqual(self.client.get(f"/chat/progress/{run_id}").status_code, 404)
            self.assertEqual(self.client.get(f"/chat/runs/{run_id}/result").status_code, 404)

    def test_progress_endpoint_sse_emits_only_safe_serialized_event(self):
        run_id = hermes_app._create_progress_run(self.owner_a["sub"], None, "message with API_KEY=secret", [])
        hermes_app._append_run_event(run_id, "active", "code", "Working on the requested code")
        hermes_app._append_run_event(run_id, "completed", "complete", "Task complete")
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)):
            response = self.client.get(f"/chat/progress/{run_id}")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: progress", body)
        self.assertIn("Task complete", body)
        self.assertNotIn("API_KEY", body)
        self.assertNotIn("secret", body)

    def test_result_requires_verified_persistence_and_owner(self):
        run_id = hermes_app._create_progress_run(self.owner_a["sub"], None, "hello", [])
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)):
            self.assertEqual(self.client.get(f"/chat/runs/{run_id}/result").status_code, 409)
        run = hermes_app.RUN_REGISTRY[run_id]
        run.update({"status": "completed", "persisted": True, "reply": "Safe reply"})
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)):
            response = self.client.get(f"/chat/runs/{run_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"reply": "Safe reply"})

    def test_progress_create_refuses_anonymous_and_sensitive_input(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)):
            self.assertEqual(self.client.post("/chat/runs", json={"message": "hello"}).status_code, 401)
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)):
            response = self.client.post("/chat/runs", json={"message": "__Secure-1PSIDTS=credential"})
        self.assertEqual(response.status_code, 400)

    def test_direct_bengali_and_english_cannot_create_public_activity_runs(self):
        for message in ("হেলো", "Hello"):
            hermes_app.RUN_REGISTRY.clear()
            with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)), \
                 patch.object(hermes_app.threading, "Thread") as thread:
                response = self.client.post("/chat/runs", json={"message": message})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(hermes_app.RUN_REGISTRY, {})
            thread.assert_not_called()

    def test_direct_bengali_and_english_normal_chat_return_without_activity_state(self):
        for message in ("হেলো", "Hello"):
            hermes_app.RUN_REGISTRY.clear()
            upstream = FakeResponse(payload={"choices": [{"message": {"content": "Normal reply"}}]})
            with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.owner_a, None)), \
                 patch.object(hermes_app.requests, "post", return_value=upstream), \
                 patch.object(hermes_app, "_record_authenticated_chat"), \
                 patch.object(hermes_app.threading, "Thread") as thread:
                response = self.client.post("/chat", json={"message": message})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["reply"], "Normal reply")
            self.assertEqual(hermes_app.RUN_REGISTRY, {})
            thread.assert_not_called()

    def test_upstream_sse_parser_keeps_event_boundaries_without_payload_rendering(self):
        frames = list(hermes_app._parse_sse_lines(["event: tool.started", "data: {\"tool_name\":\"web_search\"}", ""]))
        self.assertEqual(frames, [("tool.started", '{"tool_name":"web_search"}')])

    def test_frontend_declares_safe_progress_transport_and_text_rendering(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        self.assertIn("/chat/progress/", html)
        self.assertIn("Agent activity", html)
        self.assertIn("textContent = event.label", html)
        self.assertIn("hermes-auth-cleared", html)
        self.assertIn("PROGRESS_RUN_KEY", html)
        self.assertIn("if (!run || !run.runId || !run.events.length) return;", html)
        self.assertIn("agentActivity-' + run.runId", html)
        self.assertIn("card.dataset.runId = run.runId", html)
        self.assertNotIn("renderAgentActivity(run);\n    await consumeProgressStream(run);", html)
        self.assertNotIn("innerHTML = event.label", html)

    def test_message_rows_omit_user_and_hermes_avatars_but_preserve_header_branding(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        self.assertNotIn("const avatarHtml", html)
        self.assertNotIn("messageEl.innerHTML = avatarHtml + contentHtml", html)
        self.assertNotIn('class="message-avatar"', html)
        self.assertIn('class="typing-avatar"', html)
        self.assertIn('<span class="topbar-title">Hermes AI</span>', html)

    def test_startup_keeps_one_process_with_bounded_threads_for_shared_run_registry(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "start.sh"), encoding="utf-8") as source:
            start_script = source.read()
        self.assertIn("--workers 1", start_script)
        self.assertIn("--threads 8", start_script)
        self.assertIn("--worker-class gthread", start_script)
        self.assertIn("disabled_toolsets", start_script)
        self.assertIn("session_search", start_script)


if __name__ == "__main__":
    unittest.main()
