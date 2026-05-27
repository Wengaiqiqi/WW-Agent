"""In-memory per-user token bucket. Process-local — fine because the web
process serialises turns on the shared concurrency guard anyway; this is a
fairness/abuse guardrail, not a distributed quota."""
from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        capacity: int,
        refill_per_sec: float,
        now: Callable[[], float] = time.monotonic,
    ):
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._now = now
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._now()
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True


def default_limiter() -> RateLimiter:
    from web import config

    per_min = config.rate_limit_per_min()
    return RateLimiter(capacity=per_min, refill_per_sec=per_min / 60.0)
