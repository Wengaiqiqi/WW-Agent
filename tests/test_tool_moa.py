"""Tests for tool/tool_moa.py.

Mixture-of-Agents fans out to N reference models in a thread pool then runs
an aggregator. Tests use a fake ``_build_model_llm`` so no real provider is
contacted. Covers:

- happy path: all references succeed → aggregator combines them
- partial failure: some references fail; we proceed when ≥ MIN succeed
- total failure: no successful references → structured error result
- aggregator empty-then-retry behaviour
- the input-shape contracts (must have a non-empty user_prompt; must have
  at least one reference model)
"""
from __future__ import annotations

import pytest

from tool import tool_moa


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class _FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Returns a scripted response. If ``raises`` is set, raises that on
    every invoke instead."""

    def __init__(self, *, content="ok", raises: Exception | None = None):
        self.content = content
        self.raises = raises
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return _FakeLLMResponse(self.content)


def _patch_builder(monkeypatch, factory):
    """Wire up tool_moa._build_model_llm so each call gets a (possibly model-
    specific) fake LLM. ``factory(model, temperature) -> _FakeLLM``."""
    monkeypatch.setattr(tool_moa, "_build_model_llm", factory)


# ---------------------------------------------------------------------------
# input-shape contract
# ---------------------------------------------------------------------------


def test_empty_user_prompt_rejected():
    with pytest.raises(ValueError, match="user_prompt"):
        tool_moa.mixture_of_agents("")
    with pytest.raises(ValueError, match="user_prompt"):
        tool_moa.mixture_of_agents("   ")


def test_empty_reference_models_falls_back_to_defaults(monkeypatch):
    """``reference_models=[]`` is treated like ``None`` (use DEFAULT_REFERENCE_MODELS)
    rather than raising. The defaults are a non-empty tuple, so the
    ``at least one reference`` guard never trips for the public API — it's
    a belt-and-braces check for someone calling the helper directly with a
    forced empty list."""
    seen = []

    def factory(m, _t):
        seen.append(m)
        return _FakeLLM(content="ok")

    _patch_builder(monkeypatch, factory)
    out = tool_moa.mixture_of_agents("hello", reference_models=[])
    assert out["success"] is True
    # At least one of the DEFAULT_REFERENCE_MODELS got called.
    assert any(m in seen for m in tool_moa.DEFAULT_REFERENCE_MODELS)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_aggregates_references(monkeypatch):
    # Three distinct reference models, then one aggregator. Pick by model name.
    refs = {
        "ref-a": "Answer from A",
        "ref-b": "Answer from B",
        "ref-c": "Answer from C",
        "agg-x": "Synthesised: A+B+C",
    }
    _patch_builder(monkeypatch, lambda m, t: _FakeLLM(content=refs[m]))

    result = tool_moa.mixture_of_agents(
        "What's 2+2?",
        reference_models=["ref-a", "ref-b", "ref-c"],
        aggregator_model="agg-x",
    )

    assert result["success"] is True
    assert result["response"] == "Synthesised: A+B+C"
    assert set(result["models_used"]["reference_models"]) == {"ref-a", "ref-b", "ref-c"}
    assert result["models_used"]["aggregator_model"] == "agg-x"
    assert result["failures"] == []


def test_aggregator_sees_enumerated_references(monkeypatch):
    """The aggregator's system prompt should contain a numbered list of
    every successful reference response."""
    seen_aggregator_prompt: dict = {}

    class _RecordingLLM:
        def __init__(self, *, model: str):
            self.model = model

        def invoke(self, messages):
            if self.model.startswith("agg-"):
                seen_aggregator_prompt["system"] = messages[0].content
                return _FakeLLMResponse(content="ok")
            return _FakeLLMResponse(content=f"ref-{self.model}")

    _patch_builder(monkeypatch, lambda m, t: _RecordingLLM(model=m))

    tool_moa.mixture_of_agents(
        "q", reference_models=["model-1", "model-2"], aggregator_model="agg-1",
    )

    system = seen_aggregator_prompt["system"]
    assert "1." in system
    assert "2." in system
    assert "ref-model-1" in system
    assert "ref-model-2" in system


# ---------------------------------------------------------------------------
# partial / total failure
# ---------------------------------------------------------------------------


def test_partial_failure_still_succeeds(monkeypatch):
    """As long as at least ``MIN_SUCCESSFUL_REFERENCES`` (=1) reference
    succeeds, the run completes."""
    def factory(model, _t):
        if model == "broken":
            return _FakeLLM(raises=RuntimeError("provider 500"))
        return _FakeLLM(content=f"ans-{model}")

    _patch_builder(monkeypatch, factory)

    result = tool_moa.mixture_of_agents(
        "q",
        reference_models=["ok-1", "broken"],
        aggregator_model="agg",
    )

    assert result["success"] is True
    # The failed reference is listed with its error message.
    fails = {m: msg for m, msg in result["failures"]}
    assert "broken" in fails
    assert "provider 500" in fails["broken"]


def test_total_failure_returns_structured_error(monkeypatch):
    _patch_builder(
        monkeypatch,
        lambda m, t: _FakeLLM(raises=RuntimeError("everything down")),
    )

    # max_retries inside _run_reference is 3 with exponential sleep —
    # patch the sleep so the test runs fast.
    monkeypatch.setattr(tool_moa.time, "sleep", lambda *_a, **_kw: None)

    result = tool_moa.mixture_of_agents(
        "q",
        reference_models=["a", "b"],
        aggregator_model="agg",
    )

    assert result["success"] is False
    assert "Only 0" in result["error"] or "Only 0 reference" in result["error"]
    assert len(result["failures"]) == 2


def test_empty_reference_response_treated_as_failure(monkeypatch):
    def factory(model, _t):
        if model == "blank":
            return _FakeLLM(content="")
        if model == "good":
            return _FakeLLM(content="solid")
        if model == "agg":
            return _FakeLLM(content="final")
        raise AssertionError(f"unexpected model {model!r}")

    _patch_builder(monkeypatch, factory)
    monkeypatch.setattr(tool_moa.time, "sleep", lambda *_a, **_kw: None)

    result = tool_moa.mixture_of_agents(
        "q",
        reference_models=["good", "blank"],
        aggregator_model="agg",
    )

    assert result["success"] is True
    # ``blank`` should appear in failures with the "(empty response)" tag.
    fails = dict(result["failures"])
    assert fails.get("blank") == "(empty response)"


# ---------------------------------------------------------------------------
# aggregator retry on empty response
# ---------------------------------------------------------------------------


def test_aggregator_retries_once_on_empty_response(monkeypatch):
    """``_run_aggregator`` invokes the aggregator a second time if the first
    returns empty text. Only one retry — and only on the aggregator stage,
    not the references."""
    aggregator_responses = iter(["", "second-try"])

    class _Llm:
        def __init__(self, model):
            self.model = model

        def invoke(self, messages):
            if self.model.startswith("agg-"):
                return _FakeLLMResponse(next(aggregator_responses))
            return _FakeLLMResponse(content=f"r-{self.model}")

    _patch_builder(monkeypatch, lambda m, t: _Llm(m))

    result = tool_moa.mixture_of_agents(
        "q",
        reference_models=["x"],
        aggregator_model="agg-x",
    )

    assert result["success"] is True
    assert result["response"] == "second-try"


# ---------------------------------------------------------------------------
# extract_text helper
# ---------------------------------------------------------------------------


def test_extract_text_string():
    assert tool_moa._extract_text(_FakeLLMResponse(content="hi")) == "hi"


def test_extract_text_list_of_blocks():
    blocks = [
        {"type": "text", "text": "alpha"},
        {"type": "text", "text": "beta"},
        "raw",
    ]
    out = tool_moa._extract_text(_FakeLLMResponse(content=blocks))
    assert "alpha" in out and "beta" in out and "raw" in out


def test_extract_text_unknown_shape():
    """Falls back to ``str()`` so a stringified response is never lost."""
    out = tool_moa._extract_text(_FakeLLMResponse(content=12345))
    assert out == "12345"
