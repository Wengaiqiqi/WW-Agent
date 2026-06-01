"""Shared fixtures: ephemeral self-signed TLS certs + a Mock A2A peer."""
from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.x509 import load_pem_x509_certificate

# ``trustme`` is a *dev-only* dependency (declared in pyproject ``[dev]``).
# Import it lazily inside the fixtures via ``importorskip`` rather than at
# module top level: a bare top-level ``import trustme`` here raises during
# conftest collection, which pytest treats as a FATAL error that aborts the
# ENTIRE session (every test, not just this directory). Skipping inside the
# fixture degrades gracefully — the comm-agent TLS tests skip, the rest run.

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card


@pytest.fixture
def tls_ca() -> "trustme.CA":
    trustme = pytest.importorskip(
        "trustme", reason="comm-agent TLS tests need trustme (pip install -e '.[dev]')"
    )
    return trustme.CA()


@pytest.fixture
def tls_cert(tls_ca: trustme.CA):
    return tls_ca.issue_cert("127.0.0.1", "localhost")


def cert_fingerprint_sha256(cert) -> str:
    """Return lowercase hex SHA-256 fingerprint of the leaf cert."""
    pem_bytes = cert.cert_chain_pems[0].bytes()
    cert_obj = load_pem_x509_certificate(pem_bytes)
    return cert_obj.fingerprint(hashes.SHA256()).hex()


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class _RunningPeer:
    port: int
    base_url: str
    hmac_secret: str
    my_peer_id: str
    fingerprint_sha256: str
    server: uvicorn.Server
    thread: threading.Thread


async def _default_sync(skill: str, params: dict, claims: dict) -> dict:
    return {"echo": params, "skill": skill}


async def _default_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    yield {"type": "task", "state": "working"}
    yield {"type": "task", "state": "completed", "result": "done"}


@contextlib.asynccontextmanager
async def running_peer(
    tls_ca: trustme.CA,
    *,
    my_peer_id: str = "remote",
    hmac_secret: str = "shared",
    sync_dispatcher=_default_sync,
    stream_dispatcher=_default_stream,
):
    """Spin up a real HTTPS uvicorn serving build_app(), yield connection info."""
    cert = tls_ca.issue_cert("127.0.0.1", "localhost")
    fp = cert_fingerprint_sha256(cert)
    port = pick_free_port()
    self_card = build_self_card(
        name=my_peer_id, description="test peer",
        public_url=f"https://127.0.0.1:{port}", version="1.0.0",
    )
    app = build_app(
        self_card=self_card, hmac_secret=hmac_secret, my_peer_id=my_peer_id,
        skill_dispatcher=sync_dispatcher, stream_dispatcher=stream_dispatcher,
    )

    # Write cert + key to temp files for uvicorn.
    import tempfile
    cert_pem = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_pem = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    for blob in cert.cert_chain_pems:
        cert_pem.write(blob.bytes())
    cert_pem.close()
    key_pem.write(cert.private_key_pem.bytes())
    key_pem.close()

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        ssl_certfile=cert_pem.name, ssl_keyfile=key_pem.name,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-peer", daemon=True)
    thread.start()
    # Wait for readiness.
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)

    try:
        yield _RunningPeer(
            port=port,
            base_url=f"https://127.0.0.1:{port}",
            hmac_secret=hmac_secret,
            my_peer_id=my_peer_id,
            fingerprint_sha256=fp,
            server=server,
            thread=thread,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
