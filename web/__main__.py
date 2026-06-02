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
    # Single source of truth for "safe to expose" lives in web.config.
    from web import config

    try:
        config.assert_safe_for_exposure(args.host)
    except config.UnsafeExposureError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import uvicorn

    from web.app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
