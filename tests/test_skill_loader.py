"""Tests for the skill loader."""

import json
import pytest
from pathlib import Path
from skills.skill_loader import Skill, load_skills, select_skills_for_text, _load_meta_keywords


class TestSkill:
    """Test Skill dataclass methods."""

    def test_title_from_markdown_header(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="# Test Skill Title\n\nDescription here.",
        )
        assert skill.title == "Test Skill Title"

    def test_title_from_first_line(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="First line title\n\nMore content.",
        )
        assert skill.title == "First line title"

    def test_title_fallback_to_name(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="",
        )
        assert skill.title == "test-skill"

    def test_description_from_frontmatter(self):
        content = """---
description: This is a test skill
---
# Skill Content
"""
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content=content,
        )
        assert skill.description == "This is a test skill"

    def test_description_fallback_to_title(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="# Test Title\n\nContent.",
        )
        assert skill.description == "Test Title"

    def test_matches_by_name_tokens(self):
        skill = Skill(
            name="baidu-ecommerce-search",
            path=Path("test/SKILL.md"),
            content="",
        )
        assert skill.matches("I want to search ecommerce")
        assert skill.matches("baidu search")
        assert not skill.matches("unrelated query")

    def test_matches_by_keywords(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="",
            match_keywords=("购物", "商品", "订单"),
        )
        assert skill.matches("我想购物")
        assert skill.matches("查看商品")
        assert skill.matches("订单状态")
        assert not skill.matches("unrelated query")

    def test_matches_case_insensitive(self):
        skill = Skill(
            name="test-skill",
            path=Path("test/SKILL.md"),
            content="",
            match_keywords=("shopping",),
        )
        assert skill.matches("I want to go SHOPPING")
        assert skill.matches("Shopping cart")


class TestLoadMetaKeywords:
    """Test loading keywords from _meta.json."""

    def test_load_keywords_from_valid_meta(self, tmp_path):
        meta_file = tmp_path / "_meta.json"
        meta_file.write_text(json.dumps({
            "matchKeywords": ["keyword1", "keyword2", "关键词"]
        }))
        keywords = _load_meta_keywords(tmp_path)
        assert keywords == ("keyword1", "keyword2", "关键词")

    def test_load_keywords_missing_file(self, tmp_path):
        keywords = _load_meta_keywords(tmp_path)
        assert keywords == ()

    def test_load_keywords_invalid_json(self, tmp_path):
        meta_file = tmp_path / "_meta.json"
        meta_file.write_text("invalid json{")
        keywords = _load_meta_keywords(tmp_path)
        assert keywords == ()

    def test_load_keywords_missing_field(self, tmp_path):
        meta_file = tmp_path / "_meta.json"
        meta_file.write_text(json.dumps({"other": "field"}))
        keywords = _load_meta_keywords(tmp_path)
        assert keywords == ()

    def test_load_keywords_not_a_list(self, tmp_path):
        meta_file = tmp_path / "_meta.json"
        meta_file.write_text(json.dumps({"matchKeywords": "not-a-list"}))
        keywords = _load_meta_keywords(tmp_path)
        assert keywords == ()


class TestLoadSkills:
    """Test loading skills from filesystem."""

    def test_load_skills_from_directory(self, tmp_path):
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# Test Skill\n\nContent here.")

        skills = load_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert "Test Skill" in skills[0].content

    def test_load_skills_empty_directory(self, tmp_path):
        skills = load_skills(tmp_path)
        assert skills == []

    def test_load_skills_nonexistent_directory(self, tmp_path):
        skills = load_skills(tmp_path / "nonexistent")
        assert skills == []

    def test_load_skills_with_keywords(self, tmp_path):
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# Test Skill")
        meta_file = skill_dir / "_meta.json"
        meta_file.write_text(json.dumps({"matchKeywords": ["test", "demo"]}))

        skills = load_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].match_keywords == ("test", "demo")

    def test_load_skills_with_requires_env(self, tmp_path):
        skill_dir = tmp_path / "needs-token"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Needs Token")
        (skill_dir / "_meta.json").write_text(
            json.dumps({"requiresEnv": ["MY_TOKEN", "MY_OTHER"]})
        )

        skills = load_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].requires_env == ("MY_TOKEN", "MY_OTHER")

    def test_requires_env_rejects_internal_control_vars(self, tmp_path, caplog):
        """Skills must NOT be able to opt into AUTHZ_HMAC_KEY, LANGCHAIN_AGENT_*,
        or provider credential names — the requiresEnv channel bypasses the
        secret filter and would otherwise be a privilege-escalation path."""
        import logging

        skill_dir = tmp_path / "hostile"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Hostile")
        (skill_dir / "_meta.json").write_text(
            json.dumps({"requiresEnv": [
                "AUTHZ_HMAC_KEY",
                "OPENAI_API_KEY",
                "LANGCHAIN_AGENT_PERMISSION_MODE",
                "LEGITIMATE_TOKEN",
            ]})
        )

        with caplog.at_level(logging.WARNING):
            skills = load_skills(tmp_path)
        assert len(skills) == 1
        # Only the legitimate-looking name survives.
        assert skills[0].requires_env == ("LEGITIMATE_TOKEN",)
        # Every rejected name is logged.
        warnings = "\n".join(rec.message for rec in caplog.records)
        assert "AUTHZ_HMAC_KEY" in warnings
        assert "OPENAI_API_KEY" in warnings
        assert "LANGCHAIN_AGENT_PERMISSION_MODE" in warnings

    def test_collect_skill_env_keys_unions_across_skills(self, tmp_path):
        """``collect_skill_env_keys`` is what the MCP host calls to decide
        which user-env vars to forward to subprocesses. It must merge across
        all installed skills, ignoring those without a requiresEnv field."""
        from skills.skill_loader import collect_skill_env_keys

        a = tmp_path / "skill-a"
        a.mkdir()
        (a / "SKILL.md").write_text("# A")
        (a / "_meta.json").write_text(json.dumps({"requiresEnv": ["TOKEN_A"]}))

        b = tmp_path / "skill-b"
        b.mkdir()
        (b / "SKILL.md").write_text("# B")
        (b / "_meta.json").write_text(json.dumps({"requiresEnv": ["TOKEN_B", "TOKEN_A"]}))

        c = tmp_path / "skill-c"
        c.mkdir()
        (c / "SKILL.md").write_text("# C with no meta")

        keys = collect_skill_env_keys(tmp_path)
        assert keys == {"TOKEN_A", "TOKEN_B"}


class TestSelectSkillsForText:
    """Test skill selection based on text."""

    def test_select_matching_skills(self):
        skills = [
            Skill("skill1", Path("s1"), "", match_keywords=("shopping",)),
            Skill("skill2", Path("s2"), "", match_keywords=("coding",)),
            Skill("skill3", Path("s3"), "", match_keywords=("music",)),
        ]
        selected = select_skills_for_text(skills, "I want to go shopping")
        assert len(selected) == 1
        assert selected[0].name == "skill1"

    def test_select_multiple_matching_skills(self):
        skills = [
            Skill("skill1", Path("s1"), "", match_keywords=("shopping",)),
            Skill("skill2", Path("s2"), "", match_keywords=("buy",)),
        ]
        selected = select_skills_for_text(skills, "I want to buy and go shopping")
        assert len(selected) == 2

    def test_select_no_matching_skills(self):
        skills = [
            Skill("skill1", Path("s1"), "", match_keywords=("shopping",)),
        ]
        selected = select_skills_for_text(skills, "unrelated text")
        assert len(selected) == 0
