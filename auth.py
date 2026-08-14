"""
Supabase JWT validation for Hermes AI (Phase 3-A: Google Authentication).

Loads Supabase's public JWKS from
https://<project>.supabase.co/auth/v1/.well-known/jwks.json
and validates RS256 Supabase access tokens offline (no DB round-trip).
Anonymous requests are allowed: this module is opt-in per route.

Secrets: Supabase service role key is read ONLY from env vars (Render
Environment Variables). No secret is ever hard-coded or committed.
"""

import os
import threading
import time
from functools import lru_cache

import jwt  # PyJWT
import requests

from auth_google import verify_google_token

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://bjoljeysryycwflhcnha.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""

_jwks_cache = {"keys": None, "fetched_at": 0.0}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 3600


def _fetch_jwks():
    """Fetch the Supabase auth JWKS (cached for CACHE_TTL_SECONDS)."""
    with _cache_lock:
        now = time.time()
        if _jwks_cache["keys"] is not None and now - _jwks_cache["fetched_at"] < CACHE_TTL_SECONDS:
            return _jwks_cache["keys"]
        url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
        headers = {}
        if SUPABASE_ANON_KEY:
            # This Supabase project enforces an API key on /auth/v1 endpoints.
            headers["apikey"] = SUPABASE_ANON_KEY
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        keys = response.json().get("keys", [])
        _jwks_cache["keys"] = keys
        _jwks_cache["fetched_at"] = now
        return keys


def _get_public_key(kid):
    try:
        keys = _fetch_jwks()
    except requests.HTTPError:
        # The public-key discovery endpoint is unreachable (e.g. invalid API
        # key, network failure, or project outage). We cannot validate the
        # token, so treat it as non-verifiable (401) rather than a 500 error.
        return None
    for key_data in keys:
        if key_data.get("kid") == kid:
            return _build_public_key(key_data)
    return None


def _build_public_key(key_data):
    """Build a public key object from a JWK dict, supporting EC (ES256) and RSA (RS256)."""
    public = {k: v for k, v in key_data.items() if k != "kid"}
    kty = key_data.get("kty", "")
    if kty == "EC":
        return jwt.algorithms.ECAlgorithm.from_jwk(public)
    if kty == "RSA":
        return jwt.algorithms.RSAAlgorithm.from_jwk(public)
    raise ValueError(f"Unsupported key type: {kty}")


def _decode_jwks_public_key(key_data):
    """Build an RSA public key from a JWK dict that lacks 'kid' (local build)."""
    return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)


def verify_supabase_token(token):
    """
    Validate a Supabase access token.

    Returns (claims_dict, None) on success, or (None, error_message) on failure.
    Does not log or expose the token value beyond this function scope.
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
    if alg not in ("ES256", "RS256"):
        return None, f"Unsupported signing algorithm: {alg}"

    public_key = _get_public_key(kid)
    if public_key is None:
        return None, "No matching public key for kid"

    algorithms = [alg] if alg in ("ES256", "RS256") else ["RS256"]
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=algorithms,
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

    if claims.get("role") != "authenticated":
        return None, "Token role is not authenticated"

    return claims, None


def get_auth_header_claims(request):
    """
    Extract and validate the optional Bearer token from a Flask request.

    Tries Supabase validation first (access tokens from the Supabase token
    exchange). If the token is clearly a Supabase-format token (has a `kid`
    and a `role` claim) but invalid, it fails immediately. Otherwise, as a
    fallback for Option B (native Credential Manager Google sign-in), it
    validates the token as a Google-signed ID token against Google's public
    JWKS.

    Returns (claims_dict, None) when a valid token is present,
    (None, None) when no token is given (anonymous), or
    (None, error_message) when the token is present but invalid.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, None
    token = header[len("Bearer ") :].strip()
    if not token:
        return None, None
    claims, err = verify_supabase_token(token)
    if claims is not None:
        return claims, None
    # Only fall back to Google validation when the token is not a Supabase
    # token at all (no key id and no role claim). A Supabase token that
    # failed validation is rejected outright.
    try:
        payload = jwt.get_unverified_claims(token)
    except Exception:
        payload = {}
    if payload.get("kid") or payload.get("role") is not None:
        return None, err or "Invalid token"
    return verify_google_token(token)
