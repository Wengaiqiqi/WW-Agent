from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_conversation_history_load_is_chunked_and_cancelable():
    app_js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "renderMessagesChunked" in app_js
    assert "state.loadToken" in app_js
    assert "await nextFrame()" in app_js
