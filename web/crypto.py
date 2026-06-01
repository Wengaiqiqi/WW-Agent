"""Symmetric encryption for secrets stored at rest (custom-endpoint API keys).

Keys are derived deterministically from the web auth secret (see
``web.config.auth_secret``), so as long as that secret is stable — set
``WEB_AUTH_SECRET`` in prod, or rely on the persisted dev secret — ciphertext
round-trips across restarts and workers. The store keeps only ciphertext; the
plaintext key exists in memory only for the duration of a turn.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from web import config


def _fernet() -> Fernet:
    # Fernet wants a 32-byte urlsafe-base64 key. Derive it from the auth secret
    # via SHA-256 so any secret length yields a valid key.
    digest = hashlib.sha256(config.auth_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty input stays empty."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a stored secret. Returns "" if the token is empty or can't be
    decrypted (e.g. the auth secret rotated), so callers fail as "no key
    configured" rather than crashing."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
