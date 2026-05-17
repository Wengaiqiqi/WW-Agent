"""Mixture-of-Agents (MoA).

Ported from ``hermes-agent/tools/mixture_of_agents_tool.py`` and rewired to
use your project's ``config.build_llm`` instead of a direct OpenRouter
client. Reference responses run in parallel via ``ThreadPoolExecutor``.

The aggregator stage and the reference stage both call ``build_llm`` with an
overridden model name (if provided). All requests share the active config's
base_url / api_key, so the user's provider must support every model in the
list — OpenRouter / Anthropic / OpenAI work well here; a single-model local
endpoint will fall back to sampling the same model multiple times.

Source paper: arXiv:2406.04692.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_REFERENCE_MODELS = (
    "anthropic/claude-opus-4.6",
    "google/gemini-2.5-pro",
    "openai/gpt-5.4-pro",
    "deepseek/deepseek-v3.2",
)
DEFAULT_AGGREGATOR_MODEL = "anthropic/claude-opus-4.6"
REFERENCE_TEMPERATURE = 0.6
AGGREGATOR_TEMPERATURE = 0.4
MIN_SUCCESSFUL_REFERENCES = 1

AGGREGATOR_SYSTEM_PROMPT = (
    "You have been provided with a set of responses from various models to the "
    "latest user query. Your task is to synthesize these responses into a single, "
    "high-quality response. Critically evaluate the information provided, "
    "recognizing that some of it may be biased or incorrect. Your response should "
    "not simply replicate the given answers but offer a refined, accurate, and "
    "comprehensive reply. Ensure your response is well-structured and coherent.\n\n"
    "Responses from models:"
)


def _build_model_llm(model_name: Optional[str], temperature: float):
    """Construct a ChatModel honoring an optional model-name override."""
    from config import build_llm  # type: ignore
    from config._settings import load_active_config  # type: ignore

    cfg = load_active_config()
    if model_name:
        try:
            cfg.model = model_name  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        cfg.temperature = temperature  # type: ignore[attr-defined]
    except Exception:
        pass
    return build_llm(cfg)


def _extract_text(response) -> str:
    raw = getattr(response, "content", response)
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = []
        for chunk in raw:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                parts.append(chunk.get("text", ""))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "\n".join(p for p in parts if p).strip()
    return str(raw).strip()


def _run_reference(model: str, user_prompt: str, max_retries: int = 3) -> tuple[str, str, bool]:
    """Returns (model, response_or_error, ok)."""
    from langchain_core.messages import HumanMessage

    for attempt in range(max_retries):
        try:
            llm = _build_model_llm(model, REFERENCE_TEMPERATURE)
            response = llm.invoke([HumanMessage(content=user_prompt)])
            text = _extract_text(response)
            if not text:
                if attempt < max_retries - 1:
                    time.sleep(min(2 ** (attempt + 1), 30))
                    continue
                return model, "(empty response)", False
            return model, text, True
        except Exception as exc:
            logger.warning("MoA reference %s attempt %s failed: %s", model, attempt + 1, exc)
            if attempt < max_retries - 1:
                time.sleep(min(2 ** (attempt + 1), 30))
            else:
                return model, f"{type(exc).__name__}: {exc}", False
    return model, "exhausted retries", False


def _run_aggregator(aggregator_model: str, user_prompt: str, references: list[str]) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    enumerated = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(references))
    system_prompt = f"{AGGREGATOR_SYSTEM_PROMPT}\n\n{enumerated}"

    llm = _build_model_llm(aggregator_model, AGGREGATOR_TEMPERATURE)
    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )
    text = _extract_text(response)
    if not text:
        response = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        text = _extract_text(response)
    return text


def mixture_of_agents(
    user_prompt: str,
    reference_models: Optional[list[str]] = None,
    aggregator_model: Optional[str] = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Run MoA over ``user_prompt`` and return the synthesized response."""
    if not user_prompt or not user_prompt.strip():
        raise ValueError("user_prompt is required")

    ref_models = list(reference_models) if reference_models else list(DEFAULT_REFERENCE_MODELS)
    agg_model = (aggregator_model or os.getenv("AGENT_MOA_AGGREGATOR") or DEFAULT_AGGREGATOR_MODEL).strip()
    if not ref_models:
        raise ValueError("at least one reference model is required")

    started = time.time()
    successes: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ref_models)))) as pool:
        futures = {pool.submit(_run_reference, m, user_prompt): m for m in ref_models}
        for fut in as_completed(futures):
            model, body, ok = fut.result()
            if ok:
                successes.append((model, body))
            else:
                failures.append((model, body))

    if len(successes) < MIN_SUCCESSFUL_REFERENCES:
        return {
            "success": False,
            "error": (
                f"Only {len(successes)} reference model(s) succeeded; "
                f"need at least {MIN_SUCCESSFUL_REFERENCES}."
            ),
            "failures": failures,
            "elapsed_seconds": round(time.time() - started, 2),
        }

    final = _run_aggregator(agg_model, user_prompt, [body for _, body in successes])
    return {
        "success": True,
        "response": final,
        "models_used": {
            "reference_models": [m for m, _ in successes],
            "aggregator_model": agg_model,
        },
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2),
    }
