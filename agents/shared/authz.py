from __future__ import annotations

from typing import Any

import jwt as pyjwt


class AuthzError(Exception):
    pass


def verify_grant(token: str, *, key: str, requested_tool: str) -> dict[str, Any]:
    """Verify the authz_grant JWT. Returns the decoded claims on success.

    Raises AuthzError on signature failure, expiry, or tool not in allowed_tools.
    Note: `sub` is audit-only (capability delegation model); gating is purely
    via `allowed_tools` containment.
    """
    try:
        claims = pyjwt.decode(token, key, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise AuthzError("authz_grant expired") from None
    except pyjwt.InvalidSignatureError:
        raise AuthzError("authz_grant signature invalid") from None
    except pyjwt.PyJWTError as exc:
        raise AuthzError(f"authz_grant decode error: {exc}") from exc

    allowed = claims.get("allowed_tools") or []
    if requested_tool not in allowed:
        raise AuthzError(
            f"tool {requested_tool!r} not in allowed_tools {allowed!r}"
        )
    return claims


# --- Cross-machine grants (comm-agent) -------------------------------------

import secrets
import time
from collections import OrderedDict


def sign_cross_machine_grant(
    *,
    my_peer_id: str,
    target_peer_id: str,
    requested_skill: str,
    key: str,
    ttl_seconds: int = 60,
) -> str:
    """Sign an HMAC grant for one cross-machine A2A call.

    Claims:
      - peer_id: caller's self-identity
      - target_peer_id: who the verifier MUST be (anti-forward)
      - requested_skill: A2A skill id we're calling
      - nonce: 16-byte hex random (anti-replay; verifier remembers it)
      - exp: unix timestamp
    """
    claims = {
        "peer_id": my_peer_id,
        "target_peer_id": target_peer_id,
        "requested_skill": requested_skill,
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + ttl_seconds,
    }
    return pyjwt.encode(claims, key, algorithm="HS256")


def verify_cross_machine_grant(
    token: str,
    *,
    key: str,
    my_peer_id: str,
    requested_skill: str,
) -> dict[str, Any]:
    """Verify a cross-machine grant. Returns claims on success.

    Note: nonce replay-check is the CALLER's job (use NonceCache); this
    function only validates signature/exp/target/skill so the caller can
    skip the cache lookup on tampered grants.
    """
    try:
        claims = pyjwt.decode(token, key, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise AuthzError("cross-machine grant expired") from None
    except pyjwt.InvalidSignatureError:
        raise AuthzError("cross-machine grant signature invalid") from None
    except pyjwt.PyJWTError as exc:
        raise AuthzError(f"cross-machine grant decode error: {exc}") from exc

    if claims.get("target_peer_id") != my_peer_id:
        raise AuthzError(
            f"target_peer_id {claims.get('target_peer_id')!r} does not match "
            f"local peer_id {my_peer_id!r} (anti-forward check)"
        )
    if claims.get("requested_skill") != requested_skill:
        raise AuthzError(
            f"requested_skill mismatch: grant says "
            f"{claims.get('requested_skill')!r}, route is {requested_skill!r}"
        )
    return claims


class NonceCache:
    """Bounded-size LRU with TTL for anti-replay nonces.

    Spec §6.2: 10 000 entries, 60-second TTL by default. Cache fills up
    in the verifier; eviction by LRU once full, by TTL on lookup.
    """

    def __init__(self, *, maxlen: int = 10000, ttl_seconds: int = 60):
        self._maxlen = maxlen
        self._ttl = ttl_seconds
        # nonce -> unix_ts_when_inserted
        self._entries: OrderedDict[str, float] = OrderedDict()

    def check_and_remember(self, nonce: str) -> bool:
        """Return True if first time seen; False if replay."""
        now = time.time()
        # Drop expired entries lazily on access (cheap because OrderedDict
        # popitem(last=False) is O(1)).
        while self._entries:
            oldest_nonce, inserted_at = next(iter(self._entries.items()))
            if now - inserted_at <= self._ttl:
                break
            self._entries.popitem(last=False)
        if nonce in self._entries:
            return False
        # Capacity guard: evict LRU before inserting.
        while len(self._entries) >= self._maxlen:
            self._entries.popitem(last=False)
        self._entries[nonce] = now
        return True
