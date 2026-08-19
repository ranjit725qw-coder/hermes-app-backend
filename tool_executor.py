"""Fail-closed executor for only server-created, policy-approved ToolCommands."""

from __future__ import annotations

import time
import uuid
from typing import Dict, Optional

from tool_adapters import ToolAdapter, ToolAdapterUnavailable
from tool_approval import ApprovalService
from tool_contracts import ToolCommand, ToolExecutionResult, ToolReceipt, build_action_digest
from tool_events import VerifiedEventGateway
from tool_policy import ToolPermissionPolicy


class ToolExecutor:
    def __init__(
        self,
        policy: ToolPermissionPolicy,
        approvals: ApprovalService,
        events: VerifiedEventGateway,
        adapters: Optional[Dict[str, ToolAdapter]] = None,
        clock=time.time,
    ):
        self._policy = policy
        self._approvals = approvals
        self._events = events
        self._issuer = events.executor_issuer()
        self._adapters = dict(adapters or {})
        self._clock = clock

    def create_server_command(
        self,
        owner_id: str,
        run_id: str,
        tool_id: str,
        command_name: str,
        site_id: str,
        permitted_artifact_reference: Optional[str] = None,
        ttl_seconds: int = 120,
    ) -> ToolCommand:
        expiry = self._clock() + max(1, min(int(ttl_seconds), 300))
        digest = build_action_digest(
            owner_id,
            run_id,
            tool_id,
            command_name,
            site_id,
            permitted_artifact_reference,
        )
        return ToolCommand(
            command_id=str(uuid.uuid4()),
            run_id=str(run_id),
            owner_id=str(owner_id),
            tool_id=str(tool_id),
            command_name=str(command_name),
            site_id=str(site_id),
            permitted_artifact_reference=(str(permitted_artifact_reference) if permitted_artifact_reference else None),
            action_digest=digest,
            expires_at=expiry,
        )

    def execute(self, command: ToolCommand) -> ToolExecutionResult:
        if command.expires_at <= self._clock():
            self._emit(command, "execution_failed")
            return ToolExecutionResult("failed", "command_expired")
        decision = self._policy.evaluate(command)
        if not decision.allowed:
            self._emit(command, "runner_unavailable" if decision.code == "tool_unavailable" else "policy_denied")
            return ToolExecutionResult("failed", decision.code)
        self._emit(command, "command_accepted")
        if decision.approval_required:
            ticket = self._approvals.mint(command)
            self._emit(command, "approval_requested")
            return ToolExecutionResult("waiting", "approval_required", ticket.approval_id)
        return self._execute_adapter(command)

    def resume_after_approval(self, command: ToolCommand, approval_id: str) -> ToolExecutionResult:
        decision = self._policy.evaluate(command)
        if not decision.allowed or not decision.approval_required:
            self._emit(command, "policy_denied")
            return ToolExecutionResult("failed", "policy_denied")
        approval = self._approvals.consume(approval_id, command)
        if not approval.approved:
            self._emit(command, "approval_expired" if approval.code == "approval_expired" else "approval_invalid")
            return ToolExecutionResult("failed", approval.code)
        self._emit(command, "approval_received")
        return self._execute_adapter(command)

    def authorize_deferred(self, command: ToolCommand) -> ToolExecutionResult:
        """Authorize a queued hardware action without emitting Activity before a receipt."""
        if command.expires_at <= self._clock():
            return ToolExecutionResult("failed", "command_expired")
        decision = self._policy.evaluate(command)
        if not decision.allowed:
            return ToolExecutionResult("failed", decision.code)
        if decision.approval_required:
            ticket = self._approvals.mint(command)
            return ToolExecutionResult("waiting", "approval_required", ticket.approval_id)
        return ToolExecutionResult("dispatched", "awaiting_device_receipt")

    def resume_deferred_after_approval(self, command: ToolCommand, approval_id: str) -> ToolExecutionResult:
        decision = self._policy.evaluate(command)
        if not decision.allowed or not decision.approval_required:
            return ToolExecutionResult("failed", "policy_denied")
        approval = self._approvals.consume(approval_id, command)
        if not approval.approved:
            return ToolExecutionResult("failed", approval.code)
        return ToolExecutionResult("dispatched", "awaiting_device_receipt")

    def accept_verified_deferred_receipt(self, command: ToolCommand, receipt: ToolReceipt, verifier) -> ToolExecutionResult:
        """The sole receipt-to-Activity bridge for asynchronous hardware runners."""
        if not self._valid_receipt(verifier, command, receipt):
            self._emit(command, "receipt_invalid")
            return ToolExecutionResult("failed", "receipt_invalid")
        if not self._events.emit(self._issuer, command.run_id, command.owner_id, receipt.safe_event_code):
            self._emit(command, "receipt_invalid")
            return ToolExecutionResult("failed", "receipt_invalid")
        return ToolExecutionResult("completed" if receipt.observed_state == "completed" else "failed", "verified_device_receipt")

    def approval_for_owner_run(self, owner_id: str, run_id: str):
        return self._approvals.status_for_owner_run(owner_id, run_id)

    def _execute_adapter(self, command: ToolCommand) -> ToolExecutionResult:
        adapter = self._adapters.get(command.tool_id)
        if not adapter:
            self._emit(command, "runner_unavailable")
            return ToolExecutionResult("failed", "runner_unavailable")
        try:
            receipt = adapter.execute(command)
        except ToolAdapterUnavailable:
            self._emit(command, "runner_unavailable")
            return ToolExecutionResult("failed", "runner_unavailable")
        except Exception:
            self._emit(command, "execution_failed")
            return ToolExecutionResult("failed", "execution_failed")
        if not self._valid_receipt(adapter, command, receipt):
            self._emit(command, "receipt_invalid")
            return ToolExecutionResult("failed", "receipt_invalid")
        if not self._events.emit(self._issuer, command.run_id, command.owner_id, receipt.safe_event_code):
            self._emit(command, "receipt_invalid")
            return ToolExecutionResult("failed", "receipt_invalid")
        self._emit(command, "completed")
        return ToolExecutionResult("completed", "completed")

    def _valid_receipt(self, adapter: ToolAdapter, command: ToolCommand, receipt: ToolReceipt) -> bool:
        if not isinstance(receipt, ToolReceipt):
            return False
        if (
            receipt.command_id != command.command_id
            or receipt.run_id != command.run_id
            or receipt.owner_id != command.owner_id
            or receipt.action_digest != command.action_digest
            or receipt.observed_state not in ("completed", "failed")
            or not receipt.receipt_signature
        ):
            return False
        try:
            return bool(adapter.verify_receipt(command, receipt))
        except Exception:
            return False

    def _emit(self, command: ToolCommand, fact_code: str) -> None:
        self._events.emit(self._issuer, command.run_id, command.owner_id, fact_code)
