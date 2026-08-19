"""Local-only Phase 1 security tests for the Android companion identity broker."""

import base64
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from android_device_broker import (
    AndroidDeviceBroker,
    AndroidDeviceBrokerError,
    PHASE1_IDENTITY_EVENT_CODE,
    device_poll_payload,
    identity_receipt_payload,
    registration_payload,
)


def _key_material():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    return private_key, public_key_b64


def _sign(private_key, payload):
    return base64.b64encode(private_key.sign(payload, ec.ECDSA(hashes.SHA256()))).decode("ascii")


class AndroidDeviceBrokerTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.broker = AndroidDeviceBroker(now=lambda: self.now)
        self.owner_a = "owner-a"
        self.owner_b = "owner-b"
        self.private_key, self.public_key_b64 = _key_material()

    def register(self, owner_id=None):
        owner_id = owner_id or self.owner_a
        challenge = self.broker.issue_pairing_challenge(owner_id)
        label = "Hermes Android"
        registration_nonce = "registration-nonce-a"
        signature = _sign(
            self.private_key,
            registration_payload(
                challenge.pairing_id,
                challenge.challenge,
                label,
                self.public_key_b64,
                registration_nonce,
            ),
        )
        return self.broker.register_device(
            owner_id,
            challenge.pairing_id,
            label,
            self.public_key_b64,
            registration_nonce,
            signature,
        )

    def test_registration_is_owner_scoped_and_pairing_is_one_time(self):
        device = self.register()
        self.assertEqual([device.device_id], [item["device_id"] for item in self.broker.list_devices(self.owner_a)])
        self.assertEqual([], self.broker.list_devices(self.owner_b))

    def test_registration_rejects_wrong_owner_and_invalid_signature(self):
        challenge = self.broker.issue_pairing_challenge(self.owner_a)
        with self.assertRaises(AndroidDeviceBrokerError):
            self.broker.register_device(
                self.owner_b,
                challenge.pairing_id,
                "Hermes Android",
                self.public_key_b64,
                "nonce",
                "not-base64",
            )

    def test_identity_receipt_is_signed_expiring_and_single_use(self):
        device = self.register()
        nonce = "identity-nonce-a"
        expiry = self.now + 60
        payload = identity_receipt_payload(
            device.device_id,
            nonce,
            1,
            expiry,
            PHASE1_IDENTITY_EVENT_CODE,
        )
        signature = _sign(self.private_key, payload)
        self.assertTrue(
            self.broker.verify_identity_receipt(
                device.device_id,
                nonce,
                1,
                expiry,
                PHASE1_IDENTITY_EVENT_CODE,
                signature,
            )
        )
        with self.assertRaises(AndroidDeviceBrokerError):
            self.broker.verify_identity_receipt(
                device.device_id,
                nonce,
                1,
                expiry,
                PHASE1_IDENTITY_EVENT_CODE,
                signature,
            )

    def test_revocation_blocks_identity_receipts(self):
        device = self.register()
        self.assertTrue(self.broker.revoke_device(self.owner_a, device.device_id))
        payload = identity_receipt_payload(
            device.device_id,
            "identity-nonce-b",
            1,
            self.now + 60,
            PHASE1_IDENTITY_EVENT_CODE,
        )
        with self.assertRaises(AndroidDeviceBrokerError):
            self.broker.verify_identity_receipt(
                device.device_id,
                "identity-nonce-b",
                1,
                self.now + 60,
                PHASE1_IDENTITY_EVENT_CODE,
                _sign(self.private_key, payload),
            )

    def test_one_time_pairing_code_claim_is_expiring_and_cannot_be_replayed(self):
        session = self.broker.create_pairing_session(self.owner_a)
        self.assertEqual(20, len(session.code))
        self.assertNotIn(session.code, str(self.broker._pairings))
        challenge = self.broker.claim_pairing_session(session.code.lower())
        self.assertEqual(session.expires_at, challenge.expires_at)
        with self.assertRaises(AndroidDeviceBrokerError):
            self.broker.claim_pairing_session(session.code)

    def test_pairing_code_registration_preserves_owner_binding(self):
        session = self.broker.create_pairing_session(self.owner_a)
        challenge = self.broker.claim_pairing_session(session.code)
        label = "Hermes Android"
        nonce = "paired-registration-nonce"
        signature = _sign(
            self.private_key,
            registration_payload(challenge.pairing_id, challenge.challenge, label, self.public_key_b64, nonce),
        )
        device = self.broker.register_paired_device(challenge.pairing_id, label, self.public_key_b64, nonce, signature)
        self.assertEqual(self.owner_a, device.owner_id)
        self.assertEqual([], self.broker.list_devices(self.owner_b))


class AndroidDeviceRoutesTest(unittest.TestCase):
    def setUp(self):
        import app as hermes_app

        self.hermes_app = hermes_app
        self.client = hermes_app.app.test_client()
        self.private_key, self.public_key_b64 = _key_material()
        self.broker = AndroidDeviceBroker(now=lambda: 1_800_000_000)
        self.broker_patch = patch.object(hermes_app, "ANDROID_DEVICE_BROKER", self.broker)
        self.broker_patch.start()
        self.addCleanup(self.broker_patch.stop)

    def authenticated(self, owner_id):
        return patch.object(self.hermes_app, "get_auth_header_claims", return_value=({"sub": owner_id}, None))

    def register_route_device(self, owner_id="owner-a"):
        with self.authenticated(owner_id):
            challenge = self.client.post("/android/devices/pairing-challenge").get_json()
            label = "Hermes Android"
            nonce = "route-registration-nonce"
            signature = _sign(
                self.private_key,
                registration_payload(
                    challenge["pairing_id"],
                    challenge["challenge"],
                    label,
                    self.public_key_b64,
                    nonce,
                ),
            )
            response = self.client.post(
                "/android/devices/register",
                json={
                    "pairing_id": challenge["pairing_id"],
                    "label": label,
                    "public_key": self.public_key_b64,
                    "registration_nonce": nonce,
                    "signature": signature,
                },
            )
        self.assertEqual(201, response.status_code)
        return response.get_json()["device"]

    def test_routes_are_owner_scoped_and_never_create_activity_runs(self):
        self.hermes_app.RUN_REGISTRY.clear()
        device = self.register_route_device()
        with self.authenticated("owner-b"):
            listed = self.client.get("/android/devices")
            revoked = self.client.post(f"/android/devices/{device['device_id']}/revoke")
        self.assertEqual([], listed.get_json()["devices"])
        self.assertEqual(404, revoked.status_code)
        self.assertEqual({}, self.hermes_app.RUN_REGISTRY)

    def test_identity_receipt_endpoint_never_emits_agent_activity(self):
        self.hermes_app.RUN_REGISTRY.clear()
        device = self.register_route_device()
        expiry = 1_800_000_060
        nonce = "route-identity-nonce"
        signature = _sign(
            self.private_key,
            identity_receipt_payload(
                device["device_id"], nonce, 1, expiry, PHASE1_IDENTITY_EVENT_CODE
            ),
        )
        response = self.client.post(
            f"/android/devices/{device['device_id']}/identity-receipt",
            json={
                "receipt_nonce": nonce,
                "sequence": 1,
                "expires_at": expiry,
                "safe_event_code": PHASE1_IDENTITY_EVENT_CODE,
                "signature": signature,
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "identity_verified"}, response.get_json())
        self.assertEqual({}, self.hermes_app.RUN_REGISTRY)

    def test_pairing_session_routes_do_not_transfer_web_auth_to_android(self):
        self.hermes_app.RUN_REGISTRY.clear()
        with self.authenticated("owner-a"):
            session_response = self.client.post("/android/devices/pairing-session")
        self.assertEqual(201, session_response.status_code)
        session = session_response.get_json()
        self.assertEqual(20, len(session["pairing_code"]))
        claim = self.client.post("/android/devices/pairing-session/claim", json={"pairing_code": session["pairing_code"]})
        self.assertEqual(200, claim.status_code)
        challenge = claim.get_json()
        label = "Hermes Android"
        nonce = "pairing-session-route-nonce"
        signature = _sign(
            self.private_key,
            registration_payload(challenge["pairing_id"], challenge["challenge"], label, self.public_key_b64, nonce),
        )
        registration = self.client.post(
            "/android/devices/pairing-register",
            json={
                "pairing_id": challenge["pairing_id"],
                "label": label,
                "public_key": self.public_key_b64,
                "registration_nonce": nonce,
                "signature": signature,
            },
        )
        self.assertEqual(201, registration.status_code)
        replay = self.client.post("/android/devices/pairing-session/claim", json={"pairing_code": session["pairing_code"]})
        self.assertGreaterEqual(replay.status_code, 400)
        with self.authenticated("owner-a"):
            devices = self.client.get("/android/devices")
        self.assertEqual(1, len(devices.get_json()["devices"]))
        self.assertEqual({}, self.hermes_app.RUN_REGISTRY)

    def test_device_poll_is_signed_and_revocation_blocks_delivery(self):
        device = self.register_route_device()
        expiry = 1_800_000_060
        poll = self.client.post(
            f"/android/devices/{device['device_id']}/commands/poll",
            json={
                "poll_nonce": "device-poll-nonce-a",
                "sequence": 1,
                "expires_at": expiry,
                "signature": _sign(self.private_key, device_poll_payload(device["device_id"], "device-poll-nonce-a", 1, expiry)),
            },
        )
        self.assertEqual(200, poll.status_code)
        self.assertIsNone(poll.get_json()["command"])
        with self.authenticated("owner-a"):
            self.assertEqual(200, self.client.post(f"/android/devices/{device['device_id']}/revoke").status_code)
        blocked = self.client.post(
            f"/android/devices/{device['device_id']}/commands/poll",
            json={
                "poll_nonce": "device-poll-nonce-b",
                "sequence": 2,
                "expires_at": expiry,
                "signature": _sign(self.private_key, device_poll_payload(device["device_id"], "device-poll-nonce-b", 2, expiry)),
            },
        )
        self.assertGreaterEqual(blocked.status_code, 400)
