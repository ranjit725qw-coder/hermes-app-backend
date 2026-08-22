"""Phase 2 local Android command broker.

This module deliberately has an empty production policy catalogue. Commands are
default-denied unless an approved, certificate-bound profile is supplied by a
future reviewed configuration path. It stores no screenshots, UI trees,
credentials, OTPs, cookies, or provider keys.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional

from android_device_broker import AndroidDeviceBroker, AndroidDeviceBrokerError, _canonical
from tool_contracts import ToolCommand, ToolReceipt


ANDROID_TOOL_ID = "android_companion"
ANDROID_SAFE_EVENT_SUCCESS = "android_action_verified"
ANDROID_SAFE_EVENT_FAILURE = "android_action_failed"
ACTION_EXECUTION_VERIFIED = "action_verified"
ACTION_EXECUTION_FAILED = "action_failed"
LAUNCH_EXECUTION_VERIFIED = "launch_verified"
LAUNCH_EXECUTION_POLICY_DENIED = "launch_policy_denied"
LAUNCH_EXECUTION_INTENT_UNAVAILABLE = "launch_intent_unavailable"
LAUNCH_EXECUTION_START_REJECTED = "launch_start_rejected"
LAUNCH_EXECUTION_FOREGROUND_TIMEOUT = "launch_foreground_timeout"
LAUNCH_EXECUTION_INTERRUPTED = "launch_interrupted"
LAUNCH_EXECUTION_SERVICE_UNAVAILABLE = "launch_service_unavailable"
LAUNCH_FAILURE_CATEGORIES = frozenset({
    LAUNCH_EXECUTION_POLICY_DENIED,
    LAUNCH_EXECUTION_INTENT_UNAVAILABLE,
    LAUNCH_EXECUTION_START_REJECTED,
    LAUNCH_EXECUTION_FOREGROUND_TIMEOUT,
    LAUNCH_EXECUTION_INTERRUPTED,
    LAUNCH_EXECUTION_SERVICE_UNAVAILABLE,
})
MAX_COMMAND_TTL_SECONDS = 120
MAX_TEXT_LENGTH = 256
ALLOWED_ACTIONS = frozenset({"OPEN_APP", "OBSERVE", "TAP", "SCROLL", "BACK", "TYPE"})
CONSEQUENTIAL_ACTIONS = frozenset({"TYPE"})


class AndroidAutomationBrokerError(ValueError):
    """A non-sensitive, fail-closed device command or receipt validation error."""


@dataclass(frozen=True)
class AndroidPackageProfile:
    package_id: str
    certificate_sha256: str
    allowed_actions: frozenset
    selector_ids: frozenset


@dataclass(frozen=True)
class PendingAndroidCommand:
    command: ToolCommand
    device_id: str
    package_id: str
    certificate_sha256: str
    action: str
    selector_id: str
    text: Optional[str]
    command_nonce: str
    expires_at: int
    cancelled: bool = False
    delivered: bool = False


def android_command_payload(command: PendingAndroidCommand) -> bytes:
    """Canonical command content signed by the registered Android device on receipt."""
    return _canonical(
        {
            "action": command.action,
            "action_digest": command.command.action_digest,
            "certificate_sha256": command.certificate_sha256,
            "command_id": command.command.command_id,
            "command_nonce": command.command_nonce,
            "device_id": command.device_id,
            "expires_at": command.expires_at,
            "owner_id": command.command.owner_id,
            "package_id": command.package_id,
            "run_id": command.command.run_id,
            "schema": "hermes-android-phase2-command-v1",
            "selector_id": command.selector_id,
            "text_sha256": hashlib.sha256((command.text or "").encode("utf-8")).hexdigest(),
        }
    )


def android_receipt_payload(
    command: PendingAndroidCommand,
    receipt_nonce: str,
    sequence: int,
    expires_at: int,
    outcome: str,
    execution_category: str,
) -> bytes:
    return _canonical(
        {
            "command_sha256": hashlib.sha256(android_command_payload(command)).hexdigest(),
            "execution_category": execution_category,
            "expires_at": int(expires_at),
            "outcome": outcome,
            "receipt_nonce": receipt_nonce,
            "schema": "hermes-android-phase2-receipt-v2",
            "sequence": int(sequence),
        }
    )


class AndroidAutomationBroker:
    """Owner/device/profile-bound local queue with no generic remote-control API."""

    def __init__(self, devices: AndroidDeviceBroker, profiles: Iterable[AndroidPackageProfile] = (), now: Callable[[], float] = time.time):
        self._devices = devices
        self._now = now
        self._profiles = {profile.package_id: profile for profile in profiles}
        self._lock = threading.RLock()
        self._pending: Dict[str, PendingAndroidCommand] = {}
        self._used_receipts = set()
        self._verified_receipts = set()

    def request_command(
        self,
        owner_id: str,
        device_id: str,
        command: ToolCommand,
        package_id: str,
        certificate_sha256: str,
        action: str,
        selector_id: str,
        text: Optional[str] = None,
    ) -> PendingAndroidCommand:
        action = str(action or "").upper()
        package_id = self._required(package_id, "package")
        device_id = self._required(device_id, "device")
        selector_id = self._required(selector_id, "selector")
        if action not in ALLOWED_ACTIONS:
            raise AndroidAutomationBrokerError("action_not_allowed")
        profile = self._profiles.get(package_id)
        if not profile or certificate_sha256 != profile.certificate_sha256:
            raise AndroidAutomationBrokerError("package_not_allowed")
        if action not in profile.allowed_actions or selector_id not in profile.selector_ids:
            raise AndroidAutomationBrokerError("action_not_allowed")
        if command.owner_id != owner_id or command.site_id != package_id:
            raise AndroidAutomationBrokerError("command_not_bound")
        self._devices.assert_active_owner(owner_id, device_id)
        safe_text = self._safe_text(action, text)
        now = int(self._now())
        expires_at = min(int(command.expires_at), now + MAX_COMMAND_TTL_SECONDS)
        if expires_at <= now:
            raise AndroidAutomationBrokerError("command_expired")
        pending = PendingAndroidCommand(
            command=command,
            device_id=device_id,
            package_id=package_id,
            certificate_sha256=certificate_sha256,
            action=action,
            selector_id=selector_id,
            text=safe_text,
            command_nonce=str(uuid.uuid4()),
            expires_at=expires_at,
        )
        with self._lock:
            self._pending[command.command_id] = pending
        return pending

    def next_for_device(self, device_id: str) -> Optional[dict]:
        """Return exactly one bound command, if active and not expired/cancelled."""
        device_id = self._required(device_id, "device")
        now = int(self._now())
        with self._lock:
            for command_id, pending in list(self._pending.items()):
                if pending.device_id != device_id:
                    continue
                if pending.cancelled or pending.expires_at <= now:
                    self._pending.pop(command_id, None)
                    continue
                try:
                    self._devices.assert_active_owner(pending.command.owner_id, device_id)
                except AndroidDeviceBrokerError:
                    self._pending.pop(command_id, None)
                    continue
                if pending.delivered:
                    continue
                self._pending[command_id] = PendingAndroidCommand(**{**pending.__dict__, "delivered": True})
                return {
                    "command_id": pending.command.command_id,
                    "run_id": pending.command.run_id,
                    "owner_id": pending.command.owner_id,
                    "action_digest": pending.command.action_digest,
                    "package_id": pending.package_id,
                    "certificate_sha256": pending.certificate_sha256,
                    "action": pending.action,
                    "selector_id": pending.selector_id,
                    "text": pending.text,
                    "command_nonce": pending.command_nonce,
                    "expires_at": pending.expires_at,
                }
        return None

    def profile_for_package(self, package_id: str) -> Optional[AndroidPackageProfile]:
        """Expose a copy-free, read-only policy lookup for server-side command validation."""
        return self._profiles.get(str(package_id or ""))

    def cancel(self, owner_id: str, device_id: str, command_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(str(command_id or ""))
            if not pending or pending.device_id != device_id or pending.command.owner_id != owner_id:
                return False
            self._pending[pending.command.command_id] = PendingAndroidCommand(**{**pending.__dict__, "cancelled": True})
            return True

    def verify_device_receipt(
        self,
        owner_id: str,
        device_id: str,
        command_id: str,
        receipt_nonce: str,
        sequence: int,
        expires_at: int,
        outcome: str,
        execution_category: str,
        signature: str,
    ) -> ToolReceipt:
        outcome = str(outcome or "")
        if outcome not in (ANDROID_SAFE_EVENT_SUCCESS, ANDROID_SAFE_EVENT_FAILURE):
            raise AndroidAutomationBrokerError("receipt_outcome_invalid")
        now = int(self._now())
        if not isinstance(sequence, int) or sequence <= 0 or not isinstance(expires_at, int) or expires_at <= now or expires_at > now + MAX_COMMAND_TTL_SECONDS:
            raise AndroidAutomationBrokerError("receipt_expired")
        with self._lock:
            pending = self._pending.get(str(command_id or ""))
            if not pending or pending.device_id != device_id or pending.command.owner_id != owner_id or pending.cancelled or pending.expires_at <= now:
                raise AndroidAutomationBrokerError("command_unavailable")
            if not self._execution_category_allowed(pending.action, outcome, execution_category):
                raise AndroidAutomationBrokerError("receipt_execution_category_invalid")
            receipt_key = f"{device_id}:{receipt_nonce}"
            if receipt_key in self._used_receipts:
                raise AndroidAutomationBrokerError("receipt_replayed")
            try:
                self._devices.verify_device_payload(
                    device_id,
                    receipt_nonce,
                    sequence,
                    android_receipt_payload(pending, receipt_nonce, sequence, expires_at, outcome, execution_category),
                    signature,
                )
            except AndroidDeviceBrokerError as exc:
                raise AndroidAutomationBrokerError("receipt_invalid") from exc
            self._used_receipts.add(receipt_key)
            self._pending.pop(pending.command.command_id, None)
        observed_state = "completed" if outcome == ANDROID_SAFE_EVENT_SUCCESS else "failed"
        receipt = ToolReceipt(
            command_id=pending.command.command_id,
            run_id=pending.command.run_id,
            owner_id=owner_id,
            observed_state=observed_state,
            safe_event_code=outcome,
            action_digest=pending.command.action_digest,
            receipt_signature=signature,
        )
        with self._lock:
            self._verified_receipts.add((receipt.command_id, receipt.action_digest, receipt.receipt_signature))
        return receipt

    def verify_receipt(self, command: ToolCommand, receipt: ToolReceipt) -> bool:
        """Accept only a receipt previously verified against its registered device key."""
        marker = (receipt.command_id, receipt.action_digest, receipt.receipt_signature)
        with self._lock:
            return marker in self._verified_receipts and receipt.command_id == command.command_id

    @staticmethod
    def _execution_category_allowed(action: str, outcome: str, execution_category: object) -> bool:
        category = str(execution_category or "")
        if action == "OPEN_APP":
            return (
                outcome == ANDROID_SAFE_EVENT_SUCCESS and category == LAUNCH_EXECUTION_VERIFIED
            ) or (
                outcome == ANDROID_SAFE_EVENT_FAILURE and category in LAUNCH_FAILURE_CATEGORIES
            )
        return (
            outcome == ANDROID_SAFE_EVENT_SUCCESS and category == ACTION_EXECUTION_VERIFIED
        ) or (
            outcome == ANDROID_SAFE_EVENT_FAILURE and category == ACTION_EXECUTION_FAILED
        )

    @staticmethod
    def _required(value: object, _field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise AndroidAutomationBrokerError("invalid_request")
        return normalized

    @staticmethod
    def _safe_text(action: str, text: Optional[str]) -> Optional[str]:
        if action != "TYPE":
            if text not in (None, ""):
                raise AndroidAutomationBrokerError("payload_not_allowed")
            return None
        normalized = str(text or "")
        lowered = normalized.lower()
        if not normalized or len(normalized) > MAX_TEXT_LENGTH or any(marker in lowered for marker in ("password", "otp", "one-time", "verification code")):
            raise AndroidAutomationBrokerError("sensitive_text_blocked")
        return normalized
