"""
Google-signed ID token validation for Hermes AI (Phase 3-A: Option B —
Native Credential Manager).

Loads Google's public JWKS from https://www.googleapis.com/oauth2/v3/certs
and validates Google-issued RS256 ID tokens offline (signature, issuer,
audience, expiry). Used when the frontend signs in via Android's native
Credential Manager, which returns a Google-signed ID token instead of a
Supabase access token.

This module is purely additive: it never replaces Supabase validation and
makes no DB, schema, or RLS changes.

Secrets: Google publishes its JWKS publicly — no key or secret is required.
No secret is hard-coded here beyond the public web client IDs needed for
audience checking.
"""

import time
from functools import lru_cache

import jwt  # PyJWT
import requests

# The web OAuth client IDs whose audiences we accept in Google-signed ID
# tokens. Keep this a set so additional client IDs can be added later.
GOOGLE_ALLOWED_CLIENT_IDS = frozenset([
    "131546003267-6njnutgr106kcba8m8cg2u9lco55u448.apps.googleusercontent.com",
])

# Valid issuers for Google ID tokens (current and legacy).
GOOGLE_ISSUERS = frozenset([
    "https://accounts.google.com",
    "https://oauth2.googleapis.com",
])

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

_jwks_cache = {"keys": None, "fetched_at": 0.0}
CACHE_TTL_SECONDS = 3600


def _fetch_google_jwks():
    """Fetch Google's public JWKS (cached for CACHE_TTL_SECONDS)."""
    now = time.time()
    if _jwks_cache["keys"] is not None and now - _jwks_cache["fetched_at"] < CACHE_TTL_SECONDS:
        return _jwks_cache["keys"]
    response = requests.get(GOOGLE_JWKS_URL, timeout=15)
    response.raise_for_status()
    keys = response.json().get("keys", [])
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


def _get_google_public_key(kid):
    """Return an RSA public key for the given kid from Google's JWKS."""
    try:
        keys = _fetch_google_jwks()
    except requests.HTTPError:
        # Google's public-key discovery endpoint is unreachable. We cannot
        # validate the token, so treat it as non-verifiable (401).
        return None
    for key_data in keys:
        if key_data.get("kid") == kid:
            # Google's certs are RSA — kid-less decoding is safe here.
            public = {k: v for k, v in key_data.items() if k != "kid"}
            return jwt.algorithms.RSAAlgorithm.from_jwk(public)
    return None


def verify_google_token(token):
    """
    Validate a Google-signed ID token.

    Checks: RS256 signature against Google's public JWKS, issuer
    (accounts.google.com / oauth2.googleapis.com), audience (our web client
    ID), and expiry.

    Returns (claims_dict, None) on success, or (None, error_message) on
    failure. Does not log or expose the token value beyond this scope.
    """
    if not token or not isinstance(token, str):
        return None, "Missing token"
    parts = token.split(".")
    if len(parts) != 3:
        return None, "Malformed token"

    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        return None, f"Invalid token header: {e}"
    kid = header.get("kid")
    alg = header.get("alg", "")
    if not kid:
        return None, "Token has no key id (kid)"
    if alg != "RS256":
        return None, f"Unsupported signing algorithm: {alg}"

    public_key = _get_google_public_key(kid)
    if public_key is None:
        return None, "No matching public key for kid"

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=list(GOOGLE_ALLOWED_CLIENT_IDS),
            options={"verify_exp": True},
        )
    except jwt.exceptions.ExpiredSignatureError:
        return None, "Token expired"
    except jwt.exceptions.InvalidAudienceError:
        return None, "Invalid audience"
    except jwt.exceptions.InvalidSignatureError:
        return None, "Invalid signature"
    except jwt.exceptions.DecodeError as exc:
        return None, f"Token decode failed: {exc}"
    except jwt.exceptions.PyJWTError as exc:
        return None, f"Token validation failed: {exc}"

    iss = claims.get("iss", "")
    if iss not in GOOGLE_ISSUERS:
        return None, "Invalid issuer"

    return claims, None
