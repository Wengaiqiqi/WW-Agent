"""LLM construction — the only module here that imports langchain.

Both ``ReasoningChatOpenAI`` and ``build_llm`` live here so that ``import
config._providers`` (the bulky provider registry) and ``import
config._credentials`` (file I/O for ``hydrate_env_from_credentials``) stay
free of the slow ``langchain_openai`` import — a previous version of this
module ate 6-8 seconds of startup just from the langchain import chain, and
that cost is now confined to ``build_llm`` callers.
"""
from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI

from ._credentials import get_api_key, validate_api_key
from ._providers import ActiveConfig
from ._settings import load_active_config

logger = logging.getLogger(__name__)


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that preserves the ``reasoning_content`` round-trip.

    A number of OpenAI-compatible providers (Xiaomi MiMo, DeepSeek reasoner
    series, Qwen thinking models, etc.) require the same multi-turn protocol:

    - The model emits chain-of-thought in ``reasoning_content`` alongside
      ``content`` / ``tool_calls`` on every assistant turn.
    - Every prior assistant message that had a ``reasoning_content`` MUST be
      echoed back verbatim on the next request. Omitting it triggers a 400
      ("The `reasoning_content` in the thinking mode must be passed back to
      the API.").

    Plain ``ChatOpenAI`` doesn't know about ``reasoning_content``: it neither
    captures it from the response nor sends it back. This subclass:

    - Captures ``reasoning_content`` from non-streamed responses and from
      streaming chunks into ``AIMessage.additional_kwargs``.
    - Injects ``reasoning_content`` into the outgoing payload for every
      assistant message that has one in ``additional_kwargs``.
    - Is a no-op for models that never emit ``reasoning_content`` (the field
      is only sent back when previously stored, so chat-only models like
      gpt-4o or deepseek-chat see no behavior change).

    All three hooks are robust to both dict-shaped and Pydantic-shaped chunks
    so the same code works across langchain-openai versions.
    """

    def _create_chat_result(self, response, generations):
        result = super()._create_chat_result(response, generations)
        try:
            for i, choice in enumerate(self._iter_choices(response)):
                if i >= len(result.generations):
                    break
                reasoning = self._get_attr(self._get_attr(choice, "message"), "reasoning_content") or ""
                if reasoning:
                    result.generations[i].message.additional_kwargs["reasoning_content"] = reasoning
        except Exception:  # pragma: no cover -- defensive
            logger.exception("ReasoningChatOpenAI: failed to capture reasoning_content from non-streamed response")
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None
        reasoning = self._extract_reasoning_content_from_chunk(chunk)
        if reasoning:
            # AIMessageChunk.__add__ uses merge_dicts which concatenates string
            # values for duplicate keys, so per-chunk assignment accumulates
            # into the final AIMessage's additional_kwargs.
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            source_messages = self._coerce_to_messages(input_)
            payload_msgs = payload.get("messages") or []
            for src, dst in zip(source_messages, payload_msgs):
                if not isinstance(dst, dict) or dst.get("role") != "assistant":
                    continue
                extra = getattr(src, "additional_kwargs", None) or {}
                reasoning = extra.get("reasoning_content")
                if reasoning:
                    dst["reasoning_content"] = reasoning
        except Exception:  # pragma: no cover -- defensive
            logger.exception("ReasoningChatOpenAI: failed to inject reasoning_content into request payload")
        return payload

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _coerce_to_messages(input_):
        """Mirror what the base ``_get_request_payload`` does to derive messages,
        so our index alignment matches the payload exactly."""
        from langchain_core.prompt_values import PromptValue

        if isinstance(input_, PromptValue):
            return input_.to_messages()
        if isinstance(input_, list):
            return input_
        if isinstance(input_, str):
            return []
        try:
            return list(input_)
        except TypeError:
            return []

    @staticmethod
    def _extract_reasoning_content_from_chunk(chunk) -> str:
        """Return ``delta.reasoning_content`` for a streaming chunk.

        Handles both dict-shaped chunks (older / openai>=1 raw shape) and
        Pydantic ``ChatCompletionChunk`` objects (newer SDK shapes).
        """
        try:
            choices = ReasoningChatOpenAI._iter_choices(chunk)
            if not choices:
                return ""
            choice = choices[0]
            delta = ReasoningChatOpenAI._get_attr(choice, "delta")
            return ReasoningChatOpenAI._get_attr(delta, "reasoning_content") or ""
        except (AttributeError, KeyError, TypeError, IndexError):
            return ""

    @staticmethod
    def _iter_choices(obj):
        """Return ``obj.choices`` whether *obj* is a dict or a Pydantic object."""
        choices = ReasoningChatOpenAI._get_attr(obj, "choices")
        return list(choices) if choices else []

    @staticmethod
    def _get_attr(obj, name):
        """Look up *name* on *obj*, treating dicts and objects uniformly."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)


def build_llm(cfg: ActiveConfig | None = None):
    cfg = cfg or load_active_config()
    validate_api_key(cfg)

    common_kwargs = {
        "base_url": cfg.base_url,
        "api_key": get_api_key(cfg),
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "streaming": cfg.streaming,
    }

    if cfg.protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**common_kwargs)

    # All OpenAI-compatible endpoints get ReasoningChatOpenAI. It transparently
    # round-trips ``reasoning_content`` for thinking models (MiMo, DeepSeek
    # reasoner, Qwen-thinking, etc.) and is a no-op for models that never emit
    # the field (gpt-4o, deepseek-chat, etc.).
    return ReasoningChatOpenAI(**common_kwargs)
