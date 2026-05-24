"""Bridge entrypoint.

Run from the agent-last repo root so `agents.*` and `bridge.*` are importable:

    python -m bridge.hermes_a2a

Environment:
  HERMES_A2A_HMAC          (required) shared HMAC secret with the agent-last caller
  HERMES_A2A_MY_PEER_ID    self peer id (default: hermes-home)
  HERMES_A2A_ALLOWED_PEER  optional caller peer_id allowlist (one peer)
  HERMES_A2A_PORT          local HTTP port uvicorn binds (default: 19444)
  HERMES_A2A_PUBLIC_HOST   host for the advertised card url (default: 127.0.0.1)
  HERMES_A2A_PUBLIC_PORT   public port for the card url (default: CADDY_PORT or 8443)
  HERMES_ACP_CMD           command to launch ACP server (default: "hermes acp")
  HERMES_A2A_WORKDIR       cwd for ACP sessions (default: process cwd)
  HERMES_A2A_AUTO_APPROVE  "1" to auto-approve ACP permission requests (default: deny)
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from bridge.hermes_a2a.acp_client import HermesACPClient
from bridge.hermes_a2a.dispatchers import make_dispatchers

log = logging.getLogger(__name__)


def build():
    """Assemble and return the FastAPI app (does not start the ACP subprocess)."""
    hmac_secret = os.environ.get("HERMES_A2A_HMAC", "")
    if not hmac_secret:
        raise SystemExit("HERMES_A2A_HMAC is required (the shared secret with agent-last)")

    my_peer_id = os.environ.get("HERMES_A2A_MY_PEER_ID", "hermes-home")
    allowed_peer = os.environ.get("HERMES_A2A_ALLOWED_PEER") or None
    public_host = os.environ.get("HERMES_A2A_PUBLIC_HOST") or None
    public_port = int(os.environ.get("HERMES_A2A_PUBLIC_PORT",
                                     os.environ.get("CADDY_PORT", "8443")))
    public_url = (f"https://{public_host}:{public_port}" if public_host
                  else f"https://127.0.0.1:{public_port}")

    acp = HermesACPClient(
        command=os.environ.get("HERMES_ACP_CMD", "hermes acp"),
        workdir=os.environ.get("HERMES_A2A_WORKDIR") or None,
        auto_approve=os.environ.get("HERMES_A2A_AUTO_APPROVE") == "1",
    )
    skill_dispatcher, stream_dispatcher = make_dispatchers(acp, allowed_peer=allowed_peer)
    card = build_self_card(
        name=f"hermes-{my_peer_id}",
        description="Hermes via A2A<->ACP bridge",
        public_url=public_url,
        version="1.0.0",
    )
    return build_app(
        self_card=card,
        hmac_secret=hmac_secret,
        my_peer_id=my_peer_id,
        skill_dispatcher=skill_dispatcher,
        stream_dispatcher=stream_dispatcher,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    port = int(os.environ.get("HERMES_A2A_PORT", "19444"))
    uvicorn.run(build(), host="127.0.0.1", port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
