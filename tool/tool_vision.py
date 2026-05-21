"""Vision analysis tool.

Adapted from ``hermes-agent/tools/vision_tools.py``. Sends an image (URL or
local path) together with a user prompt to a vision-capable chat model and
returns the model's text. Uses your project's ``config.build_llm`` so the
same active LLM config governs both regular agent calls and vision calls.

Override the vision model via the ``AGENT_VISION_MODEL`` env var if your
default LLM is not vision-capable. The override only changes the model name;
base_url / api_key still come from your active config.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _download_timeout() -> float:
    """Resolved at call time so a test / runtime ``monkeypatch.setenv`` takes
    effect without re-importing the module."""
    try:
        return float(os.getenv("AGENT_VISION_DOWNLOAD_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _validate_image_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False
    if not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    return bool(parsed.netloc)


def _detect_mime(data: bytes, fallback_suffix: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if fallback_suffix:
        guessed, _ = mimetypes.guess_type(f"x{fallback_suffix}")
        if guessed and guessed.startswith("image/"):
            return guessed
    return "image/jpeg"


def _load_image_as_data_url(source: str) -> str:
    """Return ``data:<mime>;base64,...`` for a URL or local path.

    Remote URLs are SSRF-checked via ``tool_web.hostname_is_safe`` and follow
    the same ``SafeRedirectHandler``-protected opener used elsewhere, so a
    prompt-injected ``http://169.254.169.254/...`` (cloud metadata) cannot
    sneak its bytes into the LLM payload through this tool.
    """
    if source.startswith(("http://", "https://")):
        if not _validate_image_url(source):
            raise ValueError(f"Invalid image URL: {source!r}")
        # Import lazily so a missing ``tool_web`` (extracted) wouldn't break
        # ``vision_analyze`` for local file inputs.
        from tool.tool_web import hostname_is_safe, OPENER

        parsed = urlparse(source)
        allowed, reason = hostname_is_safe(parsed.hostname or "")
        if not allowed:
            raise ValueError(
                f"Refused: {reason}. Set LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1 "
                "to opt out (development only)."
            )

        req = urllib.request.Request(
            source,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; agent-vision/1.0)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with OPENER.open(req, timeout=_download_timeout()) as resp:
            data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Image too large (> {_MAX_DOWNLOAD_BYTES} bytes)")
        mime = _detect_mime(data, fallback_suffix=Path(parsed.path).suffix)
    else:
        # Run the local path through the workspace-boundary check so a
        # prompt-injected ``vision_analyze image="C:\Users\xxx\.ssh\id_rsa"``
        # can't exfiltrate arbitrary files by base64-encoding them into an
        # LLM payload. Matches the policy enforced by the file_ops wrappers.
        # ``LANGCHAIN_AGENT_WORKSPACE_ROOT`` widens the sandbox for tests
        # and operators who legitimately need to read images outside cwd.
        from tool.tool_file_ops import resolve_workspace_path

        path = resolve_workspace_path(str(Path(source).expanduser()))
        if not path.is_file():
            raise FileNotFoundError(f"Image not found: {source}")
        if path.stat().st_size > _MAX_DOWNLOAD_BYTES:
            raise ValueError(f"Image too large (> {_MAX_DOWNLOAD_BYTES} bytes)")
        data = path.read_bytes()
        mime = _detect_mime(data, fallback_suffix=path.suffix)

    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _build_vision_llm():
    """Build a LangChain chat model honoring an optional ``AGENT_VISION_MODEL`` override."""
    try:
        from config import build_llm  # type: ignore
        from config._settings import load_active_config  # type: ignore
    except Exception as exc:  # pragma: no cover - misconfigured project
        raise RuntimeError(f"vision tool needs config.build_llm: {exc}") from exc

    cfg = load_active_config()
    override = os.getenv("AGENT_VISION_MODEL", "").strip()
    if override:
        try:
            cfg.model = override  # type: ignore[attr-defined]
        except Exception:
            pass
    return build_llm(cfg)


def vision_analyze(
    image: str,
    prompt: str = "Describe this image in detail.",
    *,
    max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """Send an image + prompt to a vision-capable chat model and return the text."""
    if not image:
        raise ValueError("image is required")
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    data_url = _load_image_as_data_url(image)
    llm = _build_vision_llm()

    from langchain_core.messages import HumanMessage  # local import to keep cold-start light

    content = [
        {"type": "text", "text": prompt.strip()},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    invoke_kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        invoke_kwargs["max_tokens"] = max_tokens

    response = llm.invoke([HumanMessage(content=content)], **invoke_kwargs)
    text = ""
    if hasattr(response, "content"):
        raw = response.content
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, list):
            parts = []
            for chunk in raw:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    parts.append(chunk.get("text", ""))
                elif isinstance(chunk, str):
                    parts.append(chunk)
            text = "\n".join(p for p in parts if p)
    return {
        "image": image,
        "prompt": prompt.strip(),
        "model": getattr(llm, "model_name", None) or getattr(llm, "model", None),
        "text": text.strip(),
    }
