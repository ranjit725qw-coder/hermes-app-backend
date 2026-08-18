"""Provider boundary only; no browser, transport, profile, or credential implementation."""

from __future__ import annotations

from typing import Protocol

from tool_contracts import ToolCommand, ToolReceipt


PENDING_EXTERNAL_RUNNER_AVAILABILITY = "pending_external_runner_availability"


class ToolAdapterUnavailable(RuntimeError):
    pass


class ToolAdapter(Protocol):
    def execute(self, command: ToolCommand) -> ToolReceipt:
        ...

    def verify_receipt(self, command: ToolCommand, receipt: ToolReceipt) -> bool:
        ...


class BrowserRunnerPendingAdapter:
    """Intentional boundary: it performs no mock browser activity or connection."""

    status = PENDING_EXTERNAL_RUNNER_AVAILABILITY

    def execute(self, command: ToolCommand) -> ToolReceipt:
        raise ToolAdapterUnavailable(PENDING_EXTERNAL_RUNNER_AVAILABILITY)

    def verify_receipt(self, command: ToolCommand, receipt: ToolReceipt) -> bool:
        return False
