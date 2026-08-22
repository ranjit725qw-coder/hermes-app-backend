"""Durable, backend-only storage for cryptographically enrolled Android devices.

This module persists only public verification material and lifecycle metadata.
It never stores Android private keys, pairing handoff values, web credentials,
chat content, command payloads, YouTube certificate values, or action receipts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Dict, Iterable, Optional
from urllib.parse import quote

import requests


class DeviceRepositoryError(RuntimeError):
    """Fail closed when durable enrollment state cannot be verified."""


@dataclass(frozen=True)
class DurableDeviceRecord:
    device_id: str
    owner_id: str
    label: str
    public_key_der: bytes
    registered_at: int
    revoked_at: Optional[int] = None
    last_sequence: int = 0


class DeviceEnrollmentRepository:
    """Minimal durable registry contract used by the command broker."""

    def get(self, device_id: str) -> Optional[DurableDeviceRecord]:
        raise NotImplementedError

    def list_owner(self, owner_id: str) -> Iterable[DurableDeviceRecord]:
        raise NotImplementedError

    def save(self, device: DurableDeviceRecord) -> None:
        raise NotImplementedError

    def revoke_active_for_owner(self, owner_id: str, revoked_at: int) -> None:
        raise NotImplementedError

    def advance_sequence(self, device_id: str, sequence: int) -> DurableDeviceRecord:
        raise NotImplementedError


class InMemoryDeviceEnrollmentRepository(DeviceEnrollmentRepository):
    """Deterministic local/test repository; share one instance to model restarts."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: Dict[str, DurableDeviceRecord] = {}

    def get(self, device_id: str) -> Optional[DurableDeviceRecord]:
        with self._lock:
            return self._records.get(device_id)

    def list_owner(self, owner_id: str) -> Iterable[DurableDeviceRecord]:
        with self._lock:
            return [record for record in self._records.values() if record.owner_id == owner_id]

    def save(self, device: DurableDeviceRecord) -> None:
        with self._lock:
            self._records[device.device_id] = device

    def revoke_active_for_owner(self, owner_id: str, revoked_at: int) -> None:
        with self._lock:
            for device_id, record in list(self._records.items()):
                if record.owner_id == owner_id and record.revoked_at is None:
                    self._records[device_id] = replace(record, revoked_at=revoked_at)

    def advance_sequence(self, device_id: str, sequence: int) -> DurableDeviceRecord:
        with self._lock:
            record = self._records.get(device_id)
            if not record or record.revoked_at is not None or sequence <= record.last_sequence:
                raise DeviceRepositoryError("sequence_unavailable")
            updated = replace(record, last_sequence=sequence)
            self._records[device_id] = updated
            return updated


class SupabaseDeviceEnrollmentRepository(DeviceEnrollmentRepository):
    """Backend-service-role repository for the isolated enrollment table only."""

    TABLE = "android_companion_devices"
    MAX_TIMEOUT_SECONDS = 8

    def __init__(self, supabase_url: str, service_role_key: str):
        if not supabase_url or not service_role_key:
            raise DeviceRepositoryError("durable_registry_unconfigured")
        self._base_url = supabase_url.rstrip("/") + "/rest/v1/" + self.TABLE
        self._headers = {
            "apikey": service_role_key,
            "Authorization": "Bearer " + service_role_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get(self, device_id: str) -> Optional[DurableDeviceRecord]:
        rows = self._request("GET", "?select=*&device_id=eq." + quote(device_id, safe=""))
        if not rows:
            return None
        if not isinstance(rows, list) or len(rows) != 1:
            raise DeviceRepositoryError("durable_registry_unavailable")
        return self._from_row(rows[0])

    def list_owner(self, owner_id: str) -> Iterable[DurableDeviceRecord]:
        rows = self._request("GET", "?select=*&owner_id=eq." + quote(owner_id, safe=""))
        if not isinstance(rows, list):
            raise DeviceRepositoryError("durable_registry_unavailable")
        return [self._from_row(row) for row in rows]

    def save(self, device: DurableDeviceRecord) -> None:
        payload = self._to_row(device)
        headers = {**self._headers, "Prefer": "resolution=merge-duplicates,return=minimal"}
        self._request("POST", "?on_conflict=device_id", payload, headers=headers)

    def revoke_active_for_owner(self, owner_id: str, revoked_at: int) -> None:
        headers = {**self._headers, "Prefer": "return=minimal"}
        self._request(
            "PATCH",
            "?owner_id=eq." + quote(owner_id, safe="") + "&revoked_at=is.null",
            {"revoked_at": int(revoked_at)},
            headers=headers,
        )

    def advance_sequence(self, device_id: str, sequence: int) -> DurableDeviceRecord:
        headers = {**self._headers, "Prefer": "return=representation"}
        rows = self._request(
            "PATCH",
            "?device_id=eq." + quote(device_id, safe="") + "&revoked_at=is.null&last_sequence=lt." + str(int(sequence)),
            {"last_sequence": int(sequence)},
            headers=headers,
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise DeviceRepositoryError("sequence_unavailable")
        return self._from_row(rows[0])

    def _request(self, method: str, suffix: str, payload=None, headers=None):
        try:
            response = requests.request(
                method,
                self._base_url + suffix,
                headers=headers or self._headers,
                json=payload,
                timeout=self.MAX_TIMEOUT_SECONDS,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise DeviceRepositoryError("durable_registry_unavailable")
            if not response.content:
                return None
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DeviceRepositoryError("durable_registry_unavailable") from exc

    @staticmethod
    def _to_row(device: DurableDeviceRecord) -> dict:
        import base64

        return {
            "device_id": device.device_id,
            "owner_id": device.owner_id,
            "label": device.label,
            "public_key_der_b64": base64.b64encode(device.public_key_der).decode("ascii"),
            "registered_at": int(device.registered_at),
            "revoked_at": device.revoked_at,
            "last_sequence": int(device.last_sequence),
        }

    @staticmethod
    def _from_row(row: dict) -> DurableDeviceRecord:
        import base64

        try:
            return DurableDeviceRecord(
                device_id=str(row["device_id"]),
                owner_id=str(row["owner_id"]),
                label=str(row["label"]),
                public_key_der=base64.b64decode(str(row["public_key_der_b64"]).encode("ascii"), validate=True),
                registered_at=int(row["registered_at"]),
                revoked_at=int(row["revoked_at"]) if row.get("revoked_at") is not None else None,
                last_sequence=int(row.get("last_sequence") or 0),
            )
        except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise DeviceRepositoryError("durable_registry_unavailable") from exc
