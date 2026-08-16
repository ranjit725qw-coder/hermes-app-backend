"""Regression tests for the additive conversation-history feature."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_KEY", "local-test-service-role")
os.environ.setdefault("SUPABASE_ANON_KEY", "local-test-anon")

import app as hermes_app


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", lines=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self._lines = lines or []

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class ConversationHistoryRoutesTest(unittest.TestCase):
    def setUp(self):
        hermes_app.app.config.update(TESTING=True)
        hermes_app.SUPABASE_SERVICE_ROLE_KEY = "local-test-service-role"
        hermes_app.SUPABASE_ANON_KEY = "local-test-anon"
        hermes_app.HERMES_KEY = "local-test-hermes-key"
        self.client = hermes_app.app.test_client()
        self.user_claims = {"sub": "11111111-1111-1111-1111-111111111111"}
        self.conversation_id = "22222222-2222-2222-2222-222222222222"

    def test_history_requires_authentication(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)):
            response = self.client.get("/conversations")
        self.assertEqual(response.status_code, 401)

    def test_create_conversation_records_authenticated_owner(self):
        created = {
            "id": self.conversation_id,
            "user_id": self.user_claims["sub"],
            "title": "New conversation",
            "created_at": "2026-08-15T00:00:00+00:00",
            "updated_at": "2026-08-15T00:00:00+00:00",
        }
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app.requests, "post", return_value=FakeResponse(201, [created])) as post:
            response = self.client.post("/conversations")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["conversation"]["id"], self.conversation_id)
        self.assertEqual(post.call_args.kwargs["json"]["user_id"], self.user_claims["sub"])

    def test_list_conversations_scopes_query_to_authenticated_owner(self):
        listed = [{"id": self.conversation_id, "title": "First message", "updated_at": "2026-08-15T00:00:00+00:00"}]
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app.requests, "get", return_value=FakeResponse(200, listed)) as get:
            response = self.client.get("/conversations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["conversations"], listed)
        self.assertEqual(get.call_args.kwargs["params"]["user_id"], f"eq.{self.user_claims['sub']}")

    def test_message_restore_rejects_unowned_conversation(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=None):
            response = self.client.get(f"/conversations/{self.conversation_id}/messages")
        self.assertEqual(response.status_code, 404)

    def test_authenticated_chat_records_selected_owned_conversation(self):
        llm = FakeResponse(200, {"choices": [{"message": {"content": "Local test reply"}}]})
        conversation = {"id": self.conversation_id, "title": "New conversation"}
        with patch.object(hermes_app.requests, "post", return_value=llm), \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_record_authenticated_chat"), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=conversation), \
             patch.object(hermes_app, "_get_recent_conversation_context", return_value=[]), \
             patch.object(hermes_app, "_record_conversation_turn") as record_turn:
            response = self.client.post("/chat", json={"message": "Remember this", "conversation_id": self.conversation_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auth_mode"], "authenticated")
        record_turn.assert_called_once_with(self.user_claims["sub"], self.conversation_id, "Remember this", "Local test reply")

    def test_authenticated_continuation_sends_prior_turns_to_model_in_order(self):
        llm = FakeResponse(200, {"choices": [{"message": {"content": "Context-aware reply"}}]})
        conversation = {"id": self.conversation_id, "title": "First question"}
        persisted_turns = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
            {"role": "assistant", "content": "Second answer"},
        ]
        with patch.object(hermes_app.requests, "post", return_value=llm) as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=conversation), \
             patch.object(hermes_app, "_get_recent_conversation_context", return_value=persisted_turns), \
             patch.object(hermes_app, "_record_authenticated_chat"), \
             patch.object(hermes_app, "_record_conversation_turn"):
            response = self.client.post("/chat", json={"message": "Continue from here", "conversation_id": self.conversation_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"]["messages"], persisted_turns + [{"role": "user", "content": "Continue from here"}])

    def test_live_run_terminal_completion_persists_one_owned_turn_after_verified_result(self):
        hermes_app.RUN_REGISTRY.clear()
        run_id = hermes_app._create_progress_run(
            self.user_claims["sub"], self.conversation_id, "Continue from here", []
        )
        upstream_created = FakeResponse(201, {"run_id": "gateway-run-1"})
        stream = FakeResponse(200, {}, ["event: run.completed", "data: {}", ""])
        final_status = FakeResponse(200, {"status": "completed", "output": "Streamed final reply"})
        with patch.object(hermes_app.requests, "post", return_value=upstream_created), \
             patch.object(hermes_app.requests, "get", side_effect=[stream, final_status]), \
             patch.object(hermes_app, "_record_authenticated_chat") as legacy_record, \
             patch.object(hermes_app, "_record_conversation_turn") as conversation_record:
            hermes_app._execute_progress_run(run_id)
        run = hermes_app.RUN_REGISTRY[run_id]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["persisted"])
        self.assertEqual(run["reply"], "Streamed final reply")
        legacy_record.assert_called_once_with(self.user_claims["sub"], "Continue from here", "Streamed final reply")
        conversation_record.assert_called_once_with(
            self.user_claims["sub"], self.conversation_id, "Continue from here", "Streamed final reply"
        )

    def test_live_run_failure_never_persists_partial_turn(self):
        hermes_app.RUN_REGISTRY.clear()
        run_id = hermes_app._create_progress_run(
            self.user_claims["sub"], self.conversation_id, "Do not persist", []
        )
        upstream_created = FakeResponse(201, {"run_id": "gateway-run-2"})
        stream = FakeResponse(200, {}, ["event: run.failed", "data: {}", ""])
        final_status = FakeResponse(200, {"status": "failed"})
        with patch.object(hermes_app.requests, "post", return_value=upstream_created), \
             patch.object(hermes_app.requests, "get", side_effect=[stream, final_status]), \
             patch.object(hermes_app, "_record_authenticated_chat") as legacy_record, \
             patch.object(hermes_app, "_record_conversation_turn") as conversation_record:
            hermes_app._execute_progress_run(run_id)
        self.assertEqual(hermes_app.RUN_REGISTRY[run_id]["status"], "failed")
        legacy_record.assert_not_called()
        conversation_record.assert_not_called()

    def test_history_restore_query_orders_timestamp_ties_user_before_assistant(self):
        rows = [
            {"id": "uuid-a", "role": "user", "content": "Question", "created_at": "2026-08-16T00:00:00+00:00"},
            {"id": "uuid-z", "role": "assistant", "content": "Answer", "created_at": "2026-08-16T00:00:00+00:00"},
        ]
        with patch.object(hermes_app.requests, "get", return_value=FakeResponse(200, rows)) as get:
            restored = hermes_app._get_conversation_messages(self.conversation_id)
        self.assertEqual(restored, rows)
        self.assertEqual(
            get.call_args.kwargs["params"]["order"],
            "created_at.asc,role.desc,id.asc",
        )

    def test_model_context_query_reverses_timestamp_ties_to_user_before_assistant(self):
        newest_first = [
            {"id": "uuid-z", "role": "assistant", "content": "Answer", "created_at": "2026-08-16T00:00:00+00:00"},
            {"id": "uuid-a", "role": "user", "content": "Question", "created_at": "2026-08-16T00:00:00+00:00"},
        ]
        with patch.object(hermes_app.requests, "get", return_value=FakeResponse(200, newest_first)) as get:
            context = hermes_app._get_recent_conversation_context(self.conversation_id)
        self.assertEqual([row["role"] for row in context], ["user", "assistant"])
        self.assertEqual(
            get.call_args.kwargs["params"]["order"],
            "created_at.desc,role.asc,id.desc",
        )

    def test_unowned_continuation_is_rejected_before_model_call(self):
        with patch.object(hermes_app.requests, "post") as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=None):
            response = self.client.post("/chat", json={"message": "Do not send", "conversation_id": self.conversation_id})
        self.assertEqual(response.status_code, 404)
        post.assert_not_called()

    def test_delete_conversation_is_scoped_to_owned_conversation(self):
        owned = {"id": self.conversation_id, "user_id": self.user_claims["sub"]}
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=owned), \
             patch.object(hermes_app.requests, "delete", return_value=FakeResponse(204, [])) as delete:
            response = self.client.delete(f"/conversations/{self.conversation_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(delete.call_args.kwargs["params"]["id"], f"eq.{self.conversation_id}")
        self.assertEqual(delete.call_args.kwargs["params"]["user_id"], f"eq.{self.user_claims['sub']}")

    def test_delete_conversation_rejects_unowned_id_without_delete_call(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=None), \
             patch.object(hermes_app.requests, "delete") as delete:
            response = self.client.delete(f"/conversations/{self.conversation_id}")
        self.assertEqual(response.status_code, 404)
        delete.assert_not_called()

    def test_saved_messages_include_authenticated_owner_required_by_schema(self):
        with patch.object(hermes_app.requests, "post", return_value=FakeResponse(201, [])) as post, \
             patch.object(hermes_app.requests, "patch", return_value=FakeResponse(204, [])), \
             patch.object(hermes_app, "_get_owned_conversation", return_value={"title": "New conversation"}):
            hermes_app._record_conversation_turn(
                self.user_claims["sub"],
                self.conversation_id,
                "First saved message",
                "Saved assistant reply",
            )
        inserted_messages = post.call_args.kwargs["json"]
        self.assertEqual({item["user_id"] for item in inserted_messages}, {self.user_claims["sub"]})
        self.assertEqual([item["role"] for item in inserted_messages], ["user", "assistant"])

    def test_anonymous_chat_stays_without_conversation_persistence(self):
        llm = FakeResponse(200, {"choices": [{"message": {"content": "Anonymous reply"}}]})
        with patch.object(hermes_app.requests, "post", return_value=llm) as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)), \
             patch.object(hermes_app, "_record_conversation_turn") as record_turn:
            response = self.client.post("/chat", json={"message": "Anonymous hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auth_mode"], "anonymous")
        self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "Anonymous hello"}])
        record_turn.assert_not_called()

    def test_authenticated_cookie_like_message_is_rejected_before_model_or_persistence(self):
        with patch.object(hermes_app.requests, "post") as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(self.user_claims, None)), \
             patch.object(hermes_app, "_record_authenticated_chat") as legacy_record, \
             patch.object(hermes_app, "_record_conversation_turn") as conversation_record:
            response = self.client.post("/chat", json={"message": "__Secure-1PSIDTS=credential-like-value"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not sent or saved", response.get_json()["error"])
        post.assert_not_called()
        legacy_record.assert_not_called()
        conversation_record.assert_not_called()

    def test_anonymous_cookie_like_text_preserves_anonymous_chat_boundary(self):
        llm = FakeResponse(200, {"choices": [{"message": {"content": "Anonymous reply"}}]})
        with patch.object(hermes_app.requests, "post", return_value=llm) as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)):
            response = self.client.post("/chat", json={"message": "__Secure-1PSIDTS=anonymous-text"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "__Secure-1PSIDTS=anonymous-text"}])


class LocalArtifactTest(unittest.TestCase):
    def test_frontend_has_account_and_conversation_controls(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        self.assertIn("id=\"historyBtn\"", html)
        self.assertIn("id=\"newChatBtn\"", html)
        self.assertIn("window.authGetUser && window.authGetUser()", html)
        self.assertIn("conversation_id", html)
        self.assertIn("id=\"logoutBtn\"", html)
        self.assertIn("window.authSignOut", html)
        self.assertIn("deleteConversation", html)
        self.assertIn("method: 'DELETE'", html)
        self.assertIn("overflow-wrap: anywhere", html)
        self.assertIn("min-width: 0", html)
        self.assertIn("if (response.status === 204) return {};", html)
        self.assertIn("method: 'POST'", html)
        self.assertIn("/auth/v1/logout", html)
        self.assertIn("Authorization': 'Bearer ' + token", html)
        self.assertIn("code });", html)
        self.assertNotIn("code: code.trim()", html)
        self.assertIn('id="confirmDialog"', html)
        self.assertIn("function requestConfirmation", html)
        self.assertNotIn("window.confirm(", html)
        self.assertIn("authClear();\n      authUpdateUI();", html)
        self.assertIn("return typeof origSignOut === 'function' ? origSignOut() : Promise.resolve();", html)

    def test_frontend_uses_in_page_confirmation_and_refreshes_history_after_delete(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        delete_request = "await historyFetch('/conversations/' + encodeURIComponent(conversation.id), { method: 'DELETE' });"
        self.assertIn(delete_request, html)
        self.assertIn("await loadConversations();", html)
        self.assertLess(html.index(delete_request), html.index("await loadConversations();", html.index(delete_request)))
        self.assertIn("title: 'Delete this conversation?'", html)
        self.assertIn("title: 'Log out from Hermes AI?'", html)

    def test_frontend_uses_active_conversation_id_for_chat_context(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        self.assertIn("state.activeConversationId ? { conversation_id: state.activeConversationId }", html)
        self.assertIn("state.activeConversationId = data.conversation.id", html)

    def test_migration_is_additive_and_does_not_modify_legacy_memory(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        migration = os.path.join(root, "migrations", "20260815_add_chat_conversations_and_messages.sql")
        with open(migration, encoding="utf-8") as source:
            sql = source.read().lower()
        self.assertIn("create table if not exists public.chat_conversations", sql)
        self.assertIn("create table if not exists public.chat_messages", sql)
        self.assertIn("alter table public.chat_conversations enable row level security", sql)
        self.assertIn("alter table public.chat_messages enable row level security", sql)
        self.assertIn("revoke all on table public.chat_conversations from anon, authenticated", sql)
        self.assertIn("revoke all on table public.chat_messages from anon, authenticated", sql)
        self.assertNotIn("alter table public.chat_memory", sql)
        self.assertNotIn("drop table", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
