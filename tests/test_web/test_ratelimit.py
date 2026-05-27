from __future__ import annotations

from web.ratelimit import RateLimiter


def test_allows_up_to_capacity_then_blocks():
    # capacity 3, no refill within the test window
    rl = RateLimiter(capacity=3, refill_per_sec=0.0, now=lambda: 1000.0)
    assert rl.allow("alice")
    assert rl.allow("alice")
    assert rl.allow("alice")
    assert not rl.allow("alice")  # 4th blocked


def test_per_key_independent():
    rl = RateLimiter(capacity=1, refill_per_sec=0.0, now=lambda: 1000.0)
    assert rl.allow("alice")
    assert not rl.allow("alice")
    assert rl.allow("bob")  # bob has his own bucket


def test_refills_over_time():
    t = {"v": 1000.0}
    rl = RateLimiter(capacity=2, refill_per_sec=1.0, now=lambda: t["v"])
    assert rl.allow("alice")
    assert rl.allow("alice")
    assert not rl.allow("alice")
    t["v"] = 1001.5  # 1.5s -> +1 token (capped at capacity)
    assert rl.allow("alice")
    assert not rl.allow("alice")
