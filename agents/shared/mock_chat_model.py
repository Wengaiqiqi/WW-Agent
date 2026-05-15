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
