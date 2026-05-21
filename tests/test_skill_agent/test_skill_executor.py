"""Tests for the skill-agent execution loop.

Covers:
- ToolSpec assembly + frontmatter-driven descriptions.
- The non-streaming ``execute_skill`` (back-compat with MCP dispatch).
- The streaming ``execute_skill_streaming`` event shape.
- Tolerant envelope parsing (code fences / prose-wrapped JSON).
- The iteration-cap + no-final diagnostic.
- Authz failures surface as ``error`` events, not exceptions.
"""

from __future__ import annotations

import json
import time

import jwt as pyjwt
import pytest

from agents.skill_agent import skill_executor as skill_exec
from agents.skill_agent.skill_executor import (
    _parse_envelope,
    build_skill_specs,
    execute_skill,
    execute_skill_streaming,
)
from agents.shared.mock_chat_model import MockChatModel


TEST_KEY = "test-skill-executor-key"


@pytest.fixture(autouse=True)
def _set_hmac_key(monkeypatch):
    monkeypatch.setenv("AUTHZ_HMAC_KEY", TEST_KEY)


def _grant(slug: str, *, mode: str = "workspace-write") -> str:
    return pyjwt.encode(
        {
            "iss": "orchestrator",
            "sub": "skill-agent",
            "exp": int(time.time()) + 60,
            "permission_mode": mode,
            "allowed_tools": [f"skill.{slug}"],
            "trace_id": "t1",
        },
        TEST_KEY,
        algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# Spec assembly
# ---------------------------------------------------------------------------


def test_skill_specs_loaded_from_skills_dir():
    specs = build_skill_specs()
    names = {s.name for s in specs}
    assert any("baidu" in n for n in names)
    assert all(n.startswith("skill.") for n in names)


def test_skill_spec_description_is_not_the_generic_run_x_skill_string():
    """The planner needs a real description to route requests here. The
    previous ``Run the {slug} skill`` placeholder gave zero signal."""
    specs = build_skill_specs()
    baidu = next((s for s in specs if "baidu" in s.name), None)
    assert baidu is not None
    # Either pulled from frontmatter `description:` or from the first heading.
    assert baidu.description, "skill description must not be empty"
    assert baidu.description != "Run the baidu-ecommerce-search skill", baidu.description


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


def test_parse_envelope_plain_json():
    out = _parse_envelope('{"final": "ok"}')
    assert out == {"final": "ok"}


def test_parse_envelope_strips_markdown_fences():
    out = _parse_envelope('```json\n{"final": "fenced"}\n```')
    assert out == {"final": "fenced"}


def test_parse_envelope_extracts_first_balanced_object_from_prose():
    text = 'Sure, here is the plan: {"tool_calls":[{"tool":"read_file","arguments":{"path":"x"}}]} done.'
    out = _parse_envelope(text)
    assert out == {"tool_calls": [{"tool": "read_file", "arguments": {"path": "x"}}]}


def test_parse_envelope_handles_braces_inside_string():
    # Braces inside the JSON string must not confuse the balance counter.
    out = _parse_envelope('{"final": "value with } a brace"}')
    assert out == {"final": "value with } a brace"}


def test_parse_envelope_returns_none_for_pure_prose():
    assert _parse_envelope("Hello there, no JSON here.") is None


# ---------------------------------------------------------------------------
# execute_skill — non-streaming back-compat surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skill_final_envelope_returns_answer():
    llm = MockChatModel(responses=['{"final": "all done"}'])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}
    out = await execute_skill(slug, args, llm=llm)
    assert out == "all done"


@pytest.mark.asyncio
async def test_execute_skill_plain_text_reply_is_treated_as_final():
    """Non-JSON replies are not failures — they're the model's natural-language
    answer when it decides no tools are needed."""
    llm = MockChatModel(responses=["The answer is plain text."])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}
    out = await execute_skill(slug, args, llm=llm)
    assert out == "The answer is plain text."


# ---------------------------------------------------------------------------
# execute_skill_streaming — event shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_emits_thinking_text_done_for_final():
    llm = MockChatModel(responses=['{"final":"answer"}'])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}

    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    assert "text" in types
    assert types[-1] == "done"
    assert events[-1]["text"] == "answer"


@pytest.mark.asyncio
async def test_tool_results_are_fed_back_as_user_role(monkeypatch):
    """Regression: a previous version fed tool results back as
    ``{"role": "tool", ...}``. The OpenAI API (and langchain-openai's
    dict→message converter) requires ``role: tool`` messages to carry a
    ``tool_call_id`` pointing into ``assistant.tool_calls``. Our envelope
    protocol emits calls inside ``assistant.content`` instead, so no id
    exists — the converter then KeyErrors and the whole turn aborts with
    ``skill LLM error: 'tool_call_id'``. Tool results must therefore come
    back as ``role: user`` so all providers accept them."""

    async def fake_remote(tool_name, arguments, meta, *, slug=None):
        return "RESULT-V"

    monkeypatch.setattr(skill_exec, "_call_remote_tool", fake_remote)

    captured: list[list[dict]] = []

    class _CapturingLLM:
        """Records every messages list it's invoked with, then returns scripted replies."""

        def __init__(self, scripted):
            self._scripted = list(scripted)

        def invoke(self, messages):
            captured.append(list(messages))
            response_text = self._scripted.pop(0)

            class _Resp:
                content = response_text

            return _Resp()

    llm = _CapturingLLM([
        '{"tool_calls":[{"tool":"read_file","arguments":{"path":"x"}}]}',
        '{"final":"done"}',
    ])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}

    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    assert events[-1]["type"] == "done"

    # On the SECOND LLM call, the tool result must have been appended as a
    # user-role message — never role=tool.
    second_call_messages = captured[1]
    roles = [m.get("role") for m in second_call_messages]
    assert "tool" not in roles, f"role=tool found in {roles}"
    # And the tool result must actually be in there, prefixed clearly.
    tool_result_messages = [
        m for m in second_call_messages
        if m.get("role") == "user" and "[Tool results]" in m.get("content", "")
    ]
    assert tool_result_messages, second_call_messages
    assert "RESULT-V" in tool_result_messages[-1]["content"]


@pytest.mark.asyncio
async def test_streaming_emits_tool_call_and_tool_result(monkeypatch):
    """tool_calls envelope → orchestrator-visible tool_call / tool_result events,
    routed through a fake peer-tool stub so the test stays in-process."""

    async def fake_remote(tool_name, arguments, meta, *, slug=None):
        assert tool_name == "read_file"
        assert arguments == {"path": "x.txt"}
        return "FILE-CONTENT"

    monkeypatch.setattr(skill_exec, "_call_remote_tool", fake_remote)

    llm = MockChatModel(
        responses=[
            '{"tool_calls":[{"tool":"read_file","arguments":{"path":"x.txt"}}]}',
            '{"final":"got FILE-CONTENT"}',
        ]
    )
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}

    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    tcs = [e for e in events if e["type"] == "tool_call"]
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(tcs) == 1 and tcs[0]["name"] == "read_file"
    assert len(trs) == 1 and trs[0]["name"] == "read_file"
    assert events[-1]["type"] == "done"
    assert "FILE-CONTENT" in events[-1]["text"]


# ---------------------------------------------------------------------------
# Iteration cap & diagnostic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_emits_diagnostic_when_iteration_cap_exhausted(monkeypatch):
    """Regression: the loop used to silently return the last raw model output
    after MAX_ITERATIONS. Now it appends a `_(Skill made N tool calls but did
    not reach a final answer ...)_` diagnostic so the user sees what happened."""
    # Patch the cap to keep the test fast, then script tool_calls that never
    # produce a `final`.
    monkeypatch.setattr(skill_exec, "MAX_ITERATIONS", 3)

    async def fake_remote(tool_name, arguments, meta, *, slug=None):
        return {"ok": True}

    monkeypatch.setattr(skill_exec, "_call_remote_tool", fake_remote)

    looping = '{"tool_calls":[{"tool":"read_file","arguments":{"path":"x.txt"}}]}'
    llm = MockChatModel(responses=[looping, looping, looping, looping])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {"authz_grant": _grant(slug)}, "query": "x"}

    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    text_chunks = "".join(e["chunk"] for e in events if e["type"] == "text")
    final = next(e for e in events if e["type"] == "done")
    # Diagnostic must reach the user via streamed text AND be in the done payload.
    assert "did not reach a final" in text_chunks, text_chunks
    assert "did not reach a final" in final["text"], final
    # ``tool_calls`` count should match what we observed.
    assert final["tool_calls"] >= 1


# ---------------------------------------------------------------------------
# Authz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_missing_grant_emits_error_event():
    llm = MockChatModel(responses=['{"final":"never reached"}'])
    specs = build_skill_specs()
    slug = specs[0].name[len("skill."):]
    args = {"_meta": {}, "query": "x"}  # no grant
    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    assert events[0]["type"] == "error"
    assert "authz_grant" in events[0]["message"]


@pytest.mark.asyncio
async def test_streaming_wrong_grant_audience_emits_error_event():
    """Grant for skill A must not authorize skill B — the JWT's
    ``allowed_tools`` claim is enforced by ``verify_grant``."""
    specs = build_skill_specs()
    if len(specs) < 1:
        pytest.skip("no skills installed")
    slug = specs[0].name[len("skill."):]
    other_slug = slug + "-not-this"

    llm = MockChatModel(responses=['{"final":"x"}'])
    args = {"_meta": {"authz_grant": _grant(other_slug)}, "query": "x"}
    events = [e async for e in execute_skill_streaming(slug, args, llm=llm)]
    assert events[0]["type"] == "error"
    assert "authz" in events[0]["message"].lower()
