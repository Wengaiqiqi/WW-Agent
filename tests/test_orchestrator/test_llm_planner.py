# tests/test_orchestrator/test_llm_planner.py
from orchestrator.main import LLMPlanner
from agents.shared.mock_chat_model import MockChatModel


def test_llm_planner_emits_structured_decision():
    llm = MockChatModel(responses=[
        '{"capability": "read_file", "arguments": {"path": "README.md"}}'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file", "skill.ppt-master"])
    decision = planner({"user_input": "read the readme", "trace_id": "t"})
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "README.md"


def test_llm_planner_strips_code_fences():
    llm = MockChatModel(responses=[
        '```json\n{"capability": "read_file", "arguments": {"path": "x"}}\n```'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file"])
    decision = planner({"user_input": "read x", "trace_id": "t"})
    assert decision["capability"] == "read_file"
