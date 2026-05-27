"""Password hashing (pbkdf2) and session tokens (JWT). Pure functions — the
JWT secret is always passed in explicitly so this module is trivially testable
and has no hidden global state."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

import jwt as pyjwt

_PBKDF2_ROUNDS = 200_000
_DEFAULT_TTL = 7 * 24 * 3600  # 7 days


def _pbkdf2(password: str, salt_hex: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS
    )
    return dk.hex()


def hash_password(password: str) -> tuple[str, str]:
    """Return ``(pwd_hash_hex, salt_hex)`` for a fresh random salt."""
    salt = secrets.token_hex(16)
    return _pbkdf2(password, salt), salt


def verify_password(password: str, pwd_hash: str, salt: str) -> bool:
    return hmac.compare_digest(_pbkdf2(password, salt), pwd_hash)


def mint_token(*, user_id: str, username: str, secret: str, ttl: int = _DEFAULT_TTL) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "username": username, "iat": now, "exp": now + ttl}
    return pyjwt.encode(payload, secret, algorithm="HS256")


def verify_token(token: str, secret: str) -> Optional[dict]:
    """Return the decoded claims, or None if invalid/expired/tampered."""
    try:
        return pyjwt.decode(token, secret, algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return None
