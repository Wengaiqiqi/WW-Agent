from __future__ import annotations

import io
import sys

from rich.console import Console

from orchestrator.repl_ui import COMMANDS, ReplUI


def _make_ui(stdin_text: str = "") -> tuple[ReplUI, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    ui = ReplUI(
        console=console,
        input_stream=io.StringIO(stdin_text),
        output_stream=buf,
    )
    return ui, buf


def test_replui_is_not_tty():
    ui, _ = _make_ui()
    assert not ui._is_tty()


def test_command_list_has_all_expected_commands():
    assert set(COMMANDS) == {
        "/help", "/exit", "/quit", "/status", "/agents", "/tools",
        "/permissions", "/config", "/model", "/skills",
        "/instructions", "/clear", "/compact", "/gateway",
        "/comm", "/task", "/chat",
    }


def test_render_help_contains_commands():
    ui, buf = _make_ui()
    ui.render_help()
    text = buf.getvalue()
    assert "Slash Commands" in text
    assert "/agents" in text
    assert "/compact" in text
    assert "/model" in text


def test_render_error_shows_title_and_message():
    ui, buf = _make_ui()
    ui.render_error("Planner Error", "invalid JSON")
    text = buf.getvalue()
    assert "Planner Error" in text
    assert "invalid JSON" in text


def test_render_welcome_shows_provider_model_permission_agents():
    ui, buf = _make_ui()
    ui.render_welcome(
        provider="openai", model="gpt-4o", protocol="json-rpc",
        permission_mode="workspace-write", agent_count=2,
        workspace="/home/project",
    )
    text = buf.getvalue()
    assert "W&W Agent CLI" in text
    assert "openai" in text
    assert "gpt-4o" in text
    assert "workspace-write" in text
    assert "2" in text


def test_render_warning_shows_message():
    ui, buf = _make_ui()
    ui.render_warning("Memory refresh failed, using old snapshot")
    text = buf.getvalue()
    assert "Memory refresh failed" in text


def test_render_table_renders_rows():
    ui, buf = _make_ui()
    ui.render_table(title="Test Table", columns=["Name", "Value"], rows=[["a", "1"], ["b", "2"]])
    text = buf.getvalue()
    assert "Test Table" in text
    assert "a" in text
    assert "1" in text


def test_render_table_empty_renders_none():
    ui, buf = _make_ui()
    ui.render_table(title="Empty", columns=["X", "Y"], rows=[])
    text = buf.getvalue()
    assert "<none>" in text


def test_read_input_non_tty_reads_from_stream():
    ui, _ = _make_ui(stdin_text="hello world\n")
    result = ui.read_input()
    assert result == "hello world"


def test_read_input_non_tty_eof():
    ui, _ = _make_ui(stdin_text="")
    try:
        ui.read_input()
        assert False, "expected EOFError"
    except EOFError:
        pass


def test_read_input_async_non_tty_reads_from_stream():
    import asyncio
    ui, _ = _make_ui(stdin_text="/help\n")

    async def _read():
        return await ui.read_input_async()

    result = asyncio.run(_read())
    assert result == "/help"


def test_render_text_panel():
    ui, buf = _make_ui()
    ui.render_text(title="Result", text="hello", style="green")
    text = buf.getvalue()
    assert "Result" in text
    assert "hello" in text


def test_render_divider():
    ui, buf = _make_ui()
    ui.render_divider()
    text = buf.getvalue()
    assert "-" in text


def test_clear():
    ui, buf = _make_ui()
    ui.clear()
    # clear() should not raise


def test_render_goodbye():
    ui, buf = _make_ui()
    ui.render_goodbye()
    text = buf.getvalue()
    assert "Goodbye" in text


def test_render_cancelled():
    ui, buf = _make_ui()
    ui.render_cancelled()
    text = buf.getvalue()
    assert "Cancelled" in text
