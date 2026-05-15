from __future__ import annotations
from dataclasses import dataclass


class RoutingError(Exception):
    pass


@dataclass
class _Entry:
    agent_id: str
    priority: int


class CapabilityRouter:
    """Maps capability name → owning specialist. Higher priority wins ties."""

    def __init__(self):
        self._table: dict[str, list[_Entry]] = {}

    def register(self, agent_id: str, capabilities: list[str], *, priority: int = 0):
        for cap in capabilities:
            self._table.setdefault(cap, []).append(_Entry(agent_id, priority))
            self._table[cap].sort(key=lambda e: -e.priority)

    def resolve(self, capability: str) -> str:
        entries = self._table.get(capability)
        if not entries:
            raise RoutingError(f"unknown capability: {capability}")
        return entries[0].agent_id

    def all_capabilities(self) -> list[str]:
        return sorted(self._table.keys())
