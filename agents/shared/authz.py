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
import threading
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
    """Bounded-size FIFO cache with TTL for anti-replay nonces.

    Spec §6.2: 10 000 entries, 60-second TTL by default. Entries are inserted
    in arrival order and never reordered, so the front is always the oldest —
    eviction is FIFO (by age) when full, and by TTL on lookup. This is
    deliberately NOT an LRU: for replay defense you want to retain the *newest*
    nonces until they expire, not whichever was queried most recently.

    Thread-safe: ``check_and_remember`` holds a lock so the check-then-insert
    can't interleave across threads. FastAPI/Starlette may run request handlers
    on a worker-thread pool, and without the lock two concurrent requests
    carrying the same nonce could both observe "not seen" and both pass.

    Process-LOCAL only. For uvicorn ``--workers N`` deployments (each worker
    is its own process) use ``SqliteNonceStore`` instead — otherwise the same
    grant can be accepted once per worker, silently weakening §6.2.
    """

    def __init__(self, *, maxlen: int = 10000, ttl_seconds: int = 60):
        self._maxlen = maxlen
        self._ttl = ttl_seconds
        # nonce -> unix_ts_when_inserted
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_remember(self, nonce: str) -> bool:
        """Return True if first time seen; False if replay."""
        with self._lock:
            now = time.time()
            # Drop expired entries lazily on access (cheap because OrderedDict
            # popitem(last=False) is O(1)).
            while self._entries:
                _oldest_nonce, inserted_at = next(iter(self._entries.items()))
                if now - inserted_at <= self._ttl:
                    break
                self._entries.popitem(last=False)
            if nonce in self._entries:
                return False
            # Capacity guard: evict the oldest (front) before inserting.
            while len(self._entries) >= self._maxlen:
                self._entries.popitem(last=False)
            self._entries[nonce] = now
            return True


import sqlite3
from pathlib import Path


class SqliteNonceStore:
    """Cross-process anti-replay store backed by SQLite.

    Same interface as ``NonceCache``: ``check_and_remember(nonce) -> bool``.
    Use this when more than one process can verify grants signed with the
    same inbound secret — e.g. ``uvicorn --workers N`` — so all workers
    share one replay window.

    Implementation notes:
      * One short-lived connection per call. sqlite3 connections are not
        thread-safe by default; opening per-call sidesteps that without a
        connection pool.
      * ``INSERT … ON CONFLICT DO NOTHING`` is the atomic check-then-insert.
        ``cursor.rowcount`` tells us whether the row was new.
      * WAL mode lets concurrent readers proceed during writes; replay
        verification stays low-latency under contention.
      * Expired rows are deleted lazily on each call (cheap; the table only
        holds ~10k rows at steady state) plus a capacity bound.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        maxlen: int = 10000,
        ttl_seconds: int = 60,
    ):
        self._db_path = str(db_path)
        self._maxlen = maxlen
        self._ttl = ttl_seconds
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS nonces ("
                "nonce TEXT PRIMARY KEY, inserted_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inserted_at "
                "ON nonces(inserted_at)"
            )

    def check_and_remember(self, nonce: str) -> bool:
        now = time.time()
        cutoff = now - self._ttl
        with self._connect() as conn:
            # Lazy expiry first; safe to do without a transaction wrapper
            # because SQLite serializes writes.
            conn.execute("DELETE FROM nonces WHERE inserted_at < ?", (cutoff,))
            # Capacity bound: trim oldest rows if we're over.
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM nonces"
            ).fetchone()
            if count >= self._maxlen:
                conn.execute(
                    "DELETE FROM nonces WHERE nonce IN "
                    "(SELECT nonce FROM nonces ORDER BY inserted_at ASC LIMIT ?)",
                    (count - self._maxlen + 1,),
                )
            cur = conn.execute(
                "INSERT OR IGNORE INTO nonces (nonce, inserted_at) VALUES (?, ?)",
                (nonce, now),
            )
            return cur.rowcount == 1
