"""Tests for the file operations tools."""

import json
import os
import pytest
from unittest.mock import patch

from tool.tool_file_ops import (
    read_text_file,
    write_text_file,
    edit_text_file,
    list_directory_structured,
    glob_search_files,
    grep_search_files,
    resolve_workspace_path,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace directory."""
    monkeypatch.setenv("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


class TestResolveWorkspacePath:
    """Test workspace path resolution and security."""

    def test_resolve_relative_path(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("content")
        resolved = resolve_workspace_path("test.txt")
        assert resolved == test_file.resolve()

    def test_reject_path_outside_workspace(self, workspace):
        with pytest.raises(PermissionError, match="outside workspace"):
            resolve_workspace_path("../../etc/passwd")

    def test_allow_missing_path(self, workspace):
        resolved = resolve_workspace_path("new_file.txt", allow_missing=True)
        assert str(resolved).endswith("new_file.txt")


class TestReadTextFile:
    """Test file reading."""

    def test_read_full_file(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3")
        result = json.loads(read_text_file("test.txt"))
        assert result["type"] == "text"
        assert result["file"]["numLines"] == 3
        assert result["file"]["totalLines"] == 3

    def test_read_with_offset(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3")
        result = json.loads(read_text_file("test.txt", offset=1))
        assert result["file"]["content"] == "line2\nline3"
        assert result["file"]["startLine"] == 2

    def test_read_with_limit(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("line1\nline2\nline3")
        result = json.loads(read_text_file("test.txt", limit=2))
        assert result["file"]["numLines"] == 2
        assert result["file"]["content"] == "line1\nline2"


class TestWriteTextFile:
    """Test file writing."""

    def test_create_new_file(self, workspace):
        result = json.loads(write_text_file("new.txt", "new content"))
        assert result["type"] == "create"
        assert (workspace / "new.txt").read_text() == "new content"

    def test_update_existing_file(self, workspace):
        test_file = workspace / "existing.txt"
        test_file.write_text("old content")
        result = json.loads(write_text_file("existing.txt", "updated content"))
        assert result["type"] == "update"
        assert result["originalFile"] == "old content"
        assert (workspace / "existing.txt").read_text() == "updated content"

    def test_create_with_subdirectory(self, workspace):
        write_text_file("sub/dir/file.txt", "deep content")
        assert (workspace / "sub" / "dir" / "file.txt").read_text() == "deep content"


class TestEditTextFile:
    """Test file editing."""

    def test_replace_text(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("hello world")
        result = json.loads(edit_text_file("test.txt", "hello", "goodbye"))
        assert test_file.read_text() == "goodbye world"
        assert len(result["structuredPatch"]) > 0

    def test_replace_all(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("aaa bbb aaa")
        edit_text_file("test.txt", "aaa", "ccc", replace_all=True)
        assert test_file.read_text() == "ccc bbb ccc"

    def test_error_on_same_string(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("content")
        with pytest.raises(ValueError, match="must differ"):
            edit_text_file("test.txt", "content", "content")

    def test_error_on_not_found(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("content")
        with pytest.raises(ValueError, match="not found"):
            edit_text_file("test.txt", "missing", "replacement")


class TestGrepSearchFiles:
    """Test grep search."""

    def test_search_content(self, workspace):
        test_file = workspace / "test.py"
        test_file.write_text("def hello():\n    return 'world'\n")
        result = json.loads(grep_search_files("hello", str(workspace)))
        assert result["numFiles"] >= 1

    def test_search_case_insensitive(self, workspace):
        test_file = workspace / "test.txt"
        test_file.write_text("Hello World")
        result = json.loads(grep_search_files(
            "hello", str(workspace), case_insensitive=True,
        ))
        assert result["numFiles"] >= 1

    def test_binary_file_skipped(self, workspace):
        binary_file = workspace / "binary.bin"
        binary_file.write_bytes(b"\x00\x01\x02\xff\xfe binary content")
        text_file = workspace / "text.txt"
        text_file.write_text("binary content")
        result = json.loads(grep_search_files("binary", str(workspace)))
        assert result["skippedBinaryFiles"] == 1
        assert result["numFiles"] == 1
