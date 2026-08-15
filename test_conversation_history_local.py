"""Regression tests for the additive conversation-history feature."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_KEY", "local-test-service-role")
os.environ.setdefault("SUPABASE_ANON_KEY", "local-test-anon")

import app as hermes_app


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


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
             patch.object(hermes_app, "_record_conversation_turn") as record_turn:
            response = self.client.post("/chat", json={"message": "Remember this", "conversation_id": self.conversation_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auth_mode"], "authenticated")
        record_turn.assert_called_once_with(self.user_claims["sub"], self.conversation_id, "Remember this", "Local test reply")

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
        with patch.object(hermes_app.requests, "post", return_value=llm), \
             patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)), \
             patch.object(hermes_app, "_record_conversation_turn") as record_turn:
            response = self.client.post("/chat", json={"message": "Anonymous hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auth_mode"], "anonymous")
        record_turn.assert_not_called()


class LocalArtifactTest(unittest.TestCase):
    def test_frontend_has_account_and_conversation_controls(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as source:
            html = source.read()
        self.assertIn("id=\"historyBtn\"", html)
        self.assertIn("id=\"newChatBtn\"", html)
        self.assertIn("window.authGetUser && window.authGetUser()", html)
        self.assertIn("conversation_id", html)

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
