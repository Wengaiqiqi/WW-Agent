import io
from orchestrator.main import _handle_slash_agents
from orchestrator.registry import Card


class _FakeHandle:
    def __init__(self, card_id, version, a2a_url):
        self.card = Card(
            id=card_id, display_name="X", version=version,
            entrypoint={}, mcp={}, a2a={}, capabilities_hint=[], model_override=None,
        )
        self.a2a_url = a2a_url


class _FakeHost:
    def __init__(self):
        self._h = [_FakeHandle("tool-agent", "1.0.0", "http://127.0.0.1:50001")]

    def list_handles(self):
        return list(self._h)


def test_slash_agents_renders_table():
    buf = io.StringIO()
    _handle_slash_agents(_FakeHost(), out=buf)
    text = buf.getvalue()
    assert "tool-agent" in text
    assert "50001" in text
