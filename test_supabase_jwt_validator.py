"""Local-only regressions for Hermes Supabase access-token validation.

Every token in this file is synthetic and signed by an ephemeral key. The
tests patch JWKS lookup in-process, so they do not contact Supabase, Render,
Google, or any production service.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

import app
import auth


class RequestWithBearer:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


def make_token(private_key, *, audience="authenticated", issuer=None, role="authenticated"):
    return jwt.encode(
        {
            "iss": issuer or auth.SUPABASE_ISSUER,
            "aud": audience,
            "sub": "00000000-0000-0000-0000-000000000001",
            "email": "local-test@example.invalid",
            "role": role,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "local-test-key"},
    )


def with_local_key(private_key):
    return patch.object(
        auth,
        "_get_public_key",
        side_effect=lambda kid: private_key.public_key() if kid == "local-test-key" else None,
    )


def test_valid_supabase_es256_token_is_accepted_by_validator_and_verify_route() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    token = make_token(private_key)

    with with_local_key(private_key):
        claims, error = auth.verify_supabase_token(token)
        response = app.app.test_client().get(
            "/auth/verify", headers={"Authorization": f"Bearer {token}"}
        )

    assert error is None
    assert claims["role"] == "authenticated"
    assert response.status_code == 200
    assert response.get_json()["auth_mode"] == "authenticated"
    assert response.get_json()["role"] == "authenticated"


def test_wrong_audience_is_rejected_without_google_fallback() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    token = make_token(private_key, audience="unexpected-audience")
    fallback = Mock(return_value=({}, None))

    with with_local_key(private_key), patch.object(auth, "verify_google_token", fallback):
        claims, error = auth.get_auth_header_claims(RequestWithBearer(token))

    assert claims is None
    assert error == "Invalid audience"
    fallback.assert_not_called()


def test_wrong_issuer_is_rejected_without_google_fallback() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    token = make_token(private_key, issuer="https://untrusted.example/auth/v1")
    fallback = Mock(return_value=({}, None))

    with with_local_key(private_key), patch.object(auth, "verify_google_token", fallback):
        claims, error = auth.get_auth_header_claims(RequestWithBearer(token))

    assert claims is None
    assert error == "Invalid issuer"
    fallback.assert_not_called()


def test_google_shaped_token_still_uses_google_fallback() -> None:
    token = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": "local-google-client",
            "sub": "local-google-subject",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        key="",
        algorithm="none",
    )
    expected_claims = {"sub": "local-google-subject"}

    with patch.object(auth, "verify_google_token", return_value=(expected_claims, None)) as fallback:
        claims, error = auth.get_auth_header_claims(RequestWithBearer(token))

    assert error is None
    assert claims == expected_claims
    fallback.assert_called_once_with(token)


def test_authenticated_chat_accepts_the_validated_supabase_token() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    token = make_token(private_key)
    previous_hermes_key = app.HERMES_KEY
    app.HERMES_KEY = "local-test-backend-key"
    agent_response = Mock(status_code=200)
    agent_response.json.return_value = {"choices": [{"message": {"content": "local test reply"}}]}

    try:
        with with_local_key(private_key), patch.object(app.requests, "post", return_value=agent_response):
            response = app.app.test_client().post(
                "/chat",
                json={"message": "hello"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.HERMES_KEY = previous_hermes_key

    assert response.status_code == 200
    assert response.get_json() == {"reply": "local test reply", "auth_mode": "authenticated"}


def main() -> None:
    test_valid_supabase_es256_token_is_accepted_by_validator_and_verify_route()
    print("PASS: valid Supabase ES256 access token is accepted by validator and /auth/verify")
    test_wrong_audience_is_rejected_without_google_fallback()
    print("PASS: wrong audience stays in the Supabase validation path")
    test_wrong_issuer_is_rejected_without_google_fallback()
    print("PASS: wrong issuer stays in the Supabase validation path")
    test_google_shaped_token_still_uses_google_fallback()
    print("PASS: Google-shaped token retains additive Google fallback")
    test_authenticated_chat_accepts_the_validated_supabase_token()
    print("PASS: authenticated /chat accepts validated Supabase token")


if __name__ == "__main__":
    main()
