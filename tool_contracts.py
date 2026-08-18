"""Immutable, provider-neutral contracts for Hermes server-managed tools.

This module deliberately carries only opaque identifiers and allowlisted command
metadata. It does not contain browser state, URLs, selectors, scripts,
credentials, cookies, local paths, or provider SDK clients.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple


class RiskClass(str, Enum):
    ROUTINE = "routine"
    CONSEQUENTIAL = "consequential"


class ToolAvailability(str, Enum):
    AVAILABLE = "available"
    PENDING_EXTERNAL_RUNNER_AVAILABILITY = "pending_external_runner_availability"


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    display_name: str
    enabled: bool
    availability: ToolAvailability
    commands: Mapping[str, RiskClass]
    allowed_site_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ToolCommand:
    """A server-created, owner-bound command envelope for a future adapter."""

    command_id: str
    run_id: str
    owner_id: str
    tool_id: str
    command_name: str
    site_id: str
    permitted_artifact_reference: Optional[str]
    action_digest: str
    expires_at: float


@dataclass(frozen=True)
class ToolReceipt:
    """A future adapter's factual receipt, with no raw page or browser data."""

    command_id: str
    run_id: str
    owner_id: str
    observed_state: str
    safe_event_code: str
    action_digest: str
    receipt_signature: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str
    approval_required: bool = False


@dataclass(frozen=True)
class ApprovalTicket:
    approval_id: str
    owner_id: str
    run_id: str
    tool_id: str
    command_name: str
    action_digest: str
    expires_at: float
    used_at: Optional[float] = None


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    code: str


@dataclass(frozen=True)
class SafeToolEvent:
    state: str
    kind: str
    label: str


@dataclass(frozen=True)
class ToolExecutionResult:
    status: str
    code: str
    approval_id: Optional[str] = None


def build_action_digest(
    owner_id: str,
    run_id: str,
    tool_id: str,
    command_name: str,
    site_id: str,
    permitted_artifact_reference: Optional[str],
) -> str:
    """Create an immutable digest over the constrained command intent only."""

    intent = {
        "owner_id": str(owner_id),
        "run_id": str(run_id),
        "tool_id": str(tool_id),
        "command_name": str(command_name),
        "site_id": str(site_id),
        "permitted_artifact_reference": str(permitted_artifact_reference or ""),
    }
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
