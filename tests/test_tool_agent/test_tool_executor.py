import pytest
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool


def test_tool_specs_include_read_file():
    specs = build_tool_specs()
    names = {s.name for s in specs}
    assert "read_file" in names


@pytest.mark.asyncio
async def test_execute_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")
    result = await execute_tool("read_file", {"path": str(target)})
    # Be tolerant about return shape — the wrapped tool returns whatever the
    # legacy tool/tool_file_ops.read_text_file returns. Just check the payload
    # is somewhere in the stringified result.
    assert "hi there" in str(result)


@pytest.mark.asyncio
async def test_execute_unknown_tool_raises():
    with pytest.raises(ValueError, match="unknown tool"):
        await execute_tool("not_a_tool", {})
