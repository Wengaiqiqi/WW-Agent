"""Run a gateway adapter from the command line.

Examples::

    python -m gateway feishu --port 8765
    python -m gateway qq

Required env vars per platform are listed in each adapter's module docstring
(:mod:`gateway.feishu`, :mod:`gateway.qq`) and in ``gateway/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# On Windows the default ProactorEventLoop schedules I/O via IOCP; when an
# active WebSocket connection (``websockets`` lib) coexists with httpx HTTP
# requests in the same loop, outbound POSTs to QQ's messaging endpoint hang
# indefinitely (probed standalone, the same endpoint returns in <0.5s).
# SelectorEventLoop sidesteps that interaction. It does not support
# subprocess.exec on Windows, but the gateway adapters do not need it.
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:  # noqa: BLE001 - very old Python without the policy
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m gateway")
    sub = p.add_subparsers(dest="platform", required=True)

    feishu = sub.add_parser("feishu", help="Run the Feishu gateway")
    feishu.add_argument(
        "--mode",
        choices=("ws", "webhook"),
        default=None,
        help="Override the connection mode (default: from gateways.json, falls "
        "back to 'ws' if unset)",
    )
    feishu.add_argument("--host", default="0.0.0.0", help="(webhook mode only)")
    feishu.add_argument("--port", type=int, default=8765, help="(webhook mode only)")

    sub.add_parser("qq", help="Run the QQ Official Bot WebSocket adapter")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    # Prefer credentials saved via the REPL's ``/gateway setup`` wizard. Env
    # vars (QQ_APP_ID etc.) are still honoured as a fallback so headless
    # deployments without a saved config keep working.
    from gateway import credentials as gw_creds

    if args.platform == "feishu":
        cfg = gw_creds.load("feishu") or None
        mode = (
            args.mode
            or (cfg or {}).get("mode")
            or "ws"
        )
        if mode == "ws":
            from gateway.feishu_ws import serve as serve_ws

            serve_ws(cfg=cfg)
            return 0
        from gateway.feishu import serve as serve_webhook

        host = args.host
        port = args.port
        if cfg:
            host = cfg.get("host") or host
            port = int(cfg.get("port") or port)
        serve_webhook(host=host, port=port, cfg=cfg)
        return 0
    if args.platform == "qq":
        from gateway.qq import serve

        cfg = gw_creds.load("qq") or None
        serve(cfg=cfg)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
