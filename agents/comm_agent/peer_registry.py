"""Read/write the comm-agent peer registry JSON.

Schema version 1. Secrets are NEVER stored in JSON — only env-var names
(``hmac_secret_ref``). Resolving the secret reads ``os.environ`` at call
time so a rotation just requires re-export + restart.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


class PeerRegistryError(Exception):
    pass


@dataclass
class Peer:
    peer_id: str
    display_name: str
    url: str
    hmac_secret_ref: str
    tls_verify: bool
    tls_pinned_sha256: str | None
    added_at: str
    last_seen: str | None


_SCHEMA_VERSION = 1


class PeerRegistry:
    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> dict:
        if not self._path.exists():
            return {"schemaVersion": _SCHEMA_VERSION, "peers": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PeerRegistryError(f"corrupt registry at {self._path}: {exc}") from exc
        if data.get("schemaVersion") != _SCHEMA_VERSION:
            raise PeerRegistryError(
                f"unsupported schemaVersion {data.get('schemaVersion')!r}; expected {_SCHEMA_VERSION}"
            )
        return data

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_peers(self) -> list[Peer]:
        data = self._load()
        return [_peer_from_dict(d) for d in data.get("peers", [])]

    def get(self, peer_id: str) -> Peer | None:
        for p in self.list_peers():
            if p.peer_id == peer_id:
                return p
        return None

    def add(self, peer: Peer) -> None:
        # Spec §3.5: tls.verify=false alone is forbidden. Must have a pin.
        if not peer.tls_verify and not peer.tls_pinned_sha256:
            raise PeerRegistryError(
                "tls_verify=False requires tls_pinned_sha256 (refuse to skip TLS entirely)"
            )
        data = self._load()
        # De-dupe by peer_id (overwrite).
        data["peers"] = [
            d for d in data.get("peers", []) if d.get("peer_id") != peer.peer_id
        ]
        data["peers"].append(asdict(peer))
        self._save(data)

    def remove(self, peer_id: str) -> bool:
        data = self._load()
        before = len(data.get("peers", []))
        data["peers"] = [
            d for d in data.get("peers", []) if d.get("peer_id") != peer_id
        ]
        removed = len(data["peers"]) < before
        if removed:
            self._save(data)
        return removed

    def update_last_seen(self, peer_id: str, iso_ts: str) -> None:
        data = self._load()
        for d in data.get("peers", []):
            if d.get("peer_id") == peer_id:
                d["last_seen"] = iso_ts
                self._save(data)
                return

    def resolve_secret(self, peer: Peer) -> str:
        value = os.environ.get(peer.hmac_secret_ref)
        if not value:
            raise PeerRegistryError(
                f"env var {peer.hmac_secret_ref!r} not set; "
                f"export it before starting comm-agent"
            )
        return value


def _peer_from_dict(d: dict) -> Peer:
    return Peer(
        peer_id=d["peer_id"],
        display_name=d.get("display_name", d["peer_id"]),
        url=d["url"],
        hmac_secret_ref=d["hmac_secret_ref"],
        tls_verify=d.get("tls", {}).get("verify", d.get("tls_verify", True))
            if isinstance(d.get("tls"), dict) else d.get("tls_verify", True),
        tls_pinned_sha256=d.get("tls", {}).get("pinned_sha256", d.get("tls_pinned_sha256"))
            if isinstance(d.get("tls"), dict) else d.get("tls_pinned_sha256"),
        added_at=d.get("added_at", ""),
        last_seen=d.get("last_seen"),
    )
