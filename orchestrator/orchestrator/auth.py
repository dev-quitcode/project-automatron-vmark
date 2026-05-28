"""Auth.js (NextAuth v5) session JWT verification for FastAPI.

Auth.js issues encrypted JWTs (JWE, A256CBC-HS512) keyed by AUTH_SECRET via HKDF
with the salt `"Auth.js Generated Encryption Key (authjs.session-token)"`. We
decrypt the same way and validate `exp` + the email allowlist.

The web-ui (Next.js + next-auth@beta) and this module MUST share AUTH_SECRET and
AUTOMATRON_ALLOWED_EMAILS via environment variables.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from jose import jwe

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Auth.js v5 cookie names — production has __Secure- prefix, dev does not
_COOKIE_NAMES = (
    "__Secure-authjs.session-token",
    "authjs.session-token",
    # NextAuth v4 names for backwards compat if someone hasn't upgraded
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)


def _hkdf_salt(cookie_name: str) -> bytes:
    """Auth.js derives the salt from the cookie name itself, so __Secure- prefix matters."""
    return f"Auth.js Generated Encryption Key ({cookie_name})".encode()


def _hkdf_sha256(secret: bytes, salt: bytes, info: bytes, length: int = 64) -> bytes:
    """RFC 5869 HKDF-Extract + HKDF-Expand with SHA-256, returning `length` bytes.

    Auth.js uses HKDF-SHA256 → 64 bytes for A256CBC-HS512 (32-byte HMAC key +
    32-byte AES-256-CBC key concatenated).
    """
    # Extract
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    # Expand
    output = b""
    last_block = b""
    counter = 1
    while len(output) < length:
        last_block = hmac.new(
            prk, last_block + info + bytes([counter]), hashlib.sha256
        ).digest()
        output += last_block
        counter += 1
    return output[:length]


def _derive_key(secret: str, cookie_name: str) -> bytes:
    return _hkdf_sha256(secret.encode(), _hkdf_salt(cookie_name), b"", length=64)


def decode_authjs_jwt(token: str, cookie_name: str) -> dict[str, Any] | None:
    """Decrypt the Auth.js session JWE. Returns the payload dict, or None on failure.

    The cookie name is part of the HKDF salt — `__Secure-` prefix in production
    means a different key, so the caller must pass the actual cookie name.
    """
    if not token or not settings.auth_secret:
        return None
    try:
        key = _derive_key(settings.auth_secret, cookie_name)
        decrypted = jwe.decrypt(token, key)
        if decrypted is None:
            return None
        return json.loads(decrypted)
    except Exception as exc:
        logger.debug("auth: JWE decrypt failed for cookie %s: %s", cookie_name, exc)
        return None


def _extract_token_from_cookies(cookies: dict[str, str]) -> tuple[str, str] | None:
    """Return (cookie_name, token_value) for the first matching session cookie."""
    for name in _COOKIE_NAMES:
        v = cookies.get(name)
        if v:
            return name, v
    return None


def _allowlisted(email: str) -> bool:
    """Check the email against AUTOMATRON_ALLOWED_EMAILS rules.

    Each comma-separated rule is either:
      - A full email address (exact match), e.g. `dev@quitcode.com`
      - A domain pattern starting with `@`, e.g. `@quitcode.com` matches any
        email at that domain.

    Empty list means the gate is delegated to Google (OAuth consent screen →
    Internal Workspace app, or Test users list).
    """
    rules = [r.strip().lower() for r in settings.automatron_allowed_emails.split(",") if r.strip()]
    if not rules:
        return True
    email_lower = (email or "").lower()
    for rule in rules:
        if rule.startswith("@"):
            if email_lower.endswith(rule):
                return True
        elif email_lower == rule:
            return True
    return False


def _is_auth_configured() -> bool:
    """Auth is wired up as soon as we have a secret to decrypt the JWT. The
    email allowlist is optional — Google Cloud Console can gate sign-in itself."""
    return bool(settings.auth_secret)


async def require_auth(request: Request) -> dict[str, Any]:
    """FastAPI dependency. Validate the Auth.js session cookie or raise 401.

    Returns the session payload (typically `{"sub": ..., "email": ..., "name": ..., "exp": ...}`).
    """
    if settings.automatron_dev_no_auth:
        return {"email": "dev@localhost", "name": "Dev User", "sub": "dev"}

    if not _is_auth_configured():
        # Auth is not configured yet — fail closed in prod, pass in dev
        if settings.debug:
            logger.warning("auth: AUTH_SECRET / AUTOMATRON_ALLOWED_EMAILS not set; allowing request in debug mode")
            return {"email": "unconfigured@localhost", "name": "Unconfigured", "sub": "anon"}
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth not configured")

    cookie_pair = _extract_token_from_cookies(request.cookies)
    if not cookie_pair:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    cookie_name, token = cookie_pair

    payload = decode_authjs_jwt(token, cookie_name)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    email = payload.get("email", "")
    if not _allowlisted(email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email not allowlisted")

    return payload


def authenticate_socketio_environ(environ: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the cookie on a Socket.IO connect handshake. Returns None to reject.

    Used in api/websocket.py's on_connect handler. Returning None tells Socket.IO
    to refuse the connection.
    """
    if settings.automatron_dev_no_auth:
        return {"email": "dev@localhost", "sub": "dev"}
    if not _is_auth_configured():
        if settings.debug:
            return {"email": "unconfigured@localhost", "sub": "anon"}
        return None

    # Parse cookies out of the WSGI/ASGI environ
    raw_cookie = environ.get("HTTP_COOKIE", "")
    cookies: dict[str, str] = {}
    for pair in raw_cookie.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies[k.strip()] = v.strip()

    cookie_pair = _extract_token_from_cookies(cookies)
    if not cookie_pair:
        return None
    cookie_name, token = cookie_pair
    payload = decode_authjs_jwt(token, cookie_name)
    if not payload:
        return None
    if not _allowlisted(payload.get("email", "")):
        return None
    return payload
