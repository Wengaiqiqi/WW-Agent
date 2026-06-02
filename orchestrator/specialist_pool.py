"""A process-wide cache of bootstrapped specialist hosts, reused across turns.

A specialist subprocess bakes its LLM + memory + workspace from the env handed
to it at spawn, so two turns can share a warm host iff their spawn signatures
match: ``(user_id, workspace_root, model_id, base_url, api_key, protocol)``.
``permission_mode`` rides per-call in the authz (hmac) grant and is excluded
from the key; ``runtime_dir`` and ``hmac_key`` are assigned once per pooled host.

LOAD-BEARING: an ``MCPHost`` holds asyncio stdio transports bound to the loop it
was created on. This pool must therefore be created on, and only ever driven
from, a single persistent event loop (see ``web.turn_loop.TurnLoop``). All the
public coroutines assume they run on that loop; the internal ``asyncio.Lock``
makes ``acquire``/``release``/``sweep``/``drain`` mutually consistent.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from orchestrator.turn_context import TurnContext

log = logging.getLogger(__name__)

Signature = tuple[str, str, str, str, str, str]

# factory(signature, runtime_dir, hmac_key) -> (host, router)
HostFactory = Callable[..., Awaitable[tuple[Any, Any]]]


def pool_signature(ctx: TurnContext) -> Signature:
    """The spawn-env signature that two turns must share to reuse one host."""
    return (
        ctx.user_id,
        str(ctx.workspace_root),
        ctx.model_id,
        ctx.base_url,
        ctx.api_key,
        ctx.protocol,
    )


@dataclass
class _Entry:
    host: Any
    router: Any
    hmac_key: str
    signature: Signature
    runtime_dir: Path
    leased: bool = False
    last_used: float = 0.0


@dataclass
class Lease:
    """A host leased to exactly one turn. The turn dispatches with ``hmac_key``
    (the host's baked key) and uses ``router`` for capability routing."""
    host: Any
    router: Any
    hmac_key: str
    _entry: _Entry = field(repr=False)


class SpecialistPool:
    def __init__(
        self,
        *,
        factory: HostFactory,
        max_hosts: int = 8,
        idle_ttl: float = 600.0,
        runtime_root: Path | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self._factory = factory
        self._max_hosts = max(1, max_hosts)
        self._idle_ttl = idle_ttl
        self._runtime_root = runtime_root or Path(".agent") / "runtime"
        self._now = now
        self._entries: list[_Entry] = []           # all live (idle + leased)
        self._lock = asyncio.Lock()
        self._slot_freed = asyncio.Condition(self._lock)
        # Strong refs to in-flight eviction shutdowns — asyncio only weakly
        # references tasks, so without this a GC pass could cancel an eviction
        # mid-flight and orphan the host's specialist subprocesses.
        self._evicting: set[asyncio.Task] = set()
        # Lightweight counters so the warm-pool win is measurable before
        # flipping WEB_POOL_ENABLED on in prod (see stats()).
        self._stats: dict[str, float] = {
            "acquires": 0, "hits": 0, "cold_spawns": 0,
            "evictions": 0, "sweeps": 0, "spawn_seconds_total": 0.0,
        }

    async def acquire(self, ctx: TurnContext) -> Lease:
        sig = pool_signature(ctx)
        async with self._lock:
            self._stats["acquires"] += 1
            while True:
                idle_match = next(
                    (e for e in self._entries
                     if not e.leased and e.signature == sig), None,
                )
                if idle_match is not None:
                    idle_match.leased = True
                    self._stats["hits"] += 1
                    return self._lease(idle_match)

                if len(self._entries) < self._max_hosts:
                    break  # room to cold-spawn below
                # At cap: evict the oldest idle host of ANY signature.
                if not self._evict_one_idle():
                    # All hosts are leased — wait for a release.
                    await self._slot_freed.wait()
                    continue
            # Reserve the slot before the await so concurrent acquires see it.
            entry = _Entry(host=None, router=None, hmac_key="",
                           signature=sig, runtime_dir=Path(), leased=True)
            self._entries.append(entry)

        # Cold spawn OUTSIDE the lock (it's the ~7s path; don't block the pool).
        t0 = self._now()
        try:
            hmac_key = secrets.token_urlsafe(32)
            runtime_dir = self._runtime_root / f"pool-{secrets.token_hex(8)}"
            host, router = await self._factory(
                signature=sig, runtime_dir=runtime_dir, hmac_key=hmac_key,
            )
        except BaseException:
            async with self._lock:
                self._entries.remove(entry)
                self._slot_freed.notify()
            raise
        elapsed = max(0.0, self._now() - t0)
        async with self._lock:
            entry.host, entry.router = host, router
            entry.hmac_key, entry.runtime_dir = hmac_key, runtime_dir
            self._stats["cold_spawns"] += 1
            self._stats["spawn_seconds_total"] += elapsed
            log.debug(
                "pool: cold-spawned host for %s in %.1fs (live=%d, hit_rate=%.0f%%)",
                sig, elapsed, len(self._entries),
                100 * self._stats["hits"] / max(1, self._stats["acquires"]),
            )
            return self._lease(entry)

    async def release(self, lease: Lease) -> None:
        async with self._lock:
            entry = lease._entry
            entry.leased = False
            entry.last_used = self._now()
            self._slot_freed.notify()

    async def sweep(self) -> None:
        """Shut down idle hosts past the idle TTL. Run periodically."""
        async with self._lock:
            cutoff = self._now() - self._idle_ttl
            stale = [e for e in self._entries
                     if not e.leased and e.last_used <= cutoff]
            for e in stale:
                self._entries.remove(e)
            self._stats["sweeps"] += len(stale)
            self._slot_freed.notify_all()
        for e in stale:
            await self._shutdown(e)

    async def drain(self) -> None:
        """Shut down every host (idle and leased). Called at server shutdown."""
        async with self._lock:
            entries = list(self._entries)
            self._entries.clear()
            self._slot_freed.notify_all()
        for e in entries:
            await self._shutdown(e)

    def stats(self) -> dict[str, float]:
        """A snapshot of pool counters for logging/observability. ``hit_rate``
        (reused / acquired) and ``avg_spawn_seconds`` are the two numbers worth
        watching to decide whether to raise WEB_POOL_ENABLED in production."""
        s = dict(self._stats)
        s["hit_rate"] = (s["hits"] / s["acquires"]) if s["acquires"] else 0.0
        s["avg_spawn_seconds"] = (
            s["spawn_seconds_total"] / s["cold_spawns"] if s["cold_spawns"] else 0.0
        )
        s["live_hosts"] = len(self._entries)
        return s

    # ---- internals (call only while holding self._lock, except _shutdown) ----

    def _lease(self, entry: _Entry) -> Lease:
        return Lease(host=entry.host, router=entry.router,
                     hmac_key=entry.hmac_key, _entry=entry)

    def _evict_one_idle(self) -> bool:
        idle = [e for e in self._entries if not e.leased]
        if not idle:
            return False
        victim = min(idle, key=lambda e: e.last_used)
        self._entries.remove(victim)
        self._stats["evictions"] += 1
        # Schedule shutdown without awaiting under the lock. Hold a reference
        # (see self._evicting) until it completes so it can't be GC'd mid-flight.
        task = asyncio.create_task(self._shutdown(victim))
        self._evicting.add(task)
        task.add_done_callback(self._evicting.discard)
        return True

    async def _shutdown(self, entry: _Entry) -> None:
        try:
            if entry.host is not None:
                await entry.host.shutdown_all()
        except Exception:  # noqa: BLE001
            log.warning("pool: host shutdown failed", exc_info=True)
        finally:
            # Reap this host's per-host runtime-discovery dir (peers.json etc).
            # Unlike the per-turn path, pooled hosts outlive a single turn, so
            # their dirs would otherwise accumulate under .agent/runtime. Guard
            # the reserved-slot sentinel (Path() == ".") so a drain that races an
            # in-flight cold-spawn can never rmtree the current directory.
            rt = entry.runtime_dir
            if rt and str(rt) not in (".", ""):
                shutil.rmtree(rt, ignore_errors=True)
