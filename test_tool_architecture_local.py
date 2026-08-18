import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_KEY", "local-test-service-role")
os.environ.setdefault("SUPABASE_ANON_KEY", "local-test-anon")

import app as hermes_app
from tool_adapters import BrowserRunnerPendingAdapter
from tool_approval import ApprovalService
from tool_contracts import RiskClass, ToolAvailability, ToolDescriptor, ToolReceipt
from tool_events import VerifiedEventGateway
from tool_executor import ToolExecutor
from tool_policy import ToolPermissionPolicy
from tool_registry import ToolRegistry, default_tool_registry


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class RecordingEvents:
    def __init__(self):
        self.events = []

    def append(self, run_id, owner_id, event):
        self.events.append((run_id, owner_id, event))
        return event


class LocalContractAdapter:
    """A test double only; it is not a browser runner or provider integration."""

    def execute(self, command):
        return ToolReceipt(
            command_id=command.command_id,
            run_id=command.run_id,
            owner_id=command.owner_id,
            observed_state="completed",
            safe_event_code="site_opened",
            action_digest=command.action_digest,
            receipt_signature="local-test-receipt",
        )

    def verify_receipt(self, command, receipt):
        return receipt.receipt_signature == "local-test-receipt"


class InvalidReceiptAdapter(LocalContractAdapter):
    def verify_receipt(self, command, receipt):
        return False


class PassiveThread:
    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        return None


class ToolArchitectureLocalTest(unittest.TestCase):
    OWNER_A = "11111111-1111-1111-1111-111111111111"
    OWNER_B = "22222222-2222-2222-2222-222222222222"

    def setUp(self):
        hermes_app.app.config.update(TESTING=True)
        hermes_app.RUN_REGISTRY.clear()
        self.clock = MutableClock()
        self.recorder = RecordingEvents()
        self.gateway = VerifiedEventGateway(self.recorder.append)
        descriptor = ToolDescriptor(
            tool_id="local_contract_tool",
            display_name="Local contract test tool",
            enabled=True,
            availability=ToolAvailability.AVAILABLE,
            commands={
                "read_permitted_summary": RiskClass.ROUTINE,
                "perform_approved_action": RiskClass.CONSEQUENTIAL,
            },
            allowed_site_ids=("approved_test_site",),
        )
        self.registry = ToolRegistry((descriptor,))
        self.policy = ToolPermissionPolicy(self.registry)
        self.policy.allow_owner_tool(self.OWNER_A, descriptor.tool_id)
        self.approvals = ApprovalService(clock=self.clock, ticket_ttl_seconds=60)
        self.executor = ToolExecutor(
            policy=self.policy,
            approvals=self.approvals,
            events=self.gateway,
            adapters={descriptor.tool_id: LocalContractAdapter()},
            clock=self.clock,
        )

    def _command(self, owner_id=None, command_name="read_permitted_summary"):
        return self.executor.create_server_command(
            owner_id=owner_id or self.OWNER_A,
            run_id="run-a",
            tool_id="local_contract_tool",
            command_name=command_name,
            site_id="approved_test_site",
            permitted_artifact_reference="artifact-safe-id",
        )

    def test_default_registry_is_pending_and_exposes_no_live_browser_capability(self):
        catalog = default_tool_registry().safe_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["availability"], "pending_external_runner_availability")
        self.assertFalse(catalog[0]["enabled"])
        self.assertNotIn("open_url", catalog[0]["commands"])
        self.assertNotIn("click_selector", catalog[0]["commands"])
        self.assertNotIn("evaluate_javascript", catalog[0]["commands"])

    def test_policy_default_denies_unknown_tools_unapproved_owners_commands_and_sites(self):
        unknown = self._command()
        unknown = unknown.__class__(**{**unknown.__dict__, "tool_id": "unknown"})
        self.assertEqual(self.policy.evaluate(unknown).code, "tool_unknown")
        owner_b = self._command(owner_id=self.OWNER_B)
        self.assertEqual(self.policy.evaluate(owner_b).code, "owner_not_allowed")
        bad_command = self._command()
        bad_command = bad_command.__class__(**{**bad_command.__dict__, "command_name": "open_url"})
        self.assertEqual(self.policy.evaluate(bad_command).code, "command_not_allowed")
        bad_site = self._command()
        bad_site = bad_site.__class__(**{**bad_site.__dict__, "site_id": "https://private.invalid/?token=secret"})
        self.assertEqual(self.policy.evaluate(bad_site).code, "site_not_allowed")

    def test_routine_command_completes_only_after_a_matching_verified_receipt(self):
        command = self._command()
        result = self.executor.execute(command)
        self.assertEqual((result.status, result.code), ("completed", "completed"))
        self.assertEqual(
            [event.label for _, _, event in self.recorder.events],
            ["Preparing approved tool task", "Opening approved website", "Task complete"],
        )
        self.assertEqual(sum(1 for _, _, event in self.recorder.events if event.kind == "complete"), 1)

    def test_invalid_receipt_fails_closed_without_raw_adapter_output(self):
        executor = ToolExecutor(
            policy=self.policy,
            approvals=self.approvals,
            events=self.gateway,
            adapters={"local_contract_tool": InvalidReceiptAdapter()},
            clock=self.clock,
        )
        command = executor.create_server_command(self.OWNER_A, "run-b", "local_contract_tool", "read_permitted_summary", "approved_test_site")
        result = executor.execute(command)
        self.assertEqual((result.status, result.code), ("failed", "receipt_invalid"))
        labels = " ".join(event.label for _, _, event in self.recorder.events)
        self.assertNotIn("receipt_signature", labels)
        self.assertNotIn("secret", labels.lower())

    def test_consequential_approval_is_owner_run_digest_bound_single_use_and_expiring(self):
        command = self._command(command_name="perform_approved_action")
        pending = self.executor.execute(command)
        self.assertEqual(pending.status, "waiting")
        self.assertTrue(pending.approval_id)
        owner_mismatch = self._command(owner_id=self.OWNER_B, command_name="perform_approved_action")
        mismatch = self.executor.resume_after_approval(owner_mismatch, pending.approval_id)
        self.assertEqual((mismatch.status, mismatch.code), ("failed", "policy_denied"))
        approved = self.executor.resume_after_approval(command, pending.approval_id)
        self.assertEqual((approved.status, approved.code), ("completed", "completed"))
        replay = self.executor.resume_after_approval(command, pending.approval_id)
        self.assertEqual((replay.status, replay.code), ("failed", "approval_not_valid"))

        expiring = self._command(command_name="perform_approved_action")
        expiring_pending = self.executor.execute(expiring)
        self.clock.value += 61
        expired = self.executor.resume_after_approval(expiring, expiring_pending.approval_id)
        self.assertEqual((expired.status, expired.code), ("failed", "approval_expired"))

    def test_external_code_cannot_inject_activity_events(self):
        self.assertIsNone(self.gateway.emit(object(), "run-a", self.OWNER_A, "completed"))
        self.assertEqual(self.recorder.events, [])
        self.assertIsNone(self.gateway.emit(self.gateway.executor_issuer(), "run-a", self.OWNER_A, "raw_command: cat /private/key"))
        self.assertEqual(self.recorder.events, [])

    def test_gateway_rejects_generic_model_events_but_keeps_verified_tool_activity_owner_run_bound(self):
        command = self._command()
        issuer = self.gateway.executor_issuer()
        self.assertIsNone(self.gateway.emit(issuer, command.run_id, command.owner_id, "analysis"))
        self.assertIsNone(self.gateway.emit(issuer, command.run_id, command.owner_id, "model.reasoning"))
        self.assertEqual(self.recorder.events, [])

        emitted = self.gateway.emit(issuer, command.run_id, command.owner_id, "site_opened")
        self.assertIsNotNone(emitted)
        self.assertEqual(self.recorder.events[0][0:2], (command.run_id, command.owner_id))
        self.assertEqual(self.recorder.events[0][2].label, "Opening approved website")

    def test_pending_browser_adapter_never_connects_or_claims_completion(self):
        pending_registry = default_tool_registry()
        policy = ToolPermissionPolicy(pending_registry)
        policy.allow_owner_tool(self.OWNER_A, "browser_runner")
        pending_events = RecordingEvents()
        pending_executor = ToolExecutor(
            policy=policy,
            approvals=ApprovalService(clock=self.clock),
            events=VerifiedEventGateway(pending_events.append),
            adapters={"browser_runner": BrowserRunnerPendingAdapter()},
            clock=self.clock,
        )
        command = pending_executor.create_server_command(
            self.OWNER_A, "pending-run", "browser_runner", "open_approved_site", "no_live_site"
        )
        result = pending_executor.execute(command)
        self.assertEqual((result.status, result.code), ("failed", "tool_unavailable"))
        self.assertEqual([event.label for _, _, event in pending_events.events], ["The requested tool is not available"])

    def test_tool_routes_are_authenticated_and_cross_account_approval_is_not_disclosed(self):
        run_id = hermes_app._create_progress_run(self.OWNER_A, None, "", [])
        command = self.executor.create_server_command(
            self.OWNER_A, run_id, "local_contract_tool", "perform_approved_action", "approved_test_site"
        )
        hermes_app.RUN_REGISTRY[run_id]["tool_command"] = command
        pending = self.executor.execute(command)
        self.assertEqual(pending.status, "waiting")
        client = hermes_app.app.test_client()
        with patch.object(hermes_app, "TOOL_EXECUTOR", self.executor), \
             patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": self.OWNER_B}, None)):
            self.assertEqual(client.get(f"/tools/runs/{run_id}/approval").status_code, 404)
            self.assertEqual(client.post(f"/tools/runs/{run_id}/approval", json={"approval_id": pending.approval_id}).status_code, 404)
        with patch.object(hermes_app, "TOOL_EXECUTOR", self.executor), \
             patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": self.OWNER_A}, None)), \
             patch.object(hermes_app.threading, "Thread", PassiveThread):
            response = client.get(f"/tools/runs/{run_id}/approval")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["approval_id"], pending.approval_id)
            accepted = client.post(f"/tools/runs/{run_id}/approval", json={"approval_id": pending.approval_id})
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(client.post(f"/tools/runs/{run_id}/approval", json={"approval_id": pending.approval_id}).status_code, 409)

    def test_tool_catalog_requires_existing_authenticated_user_guard(self):
        client = hermes_app.app.test_client()
        with patch.object(hermes_app, "get_auth_header_claims", return_value=(None, None)):
            self.assertEqual(client.get("/tools").status_code, 401)
        with patch.object(hermes_app, "get_auth_header_claims", return_value=({"sub": self.OWNER_A}, None)):
            payload = client.get("/tools").get_json()
        self.assertEqual(payload["tools"][0]["availability"], "pending_external_runner_availability")
        self.assertNotIn("cookie", str(payload).lower())
        self.assertNotIn("token", str(payload).lower())


if __name__ == "__main__":
    unittest.main()
