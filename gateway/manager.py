"""Run gateway adapters as background tasks inside the REPL event loop.

A single :class:`GatewayManager` instance (returned by :func:`get_manager`)
tracks one ``asyncio.Task`` per platform. ``start_*`` creates the task,
``stop`` cancels it, and ``status`` reports its lifecycle state. The REPL's
``/feishu`` / ``/qq`` slash commands drive this manager.

Adapters keep accepting env-var-only configuration when launched via
``python -m gateway``; the manager always passes an explicit cfg loaded from
:mod:`gateway.credentials`, so it never has to mutate ``os.environ``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


# Bind the FastAPI lifespan to "on" so the @app.on_event("startup") handler in
# feishu.build_app runs inside ``Server.serve()``; uvicorn defaults to "auto"
# which can quietly skip lifespan for sub-apps.
_UVICORN_LIFESPAN = "on"


_FILE_HANDLER_INSTALLED = False


def _install_file_logging() -> Path:
    """Route ``gateway.*`` + ``uvicorn`` logs to ``<config_dir>/gateway.log``.

    Idempotent — calling start_* multiple times only attaches one handler.
    The REPL's TUI must not see these logs (they'd shred the Rich layout),
    so a file is the only sane sink. Users can ``tail -f`` it from another
    terminal to watch what the gateway is doing.
    """
    global _FILE_HANDLER_INSTALLED

    from agent_paths import config_dir

    path = config_dir() / "gateway.log"
    if _FILE_HANDLER_INSTALLED:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    for name in (
        "gateway",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "lark_oapi",  # Feishu SDK -- captures ws connect / event dispatch
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    _FILE_HANDLER_INSTALLED = True
    return path


@dataclass
class _Slot:
    task: Optional[asyncio.Task] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    server: Any = None  # uvicorn.Server, set for the feishu slot
    # The QQGateway instance, kept so ``stop()`` can call ``request_stop``
    # for cooperative shutdown (since asyncio.to_thread can't interrupt the
    # underlying worker thread). None for feishu (the lark-oapi SDK has no
    # equivalent stop hook).
    gateway: Any = None


class GatewayManager:
    """Process-wide singleton — one task per platform.

    The REPL runs inside a single ``asyncio.run(...)`` call, so all slot
    tasks share that loop. ``stop()`` is non-blocking: it issues a cancel
    and lets the task tear itself down on the next yield.
    """

    def __init__(self) -> None:
        self._slots: Dict[str, _Slot] = {}

    # -- query ------------------------------------------------------------

    def is_running(self, platform: str) -> bool:
        slot = self._slots.get(platform)
        return slot is not None and slot.task is not None and not slot.task.done()

    def status(self, platform: str) -> str:
        slot = self._slots.get(platform)
        if slot is None or slot.task is None:
            return "not started"
        task = slot.task
        if not task.done():
            return "running"
        if task.cancelled():
            return "stopped"
        exc = task.exception()
        if exc is not None:
            return f"crashed: {exc}"
        return "stopped"

    def meta(self, platform: str) -> Dict[str, Any]:
        slot = self._slots.get(platform)
        return dict(slot.meta) if slot is not None else {}

    # -- start ------------------------------------------------------------

    def start_feishu(
        self,
        cfg: Dict[str, Any],
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> str:
        if self.is_running("feishu"):
            mode = self._slots["feishu"].meta.get("mode", "?")
            return f"already running (mode={mode})"

        mode = (cfg or {}).get("mode") or "ws"
        if mode == "ws":
            return self._start_feishu_ws(cfg)
        return self._start_feishu_webhook(cfg, host=host, port=port)

    def _start_feishu_ws(self, cfg: Dict[str, Any]) -> str:
        from gateway._pidlock import acquire, AlreadyRunning
        from gateway.feishu_ws import _coerce, serve

        resolved = _coerce(cfg)
        log_path = _install_file_logging()
        try:
            acquire("feishu")
        except AlreadyRunning as exc:
            raise RuntimeError(str(exc))

        async def _serve() -> None:
            # lark-oapi's ws_client.start() is blocking and creates its own
            # loop. Run it in a worker thread; cancellation of the asyncio
            # task signals the thread to stop, but the SDK doesn't expose a
            # clean stop hook -- in practice the user kills the REPL or
            # restarts the gateway, which terminates the thread on process
            # exit.
            await asyncio.to_thread(serve, resolved)

        task = asyncio.get_event_loop().create_task(_serve())
        self._slots["feishu"] = _Slot(
            task=task,
            meta={
                "mode": "ws",
                "app_id": resolved["app_id"],
                "domain": resolved["domain"],
                "log": str(log_path),
            },
        )
        task.add_done_callback(lambda t, p="feishu": self._on_done(p, t))
        return f"started (long-connection)\nlogs -> {log_path}"

    def _start_feishu_webhook(
        self, cfg: Dict[str, Any], *, host: str, port: int
    ) -> str:
        import uvicorn

        from gateway.feishu import build_app

        app = build_app(cfg)
        uvicorn_cfg = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            lifespan=_UVICORN_LIFESPAN,
        )
        server = uvicorn.Server(uvicorn_cfg)

        async def _serve() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                server.should_exit = True
                raise

        log_path = _install_file_logging()
        task = asyncio.get_event_loop().create_task(_serve())
        self._slots["feishu"] = _Slot(
            task=task,
            server=server,
            meta={
                "mode": "webhook",
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}/feishu/webhook",
                "log": str(log_path),
            },
        )
        task.add_done_callback(lambda t, p="feishu": self._on_done(p, t))
        return (
            f"started on http://{host}:{port}/feishu/webhook\n"
            f"logs -> {log_path}"
        )

    def start_qq(self, cfg: Dict[str, Any]) -> str:
        if self.is_running("qq"):
            return "already running"

        from gateway.qq import QQGateway

        gateway = QQGateway(cfg)

        async def _serve() -> None:
            # Run the entire QQ gateway in a worker thread with its OWN
            # asyncio loop. This matches Feishu's pattern (lark-oapi's
            # ws_client.start internally creates its own loop on its own
            # thread). Without this, the WS read + reply POST timers live
            # on the REPL's main event loop, where they can be starved by
            # prompt_toolkit's picker UI -- the standalone path works
            # because there's no picker competing for the loop. The cost
            # is a single dedicated thread per gateway; chat-bot QPS
            # makes that negligible.
            log.info("gateway[qq] [v3-isolated] serve task entered")

            def _run_in_isolated_loop() -> None:
                log.info("gateway[qq] [v3-isolated] worker thread started, creating loop")
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    log.info("gateway[qq] [v3-isolated] loop=%s policy=%s",
                             type(loop).__name__,
                             type(asyncio.get_event_loop_policy()).__name__)
                    loop.run_until_complete(gateway.run())
                except asyncio.CancelledError:
                    log.info("gateway[qq] [v3-isolated] inner loop cancelled")
                except Exception:  # noqa: BLE001
                    log.exception("gateway[qq] [v3-isolated] inner loop crashed")
                finally:
                    log.info("gateway[qq] [v3-isolated] worker thread closing loop")
                    try:
                        loop.close()
                    except Exception:  # noqa: BLE001
                        pass
                    log.info("gateway[qq] [v3-isolated] worker thread exit")

            try:
                await asyncio.to_thread(_run_in_isolated_loop)
            except asyncio.CancelledError:
                raise

        log_path = _install_file_logging()
        task = asyncio.get_event_loop().create_task(_serve())
        self._slots["qq"] = _Slot(
            task=task,
            gateway=gateway,
            meta={
                "sandbox": gateway._cfg.get("sandbox", False),
                "log": str(log_path),
            },
        )
        task.add_done_callback(lambda t, p="qq": self._on_done(p, t))
        return f"started (WebSocket gateway)\nlogs -> {log_path}"

    # -- stop -------------------------------------------------------------

    def stop(self, platform: str) -> str:
        slot = self._slots.get(platform)
        if slot is None or slot.task is None or slot.task.done():
            # Still release the lock in case a previous start left a stale
            # lock behind (e.g. the SDK thread won't actually exit but the
            # user wants to re-acquire the slot from this same REPL).
            self._release_lock(platform)
            self._slots.pop(platform, None)
            return "not running"
        # Feishu: ask uvicorn to drain cleanly so in-flight webhooks return a
        # response.
        if platform == "feishu" and slot.server is not None:
            slot.server.should_exit = True
        # QQ: signal the in-thread gateway to break out of its WS read +
        # reconnect loop and exit cleanly. Without this the worker thread
        # keeps a live WS connection (because asyncio.to_thread can't
        # cancel an already-running thread) and a subsequent Start would
        # cause two clients to receive the same events.
        if platform == "qq" and slot.gateway is not None:
            try:
                slot.gateway.request_stop()
            except Exception:  # noqa: BLE001
                log.exception("qq: request_stop raised")
        slot.task.cancel()
        self._release_lock(platform)
        # Drop the slot immediately so ``is_running`` returns False on the
        # next check (the menu uses that to choose Start vs Stop labels).
        # The task itself may take another loop tick to actually finish,
        # but for UX the user should see "stopped" right away.
        del self._slots[platform]
        return "stop signal sent"

    async def shutdown_all(self) -> None:
        """Cancel every running slot and await its teardown. Idempotent."""
        for platform, slot in list(self._slots.items()):
            if slot.task is None or slot.task.done():
                continue
            if platform == "feishu" and slot.server is not None:
                slot.server.should_exit = True
            if platform == "qq" and slot.gateway is not None:
                try:
                    slot.gateway.request_stop()
                except Exception:  # noqa: BLE001
                    pass
            slot.task.cancel()
            try:
                await asyncio.wait_for(slot.task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self._release_lock(platform)

    @staticmethod
    def _release_lock(platform: str) -> None:
        """Best-effort PID lock cleanup. Tolerates the lock being stale or
        missing; we just want to make sure the next ``start_*`` from this
        REPL doesn't trip on the AlreadyRunning check.

        Trade-off: if the SDK's background thread is somehow still alive,
        releasing here lets a parallel ``python -m gateway feishu`` start
        too -- but the SDK thread has no way to deliver messages anyway
        (the user already asked it to stop), so dual-running is benign.
        """
        try:
            from gateway._pidlock import release as _release

            _release(platform)
        except Exception:  # noqa: BLE001
            pass

    # -- internal ---------------------------------------------------------

    def _on_done(self, platform: str, task: asyncio.Task) -> None:
        if task.cancelled():
            log.info("gateway[%s] stopped", platform)
            return
        exc = task.exception()
        if exc is not None:
            log.error("gateway[%s] crashed: %s", platform, exc)
        else:
            log.info("gateway[%s] exited cleanly", platform)


_MANAGER: Optional[GatewayManager] = None


def get_manager() -> GatewayManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = GatewayManager()
    return _MANAGER
