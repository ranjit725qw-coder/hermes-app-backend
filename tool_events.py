"""Server-only mapper from executor facts to safe Agent Activity events."""

from __future__ import annotations

from typing import Callable, Dict, Optional

from tool_contracts import SafeToolEvent


_SAFE_EVENT_MAP: Dict[str, SafeToolEvent] = {
    "command_accepted": SafeToolEvent("active", "tool_prepare", "Preparing approved tool task"),
    "site_opened": SafeToolEvent("active", "site_open", "Opening approved website"),
    "page_navigated": SafeToolEvent("active", "page_navigation", "Reviewing approved page"),
    "summary_read": SafeToolEvent("active", "summary_review", "Reviewing permitted page information"),
    "content_prepared": SafeToolEvent("active", "content_prepare", "Preparing approved content"),
    "media_staged": SafeToolEvent("active", "media_stage", "Staging approved media"),
    "preview_presented": SafeToolEvent("active", "preview", "Preparing preview"),
    "approval_requested": SafeToolEvent("waiting", "approval", "Waiting for your approval"),
    "approval_received": SafeToolEvent("active", "approval_received", "Approval received"),
    "approved_action_performed": SafeToolEvent("active", "approved_action", "Performing approved action"),
    "completed": SafeToolEvent("completed", "complete", "Task complete"),
    "policy_denied": SafeToolEvent("failed", "failed", "Tool request is not permitted"),
    "approval_invalid": SafeToolEvent("failed", "failed", "Approval could not be verified"),
    "approval_expired": SafeToolEvent("failed", "failed", "Approval expired before the action could start"),
    "runner_unavailable": SafeToolEvent("failed", "failed", "The requested tool is not available"),
    "receipt_invalid": SafeToolEvent("failed", "failed", "Tool execution could not be verified"),
    "execution_failed": SafeToolEvent("failed", "failed", "Tool execution could not be completed"),
    "android_action_verified": SafeToolEvent("completed", "android_action", "Android action completed"),
    "android_action_failed": SafeToolEvent("failed", "failed", "Android action could not be completed"),
}


class VerifiedEventGateway:
    """Issues events only through a private capability held by the executor."""

    def __init__(self, append_event: Callable[[str, str, SafeToolEvent], object]):
        self._append_event = append_event
        self._issuer = object()

    def executor_issuer(self) -> object:
        return self._issuer

    def emit(self, issuer: object, run_id: str, owner_id: str, fact_code: str) -> Optional[SafeToolEvent]:
        if issuer is not self._issuer:
            return None
        event = _SAFE_EVENT_MAP.get(str(fact_code or ""))
        if not event:
            return None
        self._append_event(str(run_id), str(owner_id), event)
        return event


def safe_event_codes() -> tuple:
    return tuple(sorted(_SAFE_EVENT_MAP))
