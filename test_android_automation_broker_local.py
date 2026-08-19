import base64
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from android_automation_broker import (
    ANDROID_SAFE_EVENT_SUCCESS,
    AndroidAutomationBroker,
    AndroidAutomationBrokerError,
    AndroidPackageProfile,
    android_receipt_payload,
)
from android_device_broker import AndroidDeviceBroker, registration_payload
from tool_contracts import ToolCommand, build_action_digest


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


class AndroidAutomationBrokerTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.owner_a = "owner-a"
        self.owner_b = "owner-b"
        self.private_key, self.public_key_b64 = _key_material()
        self.devices = AndroidDeviceBroker(now=lambda: self.now)
        challenge = self.devices.issue_pairing_challenge(self.owner_a)
        registration_nonce = "registration-nonce"
        self.device = self.devices.register_device(
            self.owner_a,
            challenge.pairing_id,
            "Hermes Android",
            self.public_key_b64,
            registration_nonce,
            _sign(
                self.private_key,
                registration_payload(
                    challenge.pairing_id,
                    challenge.challenge,
                    "Hermes Android",
                    self.public_key_b64,
                    registration_nonce,
                ),
            ),
        )
        self.package_id = "com.example.safe"
        self.certificate = "a" * 64
        self.profile = AndroidPackageProfile(
            package_id=self.package_id,
            certificate_sha256=self.certificate,
            allowed_actions=frozenset({"OPEN_APP", "OBSERVE", "TAP", "TYPE"}),
            selector_ids=frozenset({"open", "observe", "submit", "safe_text"}),
        )
        self.broker = AndroidAutomationBroker(
            self.devices,
            profiles=(self.profile,),
            now=lambda: self.now,
        )

    def command(self, action="TAP", selector_id="submit", owner_id=None):
        owner_id = owner_id or self.owner_a
        return ToolCommand(
            command_id=f"cmd-{action.lower()}-{selector_id}",
            run_id="run-a",
            owner_id=owner_id,
            tool_id="android_companion",
            command_name=action.lower(),
            site_id=self.package_id,
            permitted_artifact_reference=None,
            action_digest=build_action_digest(
                owner_id,
                "run-a",
                "android_companion",
                action.lower(),
                self.package_id,
                None,
            ),
            expires_at=self.now + 90,
        )

    def request(self, action="TAP", selector_id="submit", text=None):
        return self.broker.request_command(
            self.owner_a,
            self.device.device_id,
            self.command(action, selector_id),
            self.package_id,
            self.certificate,
            action,
            selector_id,
            text,
        )

    def test_default_deny_and_owner_binding_reject_unreviewed_or_cross_owner_commands(self):
        denied = AndroidAutomationBroker(self.devices, now=lambda: self.now)
        with self.assertRaisesRegex(AndroidAutomationBrokerError, "package_not_allowed"):
            denied.request_command(
                self.owner_a,
                self.device.device_id,
                self.command(),
                self.package_id,
                self.certificate,
                "TAP",
                "submit",
            )

        with self.assertRaisesRegex(AndroidAutomationBrokerError, "command_not_bound"):
            self.broker.request_command(
                self.owner_a,
                self.device.device_id,
                self.command(owner_id=self.owner_b),
                self.package_id,
                self.certificate,
                "TAP",
                "submit",
            )

    def test_sensitive_type_payload_and_text_outside_type_are_rejected(self):
        with self.assertRaisesRegex(AndroidAutomationBrokerError, "sensitive_text_blocked"):
            self.request("TYPE", "safe_text", "OTP 123456")
        with self.assertRaisesRegex(AndroidAutomationBrokerError, "payload_not_allowed"):
            self.request("TAP", "submit", "unexpected")

    def test_cancelled_or_expired_commands_are_not_delivered_or_receipted(self):
        pending = self.request()
        self.assertTrue(self.broker.cancel(self.owner_a, self.device.device_id, pending.command.command_id))
        self.assertIsNone(self.broker.next_for_device(self.device.device_id))
        with self.assertRaisesRegex(AndroidAutomationBrokerError, "command_unavailable"):
            self.broker.verify_device_receipt(
                self.owner_a,
                self.device.device_id,
                pending.command.command_id,
                "receipt-cancelled",
                1,
                self.now + 30,
                ANDROID_SAFE_EVENT_SUCCESS,
                "not-a-valid-signature",
            )

    def test_only_matching_signed_receipt_completes_command_once(self):
        pending = self.request()
        delivery = self.broker.next_for_device(self.device.device_id)
        self.assertEqual(pending.command.command_id, delivery["command_id"])
        receipt_nonce = "receipt-success"
        expiry = self.now + 30
        signature = _sign(
            self.private_key,
            android_receipt_payload(
                pending,
                receipt_nonce,
                1,
                expiry,
                ANDROID_SAFE_EVENT_SUCCESS,
            ),
        )
        receipt = self.broker.verify_device_receipt(
            self.owner_a,
            self.device.device_id,
            pending.command.command_id,
            receipt_nonce,
            1,
            expiry,
            ANDROID_SAFE_EVENT_SUCCESS,
            signature,
        )
        self.assertEqual("completed", receipt.observed_state)
        self.assertTrue(self.broker.verify_receipt(pending.command, receipt))
        with self.assertRaisesRegex(AndroidAutomationBrokerError, "command_unavailable"):
            self.broker.verify_device_receipt(
                self.owner_a,
                self.device.device_id,
                pending.command.command_id,
                receipt_nonce,
                1,
                expiry,
                ANDROID_SAFE_EVENT_SUCCESS,
                signature,
            )

    def test_revoked_device_cannot_receive_a_pending_command(self):
        self.request()
        self.assertTrue(self.devices.revoke_device(self.owner_a, self.device.device_id))
        self.assertIsNone(self.broker.next_for_device(self.device.device_id))
