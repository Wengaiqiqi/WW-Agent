"""Run the web UI server from the command line.

Examples::

    python -m web
    python -m web --host 127.0.0.1 --port 9000

Host/port default from the environment (``WEB_HOST`` / ``WEB_PORT``) and may be
overridden on the command line. Other knobs (auth secret, signup code, rate
limit, cookie security) are read from the environment by :mod:`web.config`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _is_loopback(host: str) -> bool:
    """True for binds reachable only from the local machine. ``0.0.0.0`` and
    ``::`` (all-interfaces) and any concrete LAN/public address are NOT
    loopback and require secure config before exposure."""
    h = (host or "").strip().strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m web")
    p.add_argument(
        "--host",
        default=os.environ.get("WEB_HOST", "127.0.0.1"),
        help="Bind address (default: $WEB_HOST or 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEB_PORT", "8080")),
        help="Bind port (default: $WEB_PORT or 8080)",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Refuse to expose the server to the network without explicit secure config.
    # On a non-loopback bind, anyone who can reach the port can register an
    # account and drive a workspace-write agent (shell/file/python tools), so a
    # persistent JWT secret AND a registration gate are mandatory. Loopback
    # binds stay zero-config for local dev.
    if not _is_loopback(args.host):
        missing = [
            name
            for name in ("WEB_AUTH_SECRET", "WEB_SIGNUP_CODE")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            print(
                f"Refusing to bind {args.host} (network-exposed) without "
                f"{' and '.join(missing)} set. Set them, or bind 127.0.0.1 for "
                "local-only use.",
                file=sys.stderr,
            )
            return 2

    import uvicorn

    from web.app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
