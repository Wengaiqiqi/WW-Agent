from __future__ import annotations

from dataclasses import dataclass

from tool.tool_permissions import PermissionMode


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_permission: PermissionMode


TOOL_SPECS = (
    ToolSpec("calculator", "Evaluate a mathematical expression.", PermissionMode.READ_ONLY),
    ToolSpec("current_datetime", "Return the current local date and time.", PermissionMode.READ_ONLY),
    ToolSpec("list_directory", "List files and directories in the workspace.", PermissionMode.READ_ONLY),
    ToolSpec("read_file", "Read a text file with offset and limit support.", PermissionMode.READ_ONLY),
    ToolSpec("write_file", "Write a text file in the workspace.", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("edit_file", "Replace text in a workspace file.", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("apply_patch", "Apply a V4A unified-diff patch across multiple files.", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("glob_search", "Find files by glob pattern.", PermissionMode.READ_ONLY),
    ToolSpec("grep_search", "Search file contents with a regular expression.", PermissionMode.READ_ONLY),
    ToolSpec("run_python", "Execute Python code in a subprocess.", PermissionMode.DANGER_FULL_ACCESS),
    ToolSpec("run_command", "Execute a shell command in the workspace.", PermissionMode.DANGER_FULL_ACCESS),
    ToolSpec("clarify", "Ask the user a clarifying multiple-choice or open-ended question.", PermissionMode.READ_ONLY),
    ToolSpec("web_search", "Search the web (DuckDuckGo by default; Tavily when TAVILY_API_KEY is set).", PermissionMode.READ_ONLY),
    ToolSpec("web_extract", "Fetch a URL and return readable text.", PermissionMode.READ_ONLY),
    ToolSpec("memory", "Read or curate persistent memory (MEMORY.md / USER.md).", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("sleep", "Wait for a requested duration.", PermissionMode.READ_ONLY),
    ToolSpec("todo_write", "Update the structured task list for the current session.", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("config", "Get or set local agent settings.", PermissionMode.WORKSPACE_WRITE),
    ToolSpec("tool_manifest", "Return the registered tool manifest.", PermissionMode.READ_ONLY),
)


def tool_manifest_text() -> str:
    lines = ["Registered tool manifest:"]
    for spec in TOOL_SPECS:
        lines.append(f"- {spec.name} [{spec.required_permission.label}] {spec.description}")
    return "\n".join(lines)


def required_permission_for(tool_name: str) -> PermissionMode:
    for spec in TOOL_SPECS:
        if spec.name == tool_name:
            return spec.required_permission
    return PermissionMode.DANGER_FULL_ACCESS
