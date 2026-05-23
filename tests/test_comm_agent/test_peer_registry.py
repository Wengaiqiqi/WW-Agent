"""Tests for agents/comm_agent/peer_registry.py."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.comm_agent.peer_registry import (
    Peer, PeerRegistry, PeerRegistryError,
)


def test_empty_registry_on_missing_file(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    assert reg.list_peers() == []


def test_add_and_get_peer(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="openclaw-home",
        display_name="OpenClaw @ home",
        url="https://home.example.com:8443",
        hmac_secret_ref="OPENCLAW_HOME_HMAC",
        tls_verify=True,
        tls_pinned_sha256=None,
        added_at="2026-05-23T10:00:00",
        last_seen=None,
    )
    reg.add(peer)
    got = reg.get("openclaw-home")
    assert got is not None
    assert got.url == "https://home.example.com:8443"


def test_persistence_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "comm_peers.json"
    reg = PeerRegistry(path)
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    # Read back via a fresh instance
    reg2 = PeerRegistry(path)
    assert reg2.get("p") is not None


def test_remove_peer(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    reg.add(Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    ))
    assert reg.remove("p") is True
    assert reg.get("p") is None
    assert reg.remove("p") is False  # idempotent


def test_resolve_secret_reads_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_TEST_HMAC", "supersecret")
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="MY_TEST_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    assert reg.resolve_secret(peer) == "supersecret"


def test_resolve_secret_missing_env_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_HMAC", raising=False)
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="MISSING_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    with pytest.raises(PeerRegistryError, match="env var .*MISSING_HMAC"):
        reg.resolve_secret(peer)


def test_tls_verify_false_without_pin_rejected(tmp_path: Path) -> None:
    """Spec §3.5: tls.verify=false alone is forbidden. Pin required if verify off."""
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    with pytest.raises(PeerRegistryError, match="tls"):
        reg.add(Peer(
            peer_id="p", display_name="P", url="https://p:8443",
            hmac_secret_ref="P_HMAC",
            tls_verify=False, tls_pinned_sha256=None,
            added_at="t", last_seen=None,
        ))


def test_secret_never_in_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("P_HMAC", "supersecretvalue")
    path = tmp_path / "comm_peers.json"
    reg = PeerRegistry(path)
    reg.add(Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    ))
    content = path.read_text(encoding="utf-8")
    assert "supersecretvalue" not in content
    assert "P_HMAC" in content  # the ref, not the value


def test_on_disk_shape_is_nested_tls(tmp_path: Path) -> None:
    """Spec §3.5: tls.{verify,pinned_sha256} must be nested in JSON."""
    path = tmp_path / "comm_peers.json"
    reg = PeerRegistry(path)
    reg.add(Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256="abc123",
        added_at="t", last_seen=None,
    ))
    data = json.loads(path.read_text(encoding="utf-8"))
    peer_dict = data["peers"][0]
    assert "tls" in peer_dict
    assert peer_dict["tls"] == {"verify": True, "pinned_sha256": "abc123"}
    assert "tls_verify" not in peer_dict
    assert "tls_pinned_sha256" not in peer_dict
