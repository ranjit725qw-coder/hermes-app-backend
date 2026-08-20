"""Default-deny registry for constrained, server-managed tool descriptors."""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

from tool_contracts import RiskClass, ToolAvailability, ToolDescriptor


class ToolRegistry:
    def __init__(self, descriptors: Iterable[ToolDescriptor] = ()):
        self._descriptors: Dict[str, ToolDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ToolDescriptor) -> None:
        if not isinstance(descriptor, ToolDescriptor) or not descriptor.tool_id:
            raise ValueError("A valid immutable tool descriptor is required.")
        if descriptor.tool_id in self._descriptors:
            raise ValueError("Duplicate tool descriptor.")
        self._descriptors[descriptor.tool_id] = descriptor

    def get(self, tool_id: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(str(tool_id or ""))

    def safe_catalog(self) -> Tuple[dict, ...]:
        """Return public metadata only; never expose policies, credentials, or transport."""

        return tuple(
            {
                "tool_id": descriptor.tool_id,
                "display_name": descriptor.display_name,
                "enabled": descriptor.enabled,
                "availability": descriptor.availability.value,
                "commands": tuple(sorted(descriptor.commands)),
                "site_ids": tuple(sorted(descriptor.allowed_site_ids)),
            }
            for descriptor in sorted(self._descriptors.values(), key=lambda item: item.tool_id)
        )


def default_tool_registry() -> ToolRegistry:
    """Register the future browser boundary as unavailable, not as a mock runner."""

    browser_commands = {
        "open_approved_site": RiskClass.ROUTINE,
        "navigate_permitted_page": RiskClass.ROUTINE,
        "read_permitted_summary": RiskClass.ROUTINE,
        "prepare_content": RiskClass.ROUTINE,
        "stage_media": RiskClass.ROUTINE,
        "present_preview": RiskClass.ROUTINE,
        "request_approval": RiskClass.ROUTINE,
        "perform_approved_action": RiskClass.CONSEQUENTIAL,
    }
    android_commands = {
        # Launching even an approved mobile app is consequential: every request
        # must pass through a fresh server-side approval ticket.
        "OPEN_APP": RiskClass.CONSEQUENTIAL,
        "OBSERVE": RiskClass.ROUTINE,
        "TAP": RiskClass.ROUTINE,
        "SCROLL": RiskClass.ROUTINE,
        "BACK": RiskClass.ROUTINE,
        "TYPE": RiskClass.CONSEQUENTIAL,
    }
    return ToolRegistry(
        (
            ToolDescriptor(
                tool_id="browser_runner",
                display_name="Browser Runner",
                enabled=False,
                availability=ToolAvailability.PENDING_EXTERNAL_RUNNER_AVAILABILITY,
                commands=browser_commands,
                allowed_site_ids=(),
            ),
            ToolDescriptor(
                tool_id="android_companion",
                display_name="Android Companion",
                enabled=True,
                availability=ToolAvailability.AVAILABLE,
                commands=android_commands,
                allowed_site_ids=(),
            ),
        )
    )
