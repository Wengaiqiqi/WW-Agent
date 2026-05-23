"""Wrong HMAC → server returns 401, client raises A2AClientError('auth refused')."""
from __future__ import annotations

import pytest

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_wrong_hmac_yields_auth_refused(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="server-secret") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="WRONG-SECRET", my_peer_id="caller",
            retry_backoff=(0.0,),
        )
        with pytest.raises(A2AClientError, match="auth refused"):
            await client.call(method="ping", params={}, skill="ping")
