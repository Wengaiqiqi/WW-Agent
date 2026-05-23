"""Tests for cross-machine HMAC grants (extension of authz.py)."""
from __future__ import annotations

import time

import pytest

from agents.shared.authz import (
    AuthzError,
    NonceCache,
    sign_cross_machine_grant,
    verify_cross_machine_grant,
)


KEY = "test-shared-secret"


def test_signed_grant_round_trip() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="laptop",
        target_peer_id="home",
        requested_skill="task.delegate",
        key=KEY,
        ttl_seconds=60,
    )
    claims = verify_cross_machine_grant(
        token,
        key=KEY,
        my_peer_id="home",  # verifier's identity == claim's target
        requested_skill="task.delegate",
    )
    assert claims["peer_id"] == "laptop"
    assert claims["target_peer_id"] == "home"
    assert claims["requested_skill"] == "task.delegate"
    assert "nonce" in claims


def test_wrong_key_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="signature"):
        verify_cross_machine_grant(
            token, key="WRONG", my_peer_id="b", requested_skill="x",
        )


def test_wrong_target_rejected() -> None:
    """grant says target='b' but we are 'c' → reject (anti-forward)."""
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="target_peer_id"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="c", requested_skill="x",
        )


def test_wrong_skill_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="requested_skill"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="b", requested_skill="y",
        )


def test_expired_grant_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=-1,  # already expired
    )
    with pytest.raises(AuthzError, match="expired"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="b", requested_skill="x",
        )


def test_nonce_cache_replay_blocked() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=60)
    assert cache.check_and_remember("nonce-1") is True   # first time: OK
    assert cache.check_and_remember("nonce-1") is False  # replay: blocked


def test_nonce_cache_distinct_nonces_pass() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=60)
    assert cache.check_and_remember("a") is True
    assert cache.check_and_remember("b") is True
    assert cache.check_and_remember("a") is False


def test_nonce_cache_evicts_old_entries_when_full() -> None:
    cache = NonceCache(maxlen=2, ttl_seconds=60)
    cache.check_and_remember("a")
    cache.check_and_remember("b")
    cache.check_and_remember("c")  # evicts "a"
    # "a" is gone → not a replay any more
    assert cache.check_and_remember("a") is True


def test_nonce_cache_expires_by_ttl() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=0)  # immediate expiry
    cache.check_and_remember("a")
    time.sleep(0.01)
    # After TTL passes, "a" is no longer a replay
    assert cache.check_and_remember("a") is True
