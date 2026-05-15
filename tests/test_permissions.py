"""Tests for the permission system."""

import os
import pytest

from tool.tool_permissions import PermissionMode, PermissionPolicy, authorize_tool


class TestPermissionMode:
    """Test permission mode parsing."""

    def test_parse_read_only_variants(self):
        assert PermissionMode.parse("read-only") == PermissionMode.READ_ONLY
        assert PermissionMode.parse("readonly") == PermissionMode.READ_ONLY
        assert PermissionMode.parse("read") == PermissionMode.READ_ONLY

    def test_parse_workspace_write_variants(self):
        assert PermissionMode.parse("workspace-write") == PermissionMode.WORKSPACE_WRITE
        assert PermissionMode.parse("workspace") == PermissionMode.WORKSPACE_WRITE
        assert PermissionMode.parse("write") == PermissionMode.WORKSPACE_WRITE

    def test_parse_danger_full_access_variants(self):
        assert PermissionMode.parse("danger-full-access") == PermissionMode.DANGER_FULL_ACCESS
        assert PermissionMode.parse("danger") == PermissionMode.DANGER_FULL_ACCESS
        assert PermissionMode.parse("full") == PermissionMode.DANGER_FULL_ACCESS
        assert PermissionMode.parse("allow") == PermissionMode.DANGER_FULL_ACCESS

    def test_parse_none_defaults_to_workspace_write(self):
        """When None is passed, default to workspace-write."""
        assert PermissionMode.parse(None) == PermissionMode.WORKSPACE_WRITE

    def test_parse_unknown_defaults_to_read_only(self):
        """Unknown values should default to read-only for safety."""
        assert PermissionMode.parse("garbage") == PermissionMode.READ_ONLY
        assert PermissionMode.parse("something-else") == PermissionMode.READ_ONLY

    def test_parse_case_insensitive(self):
        assert PermissionMode.parse("READ-ONLY") == PermissionMode.READ_ONLY
        assert PermissionMode.parse("Workspace-Write") == PermissionMode.WORKSPACE_WRITE

    def test_parse_underscore_normalized(self):
        assert PermissionMode.parse("read_only") == PermissionMode.READ_ONLY
        assert PermissionMode.parse("workspace_write") == PermissionMode.WORKSPACE_WRITE

    def test_label(self):
        assert PermissionMode.READ_ONLY.label == "read-only"
        assert PermissionMode.WORKSPACE_WRITE.label == "workspace-write"
        assert PermissionMode.DANGER_FULL_ACCESS.label == "danger-full-access"


class TestPermissionPolicy:
    """Test permission policy authorization."""

    def test_authorize_read_only_allows_read(self):
        policy = PermissionPolicy(PermissionMode.READ_ONLY)
        # Should not raise for tools requiring READ_ONLY
        policy.authorize("calculator", PermissionMode.READ_ONLY)

    def test_authorize_read_only_denies_write(self):
        policy = PermissionPolicy(PermissionMode.READ_ONLY)
        with pytest.raises(PermissionError):
            policy.authorize("write_file", PermissionMode.WORKSPACE_WRITE)

    def test_authorize_workspace_write_allows_write(self):
        policy = PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
        policy.authorize("write_file", PermissionMode.WORKSPACE_WRITE)

    def test_authorize_workspace_write_denies_danger(self):
        policy = PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
        with pytest.raises(PermissionError):
            policy.authorize("run_command", PermissionMode.DANGER_FULL_ACCESS)

    def test_authorize_danger_allows_all(self):
        policy = PermissionPolicy(PermissionMode.DANGER_FULL_ACCESS)
        policy.authorize("calculator", PermissionMode.READ_ONLY)
        policy.authorize("write_file", PermissionMode.WORKSPACE_WRITE)
        policy.authorize("run_command", PermissionMode.DANGER_FULL_ACCESS)

    def test_error_message_contains_details(self):
        policy = PermissionPolicy(PermissionMode.READ_ONLY)
        with pytest.raises(PermissionError, match="write_file"):
            policy.authorize("write_file", PermissionMode.WORKSPACE_WRITE, "test.txt")

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")
        policy = PermissionPolicy.from_env()
        assert policy.active_mode == PermissionMode.READ_ONLY

    def test_from_env_unset_defaults(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LANGCHAIN_AGENT_PERMISSION_MODE", raising=False)
        # Set workspace to tmp_path to avoid loading local settings.json
        monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))
        # Without a settings file, parse(None) defaults to workspace-write.
        policy = PermissionPolicy.from_env()
        assert policy.active_mode == PermissionMode.WORKSPACE_WRITE
