import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as hermes_app

from youtube_open_app import (
    YOUTUBE_LAUNCH_SELECTOR_ID,
    YOUTUBE_OPEN_ACTION,
    YOUTUBE_PACKAGE_ID,
    is_valid_certificate_sha256,
    is_youtube_open_request,
)
from android_automation_broker import AndroidAutomationBrokerError


class YouTubeOpenAppIntentTest(unittest.TestCase):
    def test_only_explicit_youtube_open_phrases_match(self):
        self.assertTrue(is_youtube_open_request("Open YouTube"))
        self.assertTrue(is_youtube_open_request("YouTube অ্যাপটি খুলে দাও"))
        self.assertFalse(is_youtube_open_request("Tell me about YouTube"))
        self.assertFalse(is_youtube_open_request("Open my banking app"))
        self.assertFalse(is_youtube_open_request("youtube.com"))

    def test_contract_is_fixed_to_one_package_one_action(self):
        self.assertEqual("com.google.android.youtube", YOUTUBE_PACKAGE_ID)
        self.assertEqual("OPEN_APP", YOUTUBE_OPEN_ACTION)
        self.assertEqual("youtube_launch", YOUTUBE_LAUNCH_SELECTOR_ID)

    def test_certificate_format_is_strict_sha256_hex(self):
        self.assertTrue(is_valid_certificate_sha256("a" * 64))
        self.assertTrue(is_valid_certificate_sha256("A" * 64))
        self.assertFalse(is_valid_certificate_sha256("a" * 63))
        self.assertFalse(is_valid_certificate_sha256("g" * 64))


class YouTubeChatDiagnosticTest(unittest.TestCase):
    MESSAGE = "YouTube অ্যাপটি খুলে দাও"

    def setUp(self):
        self.previous_hermes_key = hermes_app.HERMES_KEY
        hermes_app.HERMES_KEY = "local-test-hermes-key"
        self.client = hermes_app.app.test_client()

    def tearDown(self):
        hermes_app.HERMES_KEY = self.previous_hermes_key

    @staticmethod
    def _model_reply():
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "Normal reply"}}]},
        )

    def _assert_safe_diagnostic(self, log_call, auth_mode, adapter_outcome):
        self.assertEqual(
            (
                "youtube_chat_diagnostic auth_mode=%s adapter_outcome=%s",
                auth_mode,
                adapter_outcome,
            ),
            log_call.args,
        )
        logged = str(log_call.args)
        for prohibited_value in (self.MESSAGE, "Bearer", "Authorization", "local-owner-id", "response", "claim"):
            self.assertNotIn(prohibited_value, logged)

    def test_anonymous_youtube_request_logs_only_categorical_auth_and_outcome(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)), \
             patch.object(hermes_app.requests, "post", return_value=self._model_reply()), \
             patch.object(hermes_app.app.logger, "warning") as diagnostic_log:
            response = self.client.post("/chat", json={"message": self.MESSAGE})

        self.assertEqual(200, response.status_code)
        self._assert_safe_diagnostic(diagnostic_log.call_args, "anonymous", "not_authenticated")

    def test_authenticated_request_without_record_uid_logs_bounded_outcome(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=({"aud": "authenticated"}, None)), \
             patch.object(hermes_app.requests, "post", return_value=self._model_reply()), \
             patch.object(hermes_app.app.logger, "warning") as diagnostic_log:
            response = self.client.post("/chat", json={"message": self.MESSAGE})

        self.assertEqual(200, response.status_code)
        self._assert_safe_diagnostic(diagnostic_log.call_args, "authenticated", "missing_record_uid")

    def test_authenticated_adapter_denial_logs_only_bounded_error_code(self):
        with patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": "local-owner-id"}, None)), \
             patch.object(hermes_app, "_create_youtube_open_run", return_value=(None, "owner_not_allowed")), \
             patch.object(hermes_app.app.logger, "warning") as diagnostic_log:
            response = self.client.post("/chat", json={"message": self.MESSAGE})

        self.assertEqual(200, response.status_code)
        self.assertEqual("owner_not_allowed", response.get_json()["youtube_launch"]["status"])
        self._assert_safe_diagnostic(diagnostic_log.call_args, "authenticated", "owner_not_allowed")

    def test_diagnostic_normalizes_any_unapproved_value_before_logging(self):
        with patch.object(hermes_app.app.logger, "warning") as diagnostic_log:
            hermes_app._log_youtube_chat_diagnostic("untrusted-auth-mode", "Bearer private-token")

        self._assert_safe_diagnostic(
            diagnostic_log.call_args,
            "anonymous",
            "unexpected_adapter_outcome",
        )

    def test_pre_dispatch_diagnostic_normalizes_fixed_broker_failures(self):
        self.assertEqual(
            "active_device_unavailable",
            hermes_app._safe_youtube_pre_dispatch_denial_category(
                AndroidAutomationBrokerError("device_unavailable"),
            ),
        )
        self.assertEqual(
            "fixed_policy_rejected",
            hermes_app._safe_youtube_pre_dispatch_denial_category(
                AndroidAutomationBrokerError("package_not_allowed"),
            ),
        )
        self.assertEqual(
            "pre_dispatch_rejected",
            hermes_app._safe_youtube_pre_dispatch_denial_category(
                AndroidAutomationBrokerError("untrusted internal text"),
            ),
        )
