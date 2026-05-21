"""Tests for tool/tool_vision.py.

Covers:
- URL vs. local-path image loading
- MIME detection from magic bytes
- SSRF refusal (private host)
- size limit
- max_tokens passthrough
- env-driven vision-model override
"""
from __future__ import annotations

import base64
import io

import pytest

from tool import tool_vision


@pytest.fixture(autouse=True)
def _widen_workspace(tmp_path, monkeypatch):
    """Most vision tests stash fixtures in ``tmp_path`` and pass absolute
    paths to ``vision_analyze``. Since the wrapper now enforces the
    workspace boundary (security fix — see #37), widen the boundary to
    ``tmp_path`` for the whole file. Tests that need a NARROWER workspace
    (e.g. the explicit out-of-workspace refusal test) set the env var
    again inside the test."""
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a, **kw) -> bytes:
        return self._body


class _FakeOpener:
    def __init__(self, body: bytes):
        self._body = body
        self.last_req = None

    def open(self, req, timeout=None):
        self.last_req = req
        return _FakeResponse(self._body)


class _FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Minimal stand-in for a langchain ChatModel — captures the prompt and
    returns a scripted response."""

    model_name = "fake-vision-model"

    def __init__(self, response_text="A red ball on grass."):
        self.response_text = response_text
        self.invocations = []

    def invoke(self, messages, **kwargs):
        self.invocations.append({"messages": messages, "kwargs": kwargs})
        return _FakeLLMResponse(self.response_text)


# 1×1 transparent PNG header (8 bytes magic + IHDR enough for _detect_mime).
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 50
_GIF_BYTES = b"GIF89a" + b"\x00" * 50


# ---------------------------------------------------------------------------
# MIME detection
# ---------------------------------------------------------------------------


def test_detect_mime_png():
    assert tool_vision._detect_mime(_PNG_BYTES) == "image/png"


def test_detect_mime_jpeg():
    assert tool_vision._detect_mime(_JPEG_BYTES) == "image/jpeg"


def test_detect_mime_gif():
    assert tool_vision._detect_mime(_GIF_BYTES) == "image/gif"


def test_detect_mime_webp():
    body = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
    assert tool_vision._detect_mime(body) == "image/webp"


def test_detect_mime_falls_back_to_suffix_then_jpeg():
    assert tool_vision._detect_mime(b"???unknown", fallback_suffix=".png") == "image/png"
    assert tool_vision._detect_mime(b"???unknown") == "image/jpeg"


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def test_validate_image_url_accepts_https():
    assert tool_vision._validate_image_url("https://example.com/a.png")
    assert tool_vision._validate_image_url("http://example.com/a.png")


def test_validate_image_url_rejects_other_schemes():
    assert not tool_vision._validate_image_url("file:///etc/passwd")
    assert not tool_vision._validate_image_url("data:image/png;base64,abc")
    assert not tool_vision._validate_image_url("")
    assert not tool_vision._validate_image_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Local-file path
# ---------------------------------------------------------------------------


def test_load_image_from_local_file(tmp_path, monkeypatch):
    # ``_load_image_as_data_url`` now enforces the workspace boundary on
    # local paths to prevent prompt-injected exfiltration of ``~/.ssh/id_rsa``
    # & friends via base64-into-LLM-payload. Widen the sandbox to tmp_path
    # so the test fixture is in-scope.
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))
    path = tmp_path / "img.png"
    path.write_bytes(_PNG_BYTES)
    data_url = tool_vision._load_image_as_data_url(str(path))
    assert data_url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(data_url.split(",", 1)[1])
    assert decoded == _PNG_BYTES


def test_load_image_from_missing_local_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        tool_vision._load_image_as_data_url(str(tmp_path / "nope.png"))


def test_load_image_local_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(tool_vision, "_MAX_DOWNLOAD_BYTES", 100)
    path = tmp_path / "big.png"
    path.write_bytes(_PNG_BYTES + b"\x00" * 200)
    with pytest.raises(ValueError, match="too large"):
        tool_vision._load_image_as_data_url(str(path))


def test_load_image_outside_workspace_is_refused(tmp_path, monkeypatch):
    """Regression: vision used to read any local path. After the fix, a
    path outside ``LANGCHAIN_AGENT_WORKSPACE_ROOT`` is refused — preventing
    a prompt-injected ``vision_analyze image="C:\\Users\\you\\.ssh\\id_rsa"``
    from sneaking sensitive bytes into the LLM via base64."""
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(inside))
    with pytest.raises(PermissionError, match="outside workspace"):
        tool_vision._load_image_as_data_url(str(outside))


# ---------------------------------------------------------------------------
# Remote URL path — SSRF check + opener routing
# ---------------------------------------------------------------------------


def test_load_image_from_https_routes_through_opener(monkeypatch):
    from tool import tool_web

    fake = _FakeOpener(_PNG_BYTES)
    monkeypatch.setattr(tool_web, "OPENER", fake)
    # Public hostname → hostname_is_safe should pass; stub the resolver to
    # return a public IP for a deterministic test.
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )

    data_url = tool_vision._load_image_as_data_url("https://example.com/x.png")
    assert data_url.startswith("data:image/png;base64,")
    assert fake.last_req is not None
    assert fake.last_req.full_url == "https://example.com/x.png"


def test_load_image_https_refuses_private_target(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))],
    )
    # No need to patch OPENER — hostname_is_safe blocks before any fetch.
    with pytest.raises(ValueError, match="Refused"):
        tool_vision._load_image_as_data_url("http://attacker.example.com/leak")


def test_load_image_invalid_url_format():
    with pytest.raises(ValueError, match="Invalid image URL"):
        tool_vision._load_image_as_data_url("https://")


# ---------------------------------------------------------------------------
# vision_analyze end-to-end (mocked LLM)
# ---------------------------------------------------------------------------


def test_vision_analyze_happy_path_local_image(tmp_path, monkeypatch):
    path = tmp_path / "x.png"
    path.write_bytes(_PNG_BYTES)
    fake_llm = _FakeLLM(response_text="It is a tiny red dot.")
    monkeypatch.setattr(tool_vision, "_build_vision_llm", lambda: fake_llm)

    out = tool_vision.vision_analyze(str(path), prompt="Describe.")
    assert out["text"] == "It is a tiny red dot."
    assert out["model"] == "fake-vision-model"

    # Check the LLM saw a HumanMessage with both text and a data: URL image.
    sent = fake_llm.invocations[0]["messages"][0].content
    text_blocks = [b for b in sent if b.get("type") == "text"]
    img_blocks = [b for b in sent if b.get("type") == "image_url"]
    assert text_blocks[0]["text"] == "Describe."
    assert img_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_analyze_passes_max_tokens_through(tmp_path, monkeypatch):
    path = tmp_path / "x.png"
    path.write_bytes(_PNG_BYTES)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(tool_vision, "_build_vision_llm", lambda: fake_llm)

    tool_vision.vision_analyze(str(path), prompt="describe", max_tokens=42)
    assert fake_llm.invocations[0]["kwargs"]["max_tokens"] == 42


def test_vision_analyze_requires_image():
    with pytest.raises(ValueError, match="image is required"):
        tool_vision.vision_analyze("")


def test_vision_analyze_requires_prompt(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(_PNG_BYTES)
    with pytest.raises(ValueError, match="prompt is required"):
        tool_vision.vision_analyze(str(path), prompt="   ")


def test_vision_analyze_handles_list_content(tmp_path, monkeypatch):
    """Some chat models (Anthropic native) return ``content`` as a list of
    typed blocks instead of a plain string. The extractor must concatenate
    the text blocks."""
    path = tmp_path / "x.png"
    path.write_bytes(_PNG_BYTES)
    fake_llm = _FakeLLM(response_text=[
        {"type": "text", "text": "part 1"},
        {"type": "text", "text": "part 2"},
    ])
    monkeypatch.setattr(tool_vision, "_build_vision_llm", lambda: fake_llm)

    out = tool_vision.vision_analyze(str(path), prompt="describe")
    assert "part 1" in out["text"]
    assert "part 2" in out["text"]


# ---------------------------------------------------------------------------
# Download timeout (lazy env read)
# ---------------------------------------------------------------------------


def test_download_timeout_default():
    assert tool_vision._download_timeout() == 30.0


def test_download_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_DOWNLOAD_TIMEOUT", "60")
    assert tool_vision._download_timeout() == 60.0


def test_download_timeout_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_VISION_DOWNLOAD_TIMEOUT", "not-a-number")
    assert tool_vision._download_timeout() == 30.0
