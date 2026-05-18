from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SKILLS_DIR = Path("skills")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    content: str
    source: str = "project"
    match_keywords: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()

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


def _load_meta(skill_dir: Path) -> dict[str, Any]:
    """Read _meta.json with logging on failure; returns empty dict on miss."""
    meta_path = skill_dir / "_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        parsed = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse %s: %s", meta_path, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_meta_keywords(skill_dir: Path) -> tuple[str, ...]:
    """Load matchKeywords from the skill's _meta.json file."""
    keywords = _load_meta(skill_dir).get("matchKeywords", [])
    if isinstance(keywords, list):
        return tuple(str(k).lower() for k in keywords if k)
    return ()


# Env vars a skill is NEVER allowed to opt into, even if it declares them
# in ``_meta.json::requiresEnv``. The skill-opt-in path bypasses the secret
# keyword filter, so without this deny-list a compromised or malicious skill
# could exfiltrate the orchestrator's HMAC signing key (forge JWT grants for
# any tool on tool-agent) or the user's provider credentials.
#
# Entries are matched case-insensitively against the bare name. Project-internal
# control variables are explicit; provider API keys use a prefix/suffix
# match below so we don't have to enumerate every vendor.
_REQUIRES_ENV_DENYLIST: frozenset[str] = frozenset({
    "AUTHZ_HMAC_KEY",
    "AGENT_ID",
    # LANGCHAIN_AGENT_* are reserved for orchestrator → subprocess control
    # plane; skills should never need to read them.
    "LANGCHAIN_AGENT_MODEL",
    "LANGCHAIN_AGENT_PERMISSION_MODE",
    "LANGCHAIN_AGENT_CONFIG_DIR",
    "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS",
})


def _is_requires_env_safe(name: str) -> bool:
    """Reject skill requiresEnv entries that name internal-control or provider
    credential vars. A skill that legitimately needs a provider key should
    receive it via its own scoped env var (e.g. ``BAIDU_EC_SEARCH_TOKEN``),
    not by reaching for the orchestrator's ``OPENAI_API_KEY``.
    """
    upper = name.upper()
    if upper in _REQUIRES_ENV_DENYLIST:
        return False
    if upper.startswith("LANGCHAIN_AGENT_"):
        return False
    # Catch the obvious credential-looking generic names. Skills already get
    # to bypass the broad keyword filter — the point of requiresEnv is to
    # name a *specific* variable, not "give me everything that looks like a
    # token". A skill that wants ``OPENAI_API_KEY`` is almost certainly
    # attempting a privilege grab.
    if upper in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
                 "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "GOOGLE_API_KEY", "GEMINI_API_KEY"}:
        return False
    return True


def _load_meta_requires_env(skill_dir: Path) -> tuple[str, ...]:
    """Load requiresEnv from the skill's _meta.json file.

    Skills declare here exactly which environment variables they need to
    operate (API tokens, QPS knobs, etc.). The orchestrator's MCP host
    consults the union of these across all loaded skills and passes only
    *those* variables through to agent subprocesses — keeping the strict
    env whitelist intact for everything else.

    Names that hit ``_REQUIRES_ENV_DENYLIST`` are silently dropped and
    logged so a misconfigured (or hostile) skill can't escalate privileges
    by naming the orchestrator's HMAC key, the user's provider API key,
    or another control-plane variable.
    """
    keys = _load_meta(skill_dir).get("requiresEnv", [])
    if not isinstance(keys, list):
        return ()
    accepted: list[str] = []
    for k in keys:
        if not k:
            continue
        name = str(k)
        if _is_requires_env_safe(name):
            accepted.append(name)
        else:
            logger.warning(
                "Skill %s declared %s in requiresEnv; rejected (reserved or "
                "credential-looking name).",
                skill_dir.name, name,
            )
    return tuple(accepted)


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
        requires_env = _load_meta_requires_env(skill_file.parent)
        loaded.append(
            Skill(
                name=skill_file.parent.name,
                path=skill_file,
                content=content,
                match_keywords=keywords,
                requires_env=requires_env,
            )
        )
    return loaded


def collect_skill_env_keys(skills_dir: Path = SKILLS_DIR) -> set[str]:
    """Return the union of env-var names declared by every installed skill.

    Used by ``orchestrator/mcp_host.py`` to pass through *only* the env
    variables skills declared in their ``_meta.json`` ``requiresEnv``
    field — every other variable from the user's shell is still stripped
    at the subprocess boundary.
    """
    keys: set[str] = set()
    if not skills_dir.exists():
        return keys
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "SKILL.md").is_file():
            continue
        for k in _load_meta_requires_env(skill_dir):
            keys.add(k)
    return keys


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


# Mirrors project_context's MAX_TOTAL_INSTRUCTION_CHARS budget so the two
# injection sources can't gang up to blow out the system prompt. When a
# skill's full content exceeds the remaining budget, only the truncated
# prefix is included — the agent can read the full SKILL.md with read_file
# when it needs deeper detail.
MAX_TOTAL_SKILL_CHARS = 8000


def render_skills_for_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""

    sections = [
        "Relevant local skill instructions:",
        "Follow these rules and workflows for the current request. Use available "
        "tools to run the commands named by a skill only when the referenced files exist.",
    ]
    remaining = MAX_TOTAL_SKILL_CHARS
    for skill in skills:
        if remaining <= 200:
            sections.append("_Additional skill content omitted after reaching the prompt budget._")
            break
        content = skill.content
        if len(content) > remaining:
            content = (
                content[:remaining]
                + "\n…[truncated; read the full SKILL.md via read_file when needed]"
            )
        remaining -= len(content)
        sections.append(f"\n<skill name=\"{skill.name}\" path=\"{skill.path.as_posix()}\">")
        sections.append(content)
        sections.append("</skill>")
    return "\n".join(sections)
