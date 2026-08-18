"""One-time, short-lived, owner/run/digest-bound approval tickets."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Dict, Optional

from tool_contracts import ApprovalDecision, ApprovalTicket, ToolCommand


class ApprovalService:
    def __init__(self, clock: Callable[[], float] = time.time, ticket_ttl_seconds: int = 120):
        self._clock = clock
        self._ticket_ttl_seconds = max(1, min(int(ticket_ttl_seconds), 300))
        self._tickets: Dict[str, ApprovalTicket] = {}
        self._lock = threading.RLock()

    def mint(self, command: ToolCommand) -> ApprovalTicket:
        now = self._clock()
        ticket = ApprovalTicket(
            approval_id=str(uuid.uuid4()),
            owner_id=command.owner_id,
            run_id=command.run_id,
            tool_id=command.tool_id,
            command_name=command.command_name,
            action_digest=command.action_digest,
            expires_at=min(command.expires_at, now + self._ticket_ttl_seconds),
        )
        with self._lock:
            self._tickets[ticket.approval_id] = ticket
        return ticket

    def status_for_owner_run(self, owner_id: str, run_id: str) -> Optional[ApprovalTicket]:
        now = self._clock()
        with self._lock:
            for ticket in self._tickets.values():
                if ticket.owner_id == owner_id and ticket.run_id == run_id and ticket.used_at is None and ticket.expires_at > now:
                    return ticket
        return None

    def consume(self, approval_id: str, command: ToolCommand) -> ApprovalDecision:
        """Atomically consume a single ticket only when every immutable binding matches."""

        now = self._clock()
        with self._lock:
            ticket = self._tickets.get(str(approval_id or ""))
            if not ticket:
                return ApprovalDecision(False, "approval_not_valid")
            if ticket.expires_at <= now:
                return ApprovalDecision(False, "approval_expired")
            if ticket.used_at is not None:
                return ApprovalDecision(False, "approval_not_valid")
            if (
                ticket.owner_id != command.owner_id
                or ticket.run_id != command.run_id
                or ticket.tool_id != command.tool_id
                or ticket.command_name != command.command_name
                or ticket.action_digest != command.action_digest
            ):
                return ApprovalDecision(False, "approval_not_valid")
            self._tickets[approval_id] = ApprovalTicket(
                approval_id=ticket.approval_id,
                owner_id=ticket.owner_id,
                run_id=ticket.run_id,
                tool_id=ticket.tool_id,
                command_name=ticket.command_name,
                action_digest=ticket.action_digest,
                expires_at=ticket.expires_at,
                used_at=now,
            )
            return ApprovalDecision(True, "approved")
