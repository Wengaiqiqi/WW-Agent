"""Shared fixtures: ephemeral self-signed TLS certs + a Mock A2A peer."""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
import trustme
from cryptography.hazmat.primitives import hashes
from cryptography.x509 import load_pem_x509_certificate


@pytest.fixture
def tls_ca() -> trustme.CA:
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
class MockA2APeer:
    """Minimal in-process A2A peer for E2E tests. Filled in by Task 12."""
    port: int
    hmac_secret: str
    my_peer_id: str
    fingerprint_sha256: str
