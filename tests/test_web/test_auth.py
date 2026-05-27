from __future__ import annotations

import time

from web import auth


def test_password_roundtrip():
    pwd_hash, salt = auth.hash_password("hunter2")
    assert pwd_hash and salt
    assert auth.verify_password("hunter2", pwd_hash, salt)
    assert not auth.verify_password("wrong", pwd_hash, salt)


def test_distinct_salts_per_hash():
    h1, s1 = auth.hash_password("same")
    h2, s2 = auth.hash_password("same")
    assert s1 != s2 and h1 != h2  # salted: same password, different stored hash


def test_token_roundtrip():
    tok = auth.mint_token(user_id="u1", username="alice", secret="sek")
    claims = auth.verify_token(tok, "sek")
    assert claims["sub"] == "u1"
    assert claims["username"] == "alice"


def test_token_wrong_secret_rejected():
    tok = auth.mint_token(user_id="u1", username="alice", secret="sek")
    assert auth.verify_token(tok, "other") is None


def test_token_expired_rejected():
    tok = auth.mint_token(user_id="u1", username="alice", secret="sek", ttl=-1)
    assert auth.verify_token(tok, "sek") is None


def test_token_garbage_rejected():
    assert auth.verify_token("not-a-jwt", "sek") is None
