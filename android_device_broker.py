"""Local-only Phase 1 broker for a paired Android companion identity.

This module deliberately provides no Android command issuance, Accessibility
operation, screen collection, artifact transfer, or Tool Event emission.  It
only verifies owner-scoped registration and a narrowly fixed device-identity
receipt so future action phases cannot bypass the existing Tool Architecture.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from android_device_repository import (
    DeviceEnrollmentRepository,
    DeviceRepositoryError,
    DurableDeviceRecord,
    InMemoryDeviceEnrollmentRepository,
)


PAIRING_TTL_SECONDS = 5 * 60
MAX_IDENTITY_RECEIPT_TTL_SECONDS = 2 * 60
PHASE1_IDENTITY_EVENT_CODE = "android_device_identity_verified"
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 20
PAIRING_CODE_LOOKUP_LENGTH = 4


class AndroidDeviceBrokerError(ValueError):
    """A deliberately non-sensitive validation failure."""


@dataclass(frozen=True)
class PairingChallenge:
    pairing_id: str
    challenge: str
    expires_at: int


@dataclass(frozen=True)
class PairingSession:
    """A human-transferred, short-lived pairing code with no account credential."""

    code: str
    expires_at: int


@dataclass(frozen=True)
class RegisteredDevice:
    device_id: str
    owner_id: str
    label: str
    public_key_der: bytes
    registered_at: int
    revoked_at: Optional[int] = None
    last_sequence: int = 0


def _canonical(fields: Dict[str, object]) -> bytes:
    """Length-prefix fields to avoid ambiguity without forwarding raw UI data."""
    parts = []
    for key in sorted(fields):
        value = str(fields[key])
        parts.append(f"{key}:{len(value)}:{value}")
    return "|".join(parts).encode("utf-8")


def registration_payload(pairing_id, challenge, label, public_key_b64, registration_nonce) -> bytes:
    return _canonical(
        {
            "challenge": challenge,
            "pairing_id": pairing_id,
            "public_key_sha256": hashlib.sha256(public_key_b64.encode("ascii")).hexdigest(),
            "registration_nonce": registration_nonce,
            "schema": "hermes-android-phase1-registration-v1",
            "label": label,
        }
    )


def identity_receipt_payload(device_id, receipt_nonce, sequence, expires_at, safe_event_code) -> bytes:
    return _canonical(
        {
            "device_id": device_id,
            "expires_at": int(expires_at),
            "receipt_nonce": receipt_nonce,
            "safe_event_code": safe_event_code,
            "schema": "hermes-android-phase1-identity-receipt-v1",
            "sequence": int(sequence),
        }
    )


def device_poll_payload(device_id, request_nonce, sequence, expires_at) -> bytes:
    """Canonical authenticated poll content; it carries no UI or action data."""
    return _canonical(
        {
            "device_id": device_id,
            "expires_at": int(expires_at),
            "request_nonce": request_nonce,
            "schema": "hermes-android-phase2-poll-v1",
            "sequence": int(sequence),
        }
    )


def connection_test_payload(device_id, request_nonce, sequence, expires_at) -> bytes:
    """Canonical identity-only reachability probe with no command or UI data."""
    return _canonical(
        {
            "device_id": device_id,
            "expires_at": int(expires_at),
            "request_nonce": request_nonce,
            "schema": "hermes-android-phase2-connection-test-v1",
            "sequence": int(sequence),
        }
    )


def device_recovery_payload(device_id, recovery_nonce, sequence, expires_at) -> bytes:
    """Canonical Keystore proof for an already-enrolled device only."""
    return _canonical(
        {
            "device_id": device_id,
            "expires_at": int(expires_at),
            "recovery_nonce": recovery_nonce,
            "schema": "hermes-android-phase2-device-recovery-v1",
            "sequence": int(sequence),
        }
    )


class AndroidDeviceBroker:
    """Owner-scoped registry with ephemeral pairing and durable public identities."""

    def __init__(self, now: Callable[[], float] = time.time, repository: Optional[DeviceEnrollmentRepository] = None):
        self._now = now
        self._lock = threading.RLock()
        self._pairings: Dict[str, Dict[str, object]] = {}
        self._pairing_code_index: Dict[str, str] = {}
        self._repository = repository or InMemoryDeviceEnrollmentRepository()
        self._used_receipt_nonces = set()

    def issue_pairing_challenge(self, owner_id: str) -> PairingChallenge:
        owner_id = self._required_id(owner_id, "owner")
        now = int(self._now())
        challenge = PairingChallenge(
            pairing_id=str(uuid.uuid4()),
            challenge=base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).decode("ascii").rstrip("="),
            expires_at=now + PAIRING_TTL_SECONDS,
        )
        with self._lock:
            self._pairings[challenge.pairing_id] = {
                "owner_id": owner_id,
                "challenge": challenge.challenge,
                "expires_at": challenge.expires_at,
                "consumed": False,
            }
        return challenge

    def create_pairing_session(self, owner_id: str) -> PairingSession:
        """Create an owner-authenticated, one-time handoff code for the native app.

        Only a code digest and a non-secret lookup prefix are retained locally.
        The code itself is neither written to logs nor returned by later routes.
        """
        challenge = self.issue_pairing_challenge(owner_id)
        code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
        code_digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        with self._lock:
            pairing = self._pairings[challenge.pairing_id]
            pairing["pairing_code_digest"] = code_digest
            pairing["code_claimed"] = False
            self._pairing_code_index[code[:PAIRING_CODE_LOOKUP_LENGTH]] = challenge.pairing_id
        return PairingSession(code=code, expires_at=challenge.expires_at)

    def claim_pairing_session(self, pairing_code: str) -> PairingChallenge:
        """Redeem one displayed code exactly once and expose only its registration challenge."""
        code = self._normalized_pairing_code(pairing_code)
        with self._lock:
            pairing_id = self._pairing_code_index.get(code[:PAIRING_CODE_LOOKUP_LENGTH])
            pairing = self._pairings.get(pairing_id or "")
            if not pairing or pairing.get("consumed") or pairing.get("code_claimed"):
                raise AndroidDeviceBrokerError("pairing_unavailable")
            if int(self._now()) >= int(pairing["expires_at"]):
                raise AndroidDeviceBrokerError("pairing_expired")
            expected = str(pairing.get("pairing_code_digest") or "")
            actual = hashlib.sha256(code.encode("ascii")).hexdigest()
            if not expected or not hmac.compare_digest(expected, actual):
                raise AndroidDeviceBrokerError("pairing_unavailable")
            pairing["code_claimed"] = True
            return PairingChallenge(
                pairing_id=str(pairing_id),
                challenge=str(pairing["challenge"]),
                expires_at=int(pairing["expires_at"]),
            )

    def register_device(
        self,
        owner_id: str,
        pairing_id: str,
        label: str,
        public_key_b64: str,
        registration_nonce: str,
        signature_b64: str,
    ) -> RegisteredDevice:
        owner_id = self._required_id(owner_id, "owner")
        pairing_id = self._required_id(pairing_id, "pairing")
        label = self._safe_label(label)
        registration_nonce = self._required_id(registration_nonce, "nonce")
        public_key_b64 = self._required_id(public_key_b64, "public key")

        with self._lock:
            return self._register_device_locked(
                owner_id, pairing_id, label, public_key_b64, registration_nonce, signature_b64,
                require_claimed_code=False,
            )

    def register_paired_device(
        self,
        pairing_id: str,
        label: str,
        public_key_b64: str,
        registration_nonce: str,
        signature_b64: str,
    ) -> RegisteredDevice:
        """Complete a claimed one-time pairing without transferring web auth to Android."""
        pairing_id = self._required_id(pairing_id, "pairing")
        label = self._safe_label(label)
        registration_nonce = self._required_id(registration_nonce, "nonce")
        public_key_b64 = self._required_id(public_key_b64, "public key")
        with self._lock:
            pairing = self._pairings.get(pairing_id)
            owner_id = str(pairing.get("owner_id") or "") if pairing else ""
            return self._register_device_locked(
                owner_id, pairing_id, label, public_key_b64, registration_nonce, signature_b64,
                require_claimed_code=True,
            )

    def _register_device_locked(
        self,
        owner_id: str,
        pairing_id: str,
        label: str,
        public_key_b64: str,
        registration_nonce: str,
        signature_b64: str,
        require_claimed_code: bool,
    ) -> RegisteredDevice:
        pairing = self._pairings.get(pairing_id)
        if not pairing or pairing["owner_id"] != owner_id or pairing["consumed"]:
            raise AndroidDeviceBrokerError("pairing_unavailable")
        if require_claimed_code and not pairing.get("code_claimed"):
            raise AndroidDeviceBrokerError("pairing_unavailable")
        if int(self._now()) >= int(pairing["expires_at"]):
            raise AndroidDeviceBrokerError("pairing_expired")

        public_key_der = self._decode_public_key(public_key_b64)
        self._verify_signature(
            public_key_der,
            registration_payload(pairing_id, str(pairing["challenge"]), label, public_key_b64, registration_nonce),
            signature_b64,
        )
        pairing["consumed"] = True
        device = RegisteredDevice(
            device_id=str(uuid.uuid4()),
            owner_id=owner_id,
            label=label,
            public_key_der=public_key_der,
            registered_at=int(self._now()),
        )
        try:
            # One owner has exactly one active Android companion. A fresh
            # explicit enrollment revokes its prior device instead of creating
            # an ambiguous command target.
            self._repository.revoke_active_for_owner(owner_id, int(self._now()))
            self._repository.save(self._to_record(device))
        except DeviceRepositoryError as exc:
            raise AndroidDeviceBrokerError("durable_registry_unavailable") from exc
        return device

    def list_devices(self, owner_id: str):
        owner_id = self._required_id(owner_id, "owner")
        with self._lock:
            return [self.public_device_view(device) for device in self._owner_devices(owner_id)]

    def single_active_device_for_owner(self, owner_id: str) -> str:
        """Return exactly one active device for an owner or fail closed on zero/many."""
        owner_id = self._required_id(owner_id, "owner")
        with self._lock:
            active = [device.device_id for device in self._owner_devices(owner_id) if device.revoked_at is None]
            if len(active) != 1:
                raise AndroidDeviceBrokerError("single_active_device_required")
            return active[0]

    def revoke_device(self, owner_id: str, device_id: str) -> bool:
        owner_id = self._required_id(owner_id, "owner")
        device_id = self._required_id(device_id, "device")
        with self._lock:
            device = self._device(device_id)
            if not device or device.owner_id != owner_id:
                return False
            if device.revoked_at is not None:
                return True
            self._save_device(RegisteredDevice(
                device_id=device.device_id,
                owner_id=device.owner_id,
                label=device.label,
                public_key_der=device.public_key_der,
                registered_at=device.registered_at,
                revoked_at=int(self._now()),
                last_sequence=device.last_sequence,
            ))
            return True

    def verify_identity_receipt(
        self,
        device_id: str,
        receipt_nonce: str,
        sequence: int,
        expires_at: int,
        safe_event_code: str,
        signature_b64: str,
    ) -> bool:
        """Accept only a signed no-action identity receipt; never emit Activity."""
        device_id = self._required_id(device_id, "device")
        receipt_nonce = self._required_id(receipt_nonce, "receipt nonce")
        if safe_event_code != PHASE1_IDENTITY_EVENT_CODE:
            raise AndroidDeviceBrokerError("receipt_code_not_permitted")
        if not isinstance(sequence, int) or sequence <= 0:
            raise AndroidDeviceBrokerError("receipt_sequence_invalid")
        if not isinstance(expires_at, int):
            raise AndroidDeviceBrokerError("receipt_expiry_invalid")
        now = int(self._now())
        if expires_at <= now or expires_at > now + MAX_IDENTITY_RECEIPT_TTL_SECONDS:
            raise AndroidDeviceBrokerError("receipt_expired")

        with self._lock:
            device = self._device(device_id)
            if not device or device.revoked_at is not None:
                raise AndroidDeviceBrokerError("device_unavailable")
            nonce_key = f"{device_id}:{receipt_nonce}"
            if nonce_key in self._used_receipt_nonces or sequence <= device.last_sequence:
                raise AndroidDeviceBrokerError("receipt_replayed")
            self._verify_signature(
                device.public_key_der,
                identity_receipt_payload(device_id, receipt_nonce, sequence, expires_at, safe_event_code),
                signature_b64,
            )
            self._advance_sequence(device_id, sequence)
            self._used_receipt_nonces.add(nonce_key)
            return True

    def assert_active_owner(self, owner_id: str, device_id: str) -> None:
        """Fail closed when a device is missing, revoked, or owned by another account."""
        owner_id = self._required_id(owner_id, "owner")
        device_id = self._required_id(device_id, "device")
        with self._lock:
            device = self._device(device_id)
            if not device or device.owner_id != owner_id or device.revoked_at is not None:
                raise AndroidDeviceBrokerError("device_unavailable")

    def active_owner_for_device(self, device_id: str) -> str:
        """Return a device's registered owner only after revocation checks."""
        device_id = self._required_id(device_id, "device")
        with self._lock:
            device = self._device(device_id)
            if not device or device.revoked_at is not None:
                raise AndroidDeviceBrokerError("device_unavailable")
            return device.owner_id

    def verify_device_payload(self, device_id: str, receipt_nonce: str, sequence: int, payload: bytes, signature_b64: str) -> None:
        """Verify a bounded Phase 2 receipt and advance the per-device sequence."""
        device_id = self._required_id(device_id, "device")
        receipt_nonce = self._required_id(receipt_nonce, "receipt nonce")
        if not isinstance(sequence, int) or sequence <= 0 or not isinstance(payload, bytes):
            raise AndroidDeviceBrokerError("receipt_invalid")
        with self._lock:
            device = self._device(device_id)
            if not device or device.revoked_at is not None:
                raise AndroidDeviceBrokerError("device_unavailable")
            nonce_key = f"{device_id}:{receipt_nonce}"
            if nonce_key in self._used_receipt_nonces or sequence <= device.last_sequence:
                raise AndroidDeviceBrokerError("receipt_replayed")
            self._verify_signature(device.public_key_der, payload, signature_b64)
            self._advance_sequence(device_id, sequence)
            self._used_receipt_nonces.add(nonce_key)

    def verify_device_poll(
        self,
        device_id: str,
        request_nonce: str,
        sequence: int,
        expires_at: int,
        signature_b64: str,
    ) -> None:
        """Verify a short-lived poll against the registered device identity.

        Ownership is derived from the verified device record rather than an
        owner string supplied by the phone.
        """
        device_id = self._required_id(device_id, "device")
        request_nonce = self._required_id(request_nonce, "request nonce")
        now = int(self._now())
        if not isinstance(expires_at, int) or expires_at <= now or expires_at > now + MAX_IDENTITY_RECEIPT_TTL_SECONDS:
            raise AndroidDeviceBrokerError("request_expired")
        self.active_owner_for_device(device_id)
        self.verify_device_payload(
            device_id,
            request_nonce,
            sequence,
            device_poll_payload(device_id, request_nonce, sequence, expires_at),
            signature_b64,
        )

    def verify_connection_test(
        self,
        device_id: str,
        request_nonce: str,
        sequence: int,
        expires_at: int,
        signature_b64: str,
    ) -> str:
        """Verify only paired-device reachability; never poll or emit an event."""
        device_id = self._required_id(device_id, "device")
        request_nonce = self._required_id(request_nonce, "request nonce")
        now = int(self._now())
        if not isinstance(expires_at, int) or expires_at <= now or expires_at > now + MAX_IDENTITY_RECEIPT_TTL_SECONDS:
            raise AndroidDeviceBrokerError("request_expired")
        self.active_owner_for_device(device_id)
        self.verify_device_payload(
            device_id,
            request_nonce,
            sequence,
            connection_test_payload(device_id, request_nonce, sequence, expires_at),
            signature_b64,
        )
        return "paired_companion_reachable"

    def recover_device(self, device_id: str, recovery_nonce: str, sequence: int, expires_at: int, signature_b64: str) -> RegisteredDevice:
        """Accept a new liveness proof only from the already registered key.

        Recovery never creates an enrollment, changes owner binding, or restores
        a revoked record. Thus missing Android app state cannot silently pair.
        """
        device_id = self._required_id(device_id, "device")
        recovery_nonce = self._required_id(recovery_nonce, "recovery nonce")
        now = int(self._now())
        if not isinstance(sequence, int) or sequence <= 0 or not isinstance(expires_at, int) or expires_at <= now or expires_at > now + MAX_IDENTITY_RECEIPT_TTL_SECONDS:
            raise AndroidDeviceBrokerError("request_expired")
        with self._lock:
            device = self._device(device_id)
            if not device or device.revoked_at is not None:
                raise AndroidDeviceBrokerError("device_unavailable")
            nonce_key = f"{device_id}:{recovery_nonce}"
            if nonce_key in self._used_receipt_nonces or sequence <= device.last_sequence:
                raise AndroidDeviceBrokerError("receipt_replayed")
            self._verify_signature(
                device.public_key_der,
                device_recovery_payload(device_id, recovery_nonce, sequence, expires_at),
                signature_b64,
            )
            self._advance_sequence(device_id, sequence)
            self._used_receipt_nonces.add(nonce_key)
            return self._device(device_id) or device

    @staticmethod
    def public_device_view(device: RegisteredDevice) -> Dict[str, object]:
        return {
            "device_id": device.device_id,
            "label": device.label,
            "registered_at": device.registered_at,
            "status": "revoked" if device.revoked_at is not None else "active",
        }

    def _owner_devices(self, owner_id: str):
        try:
            return [self._from_record(record) for record in self._repository.list_owner(owner_id)]
        except DeviceRepositoryError as exc:
            raise AndroidDeviceBrokerError("durable_registry_unavailable") from exc

    def _device(self, device_id: str) -> Optional[RegisteredDevice]:
        try:
            record = self._repository.get(device_id)
        except DeviceRepositoryError as exc:
            raise AndroidDeviceBrokerError("durable_registry_unavailable") from exc
        return self._from_record(record) if record else None

    def _save_device(self, device: RegisteredDevice) -> None:
        try:
            self._repository.save(self._to_record(device))
        except DeviceRepositoryError as exc:
            raise AndroidDeviceBrokerError("durable_registry_unavailable") from exc

    def _advance_sequence(self, device_id: str, sequence: int) -> RegisteredDevice:
        try:
            return self._from_record(self._repository.advance_sequence(device_id, sequence))
        except DeviceRepositoryError as exc:
            raise AndroidDeviceBrokerError("receipt_replayed") from exc

    @staticmethod
    def _from_record(record: DurableDeviceRecord) -> RegisteredDevice:
        return RegisteredDevice(
            device_id=record.device_id,
            owner_id=record.owner_id,
            label=record.label,
            public_key_der=record.public_key_der,
            registered_at=record.registered_at,
            revoked_at=record.revoked_at,
            last_sequence=record.last_sequence,
        )

    @staticmethod
    def _to_record(device: RegisteredDevice) -> DurableDeviceRecord:
        return DurableDeviceRecord(
            device_id=device.device_id,
            owner_id=device.owner_id,
            label=device.label,
            public_key_der=device.public_key_der,
            registered_at=device.registered_at,
            revoked_at=device.revoked_at,
            last_sequence=device.last_sequence,
        )

    @staticmethod
    def _required_id(value: object, _field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise AndroidDeviceBrokerError("invalid_request")
        return normalized

    @staticmethod
    def _safe_label(value: object) -> str:
        normalized = " ".join(str(value or "").split())
        if not normalized or len(normalized) > 64:
            raise AndroidDeviceBrokerError("invalid_request")
        if any(ord(character) < 0x20 for character in normalized):
            raise AndroidDeviceBrokerError("invalid_request")
        return normalized

    @staticmethod
    def _normalized_pairing_code(value: object) -> str:
        normalized = "".join(character for character in str(value or "").upper() if character not in " -")
        if len(normalized) != PAIRING_CODE_LENGTH or any(character not in PAIRING_CODE_ALPHABET for character in normalized):
            raise AndroidDeviceBrokerError("invalid_request")
        return normalized

    @staticmethod
    def _decode_public_key(public_key_b64: str) -> bytes:
        try:
            key_der = base64.b64decode(public_key_b64.encode("ascii"), validate=True)
            key = serialization.load_der_public_key(key_der)
        except (ValueError, TypeError, UnicodeEncodeError):
            raise AndroidDeviceBrokerError("invalid_public_key")
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise AndroidDeviceBrokerError("invalid_public_key")
        return key_der

    @staticmethod
    def _verify_signature(public_key_der: bytes, payload: bytes, signature_b64: str) -> None:
        try:
            signature = base64.b64decode(str(signature_b64 or "").encode("ascii"), validate=True)
            key = serialization.load_der_public_key(public_key_der)
            key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError, TypeError, UnicodeEncodeError):
            raise AndroidDeviceBrokerError("signature_invalid")
