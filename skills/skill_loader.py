from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path


SKILLS_DIR = Path("skills")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    content: str
    source: str = "project"
    match_keywords: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        for line in self._body_lines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or self.name
            if stripped:
                return stripped
        return self.name

    @property
    def description(self) -> str:
        in_frontmatter = False
        for line in self.content.splitlines():
            stripped = line.strip()
            if stripped == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter and stripped.startswith("description:"):
                return stripped.split(":", 1)[1].strip()
        return self.title

    def _body_lines(self) -> list[str]:
        lines = self.content.splitlines()
        if lines and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    return lines[index + 1 :]
        return lines

    def matches(self, text: str) -> bool:
        normalized = text.lower()
        # Match by skill name tokens (words longer than 2 characters).
        name_tokens = [token for token in self.name.lower().replace("-", " ").split() if len(token) > 2]
        if any(token in normalized for token in name_tokens):
            return True

        # Match by keywords loaded from _meta.json.
        if self.match_keywords:
            return any(keyword in normalized for keyword in self.match_keywords)

        return False


def _load_meta_keywords(skill_dir: Path) -> tuple[str, ...]:
    """Load matchKeywords from the skill's _meta.json file."""
    meta_path = skill_dir / "_meta.json"
    if not meta_path.is_file():
        return ()
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse %s: %s", meta_path, exc)
        return ()
    keywords = meta.get("matchKeywords", [])
    if isinstance(keywords, list):
        return tuple(str(k).lower() for k in keywords if k)
    return ()


def load_skills(skills_dir: Path = SKILLS_DIR) -> list[Skill]:
    """Load local skills from skills/<name>/SKILL.md."""
    if not skills_dir.exists():
        return []

    loaded: list[Skill] = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        content = skill_file.read_text(encoding="utf-8").strip()
        if not content:
            continue
        content = content.replace("${SKILL_DIR}", skill_file.parent.as_posix())
        keywords = _load_meta_keywords(skill_file.parent)
        loaded.append(
            Skill(
                name=skill_file.parent.name,
                path=skill_file,
                content=content,
                match_keywords=keywords,
            )
        )
    return loaded


def select_skills_for_text(skills: list[Skill], text: str) -> list[Skill]:
    return [skill for skill in skills if skill.matches(text)]


def render_skill_catalog_for_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""

    sections = [
        "Installed local skills are available but not fully loaded by default. "
        "Use a skill only when the user's request clearly matches its purpose.",
    ]
    for skill in skills:
        sections.append(f"- {skill.name}: {skill.description} ({skill.source}, {skill.path.as_posix()})")
    return "\n".join(sections)


def render_skills_for_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""

    sections = [
        "Relevant local skill instructions:",
        "Follow these rules and workflows for the current request. Use available "
        "tools to run the commands named by a skill only when the referenced files exist.",
    ]
    for skill in skills:
        sections.append(f"\n<skill name=\"{skill.name}\" path=\"{skill.path.as_posix()}\">")
        sections.append(skill.content)
        sections.append("</skill>")
    return "\n".join(sections)
