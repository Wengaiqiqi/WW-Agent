from dataclasses import dataclass, field
from itertools import cycle
from typing import Any


@dataclass
class _Result:
    content: str

    def _content_str(self) -> str:
        return self.content


class MockChatModel:
    """Deterministic chat model for tests. Cycles through a fixed response list."""

    def __init__(self, responses: list[str]):
        if not responses:
            raise ValueError("responses must be non-empty")
        self._responses = cycle(responses)
        self.call_history: list[Any] = []

    def invoke(self, messages: list[dict]) -> _Result:
        self.call_history.append(messages)
        return _Result(content=next(self._responses))

    @classmethod
    def from_env(cls, env_var: str, default: str = "ok") -> "MockChatModel":
        """Construct a MockChatModel whose response list is read from an env var.
        The env var's value is split on '||' to yield individual responses."""
        import os
        raw = os.environ.get(env_var, default)
        return cls(responses=raw.split("||"))
