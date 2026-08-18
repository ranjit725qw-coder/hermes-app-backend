"""Owner-bound, default-deny authorization for generic Tool Architecture commands."""

from __future__ import annotations

from typing import Dict, Iterable, Set

from tool_contracts import PolicyDecision, RiskClass, ToolAvailability, ToolCommand
from tool_registry import ToolRegistry


class ToolPermissionPolicy:
    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        self._owner_tool_allowlist: Dict[str, Set[str]] = {}

    def allow_owner_tool(self, owner_id: str, tool_id: str) -> None:
        """Explicitly allow one owner/tool pairing; all others remain denied."""

        owner = str(owner_id or "")
        tool = str(tool_id or "")
        if not owner or not tool:
            raise ValueError("Owner and tool identifiers are required.")
        self._owner_tool_allowlist.setdefault(owner, set()).add(tool)

    def revoke_owner_tool(self, owner_id: str, tool_id: str) -> None:
        self._owner_tool_allowlist.get(str(owner_id or ""), set()).discard(str(tool_id or ""))

    def evaluate(self, command: ToolCommand) -> PolicyDecision:
        if not command.owner_id or not command.run_id:
            return PolicyDecision(False, "invalid_command")
        descriptor = self._registry.get(command.tool_id)
        if not descriptor:
            return PolicyDecision(False, "tool_unknown")
        if descriptor.availability != ToolAvailability.AVAILABLE:
            return PolicyDecision(False, "tool_unavailable")
        if not descriptor.enabled:
            return PolicyDecision(False, "tool_disabled")
        if command.tool_id not in self._owner_tool_allowlist.get(command.owner_id, set()):
            return PolicyDecision(False, "owner_not_allowed")
        risk = descriptor.commands.get(command.command_name)
        if risk is None:
            return PolicyDecision(False, "command_not_allowed")
        if command.site_id not in descriptor.allowed_site_ids:
            return PolicyDecision(False, "site_not_allowed")
        return PolicyDecision(True, "allowed", approval_required=(risk == RiskClass.CONSEQUENTIAL))

    def allowed_tools_for_owner(self, owner_id: str) -> Iterable[str]:
        return tuple(sorted(self._owner_tool_allowlist.get(str(owner_id or ""), set())))
