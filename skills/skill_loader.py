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
    # Tools the skill declared it needs in ``_meta.json::requiresTools``.
    #
    # Three states, with distinct semantics:
    #   * ``None``              — field absent from _meta.json. The loader
    #                             will fill the conservative default at
    #                             grant-mint time (read-class only).
    #   * empty tuple ``()``    — explicit ``"requiresTools": []`` in
    #                             _meta.json: skill author says "I need
    #                             no tools". Honored as-is — no grants
    #                             will be minted for this skill.
    #   * non-empty tuple       — explicit list of tools.
    #
    # Distinguishing None from empty matters: the previous "falsy ⇒ default"
    # check silently elevated an explicit-empty declaration to the default
    # toolset, which was the opposite of what the author meant.
    requires_tools: tuple[str, ...] | None = None

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


# Top-level keys ``_meta.json`` is allowed to carry. Unknown keys are
# logged so authors notice typos ("requireTools" vs "requiresTools") at
# load time rather than silently inheriting the default toolset.
_KNOWN_META_KEYS: frozenset[str] = frozenset({
    "slug", "version", "ownerId", "publishedAt",
    "matchKeywords", "requiresEnv", "requiresTools",
})


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
    if not isinstance(parsed, dict):
        return {}
    unknown = set(parsed.keys()) - _KNOWN_META_KEYS
    if unknown:
        logger.warning(
            "Skill %s: unknown keys in _meta.json: %s. Known: %s.",
            skill_dir.name, sorted(unknown), sorted(_KNOWN_META_KEYS),
        )
    return parsed


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


# Tools a skill is allowed to declare in ``requiresTools``. A skill that
# names a tool outside this set is rejected at load time — better than
# silently granting it. Mirrors the union of every mode's _TOOL_AGENT_MODE_TOOLS
# plus ``edit_file`` / ``apply_patch`` / ``memory`` (which skills sometimes
# need but tool-agent's ReAct loop doesn't bind by default).
_VALID_REQUIRES_TOOLS: frozenset[str] = frozenset({
    "read_file", "grep_search", "glob_search", "list_directory",
    "web_search", "web_extract", "web_crawl",
    "write_file", "edit_file", "apply_patch", "memory",
    "run_python", "run_command", "clarify",
})


# Default toolset for skills that don't declare ``requiresTools`` in their
# ``_meta.json``. Read-class only: a skill that legitimately needs to shell
# out should say so explicitly. This downgrades the previous behavior — where
# ``workspace-write`` gave every skill ``*`` access — to a least-privilege
# model where the skill author opts INTO the dangerous tools they need.
_DEFAULT_REQUIRES_TOOLS: tuple[str, ...] = (
    "read_file", "grep_search", "glob_search", "list_directory",
    "web_search", "web_extract", "web_crawl", "clarify",
)


def _load_meta_requires_tools(skill_dir: Path) -> tuple[str, ...] | None:
    """Load and validate ``requiresTools`` from the skill's ``_meta.json``.

    Returns ``None`` when the field is absent (caller maps to the default
    toolset). Returns ``()`` when the author explicitly wrote
    ``"requiresTools": []`` (caller honors as "skill needs no tools").
    Returns the validated subset otherwise.
    """
    meta = _load_meta(skill_dir)
    if "requiresTools" not in meta:
        return None
    raw = meta["requiresTools"]
    if raw is None:
        return None  # explicit null also means "fall back to default"
    if not isinstance(raw, list):
        logger.warning(
            "Skill %s: requiresTools should be a list, got %s — ignoring.",
            skill_dir.name, type(raw).__name__,
        )
        return None
    accepted: list[str] = []
    for entry in raw:
        if not entry:
            continue
        name = str(entry)
        if name in _VALID_REQUIRES_TOOLS:
            accepted.append(name)
        else:
            logger.warning(
                "Skill %s: requiresTools entry %r unknown — rejected. "
                "Valid tools: %s",
                skill_dir.name, name, sorted(_VALID_REQUIRES_TOOLS),
            )
    return tuple(accepted)


def effective_requires_tools(skill: "Skill") -> frozenset[str]:
    """Return the tools the skill is allowed to invoke via tool-agent.

    A skill that omits ``requiresTools`` (``None``) gets the read-class
    default. An explicit empty list is honored as "skill needs no tools"
    — returns an empty set so ``_mint_tool_grant`` will refuse any call.
    """
    if skill.requires_tools is None:
        return frozenset(_DEFAULT_REQUIRES_TOOLS)
    return frozenset(skill.requires_tools)


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


# Process-level cache for ``load_skills``. The skill directory is read on
# every ``_mint_tool_grant`` call to look up the calling skill's
# requiresTools; without caching, a skill that makes 10 tool calls reads N
# SKILL.md + N _meta.json files 10 times. Skills are static within a
# subprocess lifetime, so caching by directory mtime is safe.
#
# Keyed by (resolved path, mtime_ns of the directory). Dir mtime changes
# whenever a child file is added/removed/renamed — close enough for our
# "skills don't change mid-session" assumption. Content edits to existing
# SKILL.md don't invalidate, which is fine for production (skill authors
# don't hot-reload) but a test that mutates a SKILL.md needs to call
# ``invalidate_skills_cache`` explicitly.
_skills_cache: dict[tuple[str, int], list[Skill]] = {}


def invalidate_skills_cache() -> None:
    """Drop the cached skill list. For tests that mutate ``skills/`` mid-run."""
    _skills_cache.clear()


def load_skills(skills_dir: Path = SKILLS_DIR) -> list[Skill]:
    """Load local skills from skills/<name>/SKILL.md (cached per process)."""
    if not skills_dir.exists():
        return []

    try:
        cache_key = (str(skills_dir.resolve()), skills_dir.stat().st_mtime_ns)
    except OSError:
        cache_key = None  # disable cache on stat failure

    if cache_key is not None:
        cached = _skills_cache.get(cache_key)
        if cached is not None:
            return cached

    loaded: list[Skill] = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        content = skill_file.read_text(encoding="utf-8").strip()
        if not content:
            continue
        content = content.replace("${SKILL_DIR}", skill_file.parent.as_posix())
        keywords = _load_meta_keywords(skill_file.parent)
        requires_env = _load_meta_requires_env(skill_file.parent)
        requires_tools = _load_meta_requires_tools(skill_file.parent)
        loaded.append(
            Skill(
                name=skill_file.parent.name,
                path=skill_file,
                content=content,
                match_keywords=keywords,
                requires_env=requires_env,
                requires_tools=requires_tools,
            )
        )

    if cache_key is not None:
        _skills_cache[cache_key] = loaded
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
