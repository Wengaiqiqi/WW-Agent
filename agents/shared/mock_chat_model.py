from dataclasses import dataclass, field
from itertools import cycle
from typing import Any, AsyncIterator


@dataclass
class _Result:
    content: str

    def _content_str(self) -> str:
        return self.content


@dataclass
class _Chunk:
    content: str


class MockChatModel:
    """Deterministic chat model for tests. Cycles through a fixed response list."""

    def __init__(self, responses: list[str], *, chunk_size: int = 8):
        if not responses:
            raise ValueError("responses must be non-empty")
        self._responses = cycle(responses)
        self._chunk_size = max(1, chunk_size)
        self.call_history: list[Any] = []

    def invoke(self, messages: list[dict]) -> _Result:
        self.call_history.append(messages)
        return _Result(content=next(self._responses))

    async def astream(self, messages: list[dict]) -> AsyncIterator[_Chunk]:
        self.call_history.append(messages)
        text = next(self._responses)
        if not text:
            return
        step = self._chunk_size
        for i in range(0, len(text), step):
            yield _Chunk(content=text[i : i + step])

    def bind_tools(self, tools, **_kwargs):
        """Stub bind_tools so langgraph's create_react_agent accepts the model.

        MockChatModel never actually invokes a tool — the scripted responses
        drive the conversation directly. The real ChatOpenAI / ChatAnthropic
        return a new bound copy; we return self because the mock has no
        per-tool state to track."""
        self.bound_tools = list(tools)
        return self

    @classmethod
    def from_env(cls, env_var: str, default: str = "ok") -> "MockChatModel":
        """Construct a MockChatModel whose response list is read from an env var.
        The env var's value is split on '||' to yield individual responses."""
        import os
        raw = os.environ.get(env_var, default)
        return cls(responses=raw.split("||"))
