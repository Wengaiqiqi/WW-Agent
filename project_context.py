from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


MAX_TOTAL_INSTRUCTION_CHARS = 12000


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    content: str


def workspace_root() -> Path:
    return Path(os.getenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", os.getcwd())).resolve()


def ancestors_from_workspace() -> list[Path]:
    root = workspace_root()
    return list(reversed(list(root.parents))) + [root]


def discover_instruction_files() -> list[InstructionFile]:
    files: list[InstructionFile] = []
    seen_contents: set[str] = set()
    for directory in ancestors_from_workspace():
        for candidate in (
            directory / "agent.md",
            directory / "CLAW.md",
            directory / "CLAW.local.md",
            directory / ".claw" / "CLAW.md",
            directory / ".claw" / "instructions.md",
            directory / "AGENTS.md",
            directory / ".agents" / "instructions.md",
        ):
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not content or content in seen_contents:
                continue
            seen_contents.add(content)
            files.append(InstructionFile(path=candidate, content=content))
    return files


def render_instruction_files(files: list[InstructionFile]) -> str:
    if not files:
        return ""
    sections = ["# Project instructions"]
    remaining = MAX_TOTAL_INSTRUCTION_CHARS
    for file in files:
        if remaining <= 0:
            sections.append("_Additional instruction content omitted after reaching the prompt budget._")
            break
        content = file.content[:remaining]
        remaining -= len(content)
        sections.append(f"\n## {display_path(file.path)}\n{content}")
    return "\n".join(sections)


def load_project_settings() -> dict[str, object]:
    settings: dict[str, object] = {}
    for path in (
        workspace_root() / ".claude" / "settings.json",
        workspace_root() / ".claw" / "settings.json",
        workspace_root() / ".codex" / "settings.json",
    ):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            settings.update(data)
    return settings


def display_path(path: Path) -> str:
    root = workspace_root()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)
