"""X (Twitter) search via xAI's hosted ``x_search`` Responses API tool.

Ported from ``hermes-agent/tools/x_search_tool.py``. The hermes original
supports both ``XAI_API_KEY`` and SuperGrok OAuth; this port keeps only the
API-key path (OAuth would require pulling in the whole hermes auth stack).

Env vars:

* ``XAI_API_KEY`` — required.
* ``XAI_BASE_URL`` — optional override, default ``https://api.x.ai/v1``.
* ``XAI_X_SEARCH_MODEL`` — optional, default ``grok-4.20-reasoning``.
* ``XAI_X_SEARCH_TIMEOUT`` — seconds, default 180.
* ``XAI_X_SEARCH_RETRIES`` — default 2.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.20-reasoning"
DEFAULT_TIMEOUT = 180
DEFAULT_RETRIES = 2
MAX_HANDLES = 10


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_credentials() -> tuple[str, str]:
    api_key = (os.getenv("XAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "XAI_API_KEY is not set; set it in your environment to use x_search."
        )
    base_url = (os.getenv("XAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return api_key, base_url


def _normalize_handles(handles: Optional[list[str]], field_name: str) -> list[str]:
    cleaned: list[str] = []
    for handle in handles or []:
        normalized = str(handle or "").strip().lstrip("@")
        if normalized:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise ValueError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text"):
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            for annotation in content.get("annotations", []) or []:
                if annotation.get("type") != "url_citation":
                    continue
                citations.append(
                    {
                        "url": annotation.get("url", ""),
                        "title": annotation.get("title", ""),
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def x_search(
    query: str,
    allowed_x_handles: Optional[list[str]] = None,
    excluded_x_handles: Optional[list[str]] = None,
    from_date: str = "",
    to_date: str = "",
    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
) -> dict[str, Any]:
    """Search X (Twitter) via xAI's ``x_search`` Responses API tool."""
    if not query or not query.strip():
        raise ValueError("query is required for x_search")

    api_key, base_url = _resolve_credentials()
    model = (os.getenv("XAI_X_SEARCH_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    timeout = max(30, _env_int("XAI_X_SEARCH_TIMEOUT", DEFAULT_TIMEOUT))
    retries = max(0, _env_int("XAI_X_SEARCH_RETRIES", DEFAULT_RETRIES))

    allowed = _normalize_handles(allowed_x_handles, "allowed_x_handles")
    excluded = _normalize_handles(excluded_x_handles, "excluded_x_handles")
    if allowed and excluded:
        raise ValueError("allowed_x_handles and excluded_x_handles cannot be used together")

    tool_def: dict[str, Any] = {"type": "x_search"}
    if allowed:
        tool_def["allowed_x_handles"] = allowed
    if excluded:
        tool_def["excluded_x_handles"] = excluded
    if from_date.strip():
        tool_def["from_date"] = from_date.strip()
    if to_date.strip():
        tool_def["to_date"] = to_date.strip()
    if enable_image_understanding:
        tool_def["enable_image_understanding"] = True
    if enable_video_understanding:
        tool_def["enable_video_understanding"] = True

    payload = {
        "model": model,
        "input": [{"role": "user", "content": query.strip()}],
        "tools": [tool_def],
        "store": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "agent-x-search/1.0",
    }

    last_exc: Optional[Exception] = None
    data: Optional[dict[str, Any]] = None
    for attempt in range(retries + 1):
        try:
            data = _post_json(f"{base_url}/responses", headers, payload, timeout)
            break
        except urllib.error.HTTPError as e:
            status = e.code
            if status < 500 or attempt >= retries:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                raise RuntimeError(f"x_search HTTP {status}: {err_body or e.reason}")
            last_exc = e
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt >= retries:
                raise RuntimeError(f"x_search transport error: {e}")
            last_exc = e
            time.sleep(min(5.0, 1.5 * (attempt + 1)))

    if data is None:
        raise RuntimeError(f"x_search did not return a response: {last_exc}")

    return {
        "success": True,
        "provider": "xai",
        "tool": "x_search",
        "model": model,
        "query": query.strip(),
        "answer": _extract_response_text(data),
        "citations": list(data.get("citations") or []),
        "inline_citations": _extract_inline_citations(data),
    }
