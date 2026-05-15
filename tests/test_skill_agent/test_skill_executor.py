import os
import time
import jwt as pyjwt
import pytest
from agents.skill_agent.skill_executor import build_skill_specs, execute_skill
from agents.shared.mock_chat_model import MockChatModel


TEST_KEY = "test-skill-executor-key"


@pytest.fixture(autouse=True)
def _set_hmac_key(monkeypatch):
    monkeypatch.setenv("AUTHZ_HMAC_KEY", TEST_KEY)


def _grant(slug: str) -> str:
    return pyjwt.encode(
        {"iss": "orchestrator", "sub": "skill-agent",
         "exp": int(time.time()) + 60,
         "permission_mode": "workspace-write",
         "allowed_tools": [f"skill.{slug}"], "trace_id": "t1"},
        TEST_KEY, algorithm="HS256",
    )


def test_skill_specs_loaded_from_skills_dir():
    specs = build_skill_specs()
    names = {s.name for s in specs}
    # The repo has at least the baidu-ecommerce-search skill
    assert any("baidu" in n for n in names)
    # All names follow the "skill.<slug>" pattern
    assert all(n.startswith("skill.") for n in names)


@pytest.mark.asyncio
async def test_execute_skill_calls_llm_and_returns_content():
    llm = MockChatModel(responses=["I performed the skill: result=X"])
    # Pick whatever slug actually exists; we want to test the mechanism, not a particular skill.
    specs = build_skill_specs()
    assert specs, "no skills found — test setup precondition failed"
    slug = specs[0].name[len("skill."):]  # strip "skill." prefix
    args = {
        "_meta": {"authz_grant": _grant(slug)},
        "query": "test",
    }
    out = await execute_skill(slug, args, llm=llm)
    assert "result=X" in out
