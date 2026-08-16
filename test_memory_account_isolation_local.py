import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

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


class MemoryAccountIsolationTest(unittest.TestCase):
    """Regression coverage for application-managed, Supabase-owner-scoped context."""

    ACCOUNT_A = "11111111-1111-1111-1111-111111111111"
    ACCOUNT_B = "22222222-2222-2222-2222-222222222222"
    CONVERSATION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    CONVERSATION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def setUp(self):
        hermes_app.app.config.update(TESTING=True)
        hermes_app.HERMES_KEY = "local-test-hermes-key"
        hermes_app.SUPABASE_SERVICE_ROLE_KEY = "local-test-service-role"
        hermes_app.SUPABASE_ANON_KEY = "local-test-anon"
        self.client = hermes_app.app.test_client()

    def _model_messages(self, user_id, conversation_id, persisted_turns, prompt):
        response_payload = {"choices": [{"message": {"content": "Local reply"}}]}
        owned = {"id": conversation_id, "user_id": user_id, "title": "Private chat"}
        with patch.object(hermes_app.requests, "post", return_value=FakeResponse(200, response_payload)) as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": user_id}, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=owned), \
             patch.object(hermes_app, "_get_recent_conversation_context", return_value=persisted_turns), \
             patch.object(hermes_app, "_record_authenticated_chat"), \
             patch.object(hermes_app, "_record_conversation_turn"):
            result = self.client.post(
                "/chat",
                json={"message": prompt, "conversation_id": conversation_id},
            )
        self.assertEqual(result.status_code, 200)
        return post.call_args.kwargs["json"]["messages"]

    def test_account_a_context_is_available_only_to_account_a(self):
        account_a_turns = [
            {"role": "user", "content": "My name is TestUser-A."},
            {"role": "assistant", "content": "I will use TestUser-A in this conversation."},
        ]
        a_messages = self._model_messages(
            self.ACCOUNT_A,
            self.CONVERSATION_A,
            account_a_turns,
            "What is my name?",
        )
        b_messages = self._model_messages(
            self.ACCOUNT_B,
            self.CONVERSATION_B,
            [],
            "What is my name?",
        )
        self.assertIn("TestUser-A", " ".join(message["content"] for message in a_messages))
        self.assertNotIn("TestUser-A", " ".join(message["content"] for message in b_messages))

    def test_reverse_account_b_context_is_not_available_to_account_a(self):
        account_b_turns = [
            {"role": "user", "content": "My name is TestUser-B."},
            {"role": "assistant", "content": "I will use TestUser-B in this conversation."},
        ]
        b_messages = self._model_messages(
            self.ACCOUNT_B,
            self.CONVERSATION_B,
            account_b_turns,
            "What is my name?",
        )
        a_messages = self._model_messages(
            self.ACCOUNT_A,
            self.CONVERSATION_A,
            [],
            "What is my name?",
        )
        self.assertIn("TestUser-B", " ".join(message["content"] for message in b_messages))
        self.assertNotIn("TestUser-B", " ".join(message["content"] for message in a_messages))

    def test_authenticated_context_is_read_only_after_owned_conversation_validation(self):
        with patch.object(hermes_app.requests, "post") as post, \
             patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": self.ACCOUNT_A}, None)), \
             patch.object(hermes_app, "_get_owned_conversation", return_value=None), \
             patch.object(hermes_app, "_get_recent_conversation_context") as context:
            result = self.client.post(
                "/chat",
                json={"message": "Do not read another account's context", "conversation_id": self.CONVERSATION_B},
            )
        self.assertEqual(result.status_code, 404)
        context.assert_not_called()
        post.assert_not_called()

    def test_shared_gateway_profile_memory_and_session_search_are_disabled_at_startup(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "start.sh"), encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn('agent = config.setdefault("agent", {})', source)
        self.assertIn('for toolset_name in ("memory", "session_search"):', source)
        self.assertIn('export HERMES_IGNORE_RULES="true"', source)
        self.assertLess(source.index('export HERMES_IGNORE_RULES="true"'), source.index("hermes gateway run"))

    def test_startup_configuration_persists_global_memory_toolset_lockdown(self):
        root = os.path.dirname(os.path.abspath(hermes_app.__file__))
        with open(os.path.join(root, "start.sh"), encoding="utf-8") as source_file:
            start_source = source_file.read()
        program_start = start_source.index("python3 -c '\n") + len("python3 -c '\n")
        program_end = start_source.index("\n'\n\n# ---------------------------------------------------------\n# 3. Hermes executable check")
        configuration_program = start_source[program_start:program_end]

        with tempfile.TemporaryDirectory() as isolated_home:
            run = subprocess.run(
                [sys.executable, "-c", configuration_program],
                env={**os.environ, "HOME": isolated_home},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            config_path = os.path.join(isolated_home, ".hermes", "config.yaml")
            with open(config_path, encoding="utf-8") as config_file:
                effective_config = yaml.safe_load(config_file)

        disabled = effective_config["agent"]["disabled_toolsets"]
        self.assertIn("memory", disabled)
        self.assertIn("session_search", disabled)

    def test_legacy_memory_write_remains_stamped_with_authenticated_owner(self):
        with patch.object(hermes_app.requests, "post", return_value=FakeResponse(201, [])) as post:
            hermes_app._record_authenticated_chat(self.ACCOUNT_A, "My name is TestUser-A", "Acknowledged")
        self.assertEqual(post.call_args.kwargs["json"]["user_id"], self.ACCOUNT_A)
        self.assertNotEqual(post.call_args.kwargs["json"]["user_id"], self.ACCOUNT_B)


if __name__ == "__main__":
    unittest.main()
