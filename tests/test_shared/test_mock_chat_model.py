from agents.shared.mock_chat_model import MockChatModel


def test_mock_returns_canned_response():
    model = MockChatModel(responses=["hello"])
    out = model.invoke([{"role": "user", "content": "hi"}])
    assert out.content == "hello"


def test_mock_cycles_through_responses():
    model = MockChatModel(responses=["a", "b"])
    assert model.invoke([])._content_str() == "a"
    assert model.invoke([])._content_str() == "b"
    assert model.invoke([])._content_str() == "a"  # cycles


def test_mock_records_call_history():
    model = MockChatModel(responses=["x"])
    model.invoke([{"role": "user", "content": "ping"}])
    assert model.call_history[0][0]["content"] == "ping"
