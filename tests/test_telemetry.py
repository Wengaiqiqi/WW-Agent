"""Tests for orchestrator/telemetry.py — secret redaction + log rotation."""
from __future__ import annotations

import json

import pytest

from orchestrator import telemetry
from orchestrator.telemetry import redact_secrets, emit_event


def test_redacts_openai_key():
    out = redact_secrets("response was Authorization: Bearer sk-proj-aBcD1234EfGh5678IjKl")
    assert "sk-proj-aBcD1234EfGh5678IjKl" not in out
    assert "REDACTED" in out


def test_redacts_anthropic_key():
    msg = "request failed: sk-ant-api03-XXXXXXXXXXXXXXXXXX bad request"
    assert "sk-ant-api03-XXXXXXXXXXXXXXXXXX" not in redact_secrets(msg)


def test_redacts_github_pat():
    msg = "git config token=ghp_aBcDefGhIjKlMnOpQrStUv12"
    assert "ghp_aBcDefGhIjKlMnOpQrStUv12" not in redact_secrets(msg)


def test_redacts_aws_access_key():
    msg = "stack trace: AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE in environ"
    out = redact_secrets(msg)
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redacts_bearer_header():
    msg = "header: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"
    out = redact_secrets(msg)
    assert "eyJhbGciOiJIUzI1NiJ9.abc.def" not in out
    assert "Bearer ***REDACTED***" in out or "bearer ***REDACTED***" in out.lower()


def test_redacts_envdump_style_key_assignments():
    msg = "env dump: OPENAI_API_KEY=sk-123456789012 GOOGLE_TOKEN=longsecret123"
    out = redact_secrets(msg)
    assert "sk-123456789012" not in out
    assert "longsecret123" not in out


def test_keeps_innocent_messages_intact():
    msg = "(via A2A from orchestrator) tool.task — completed in 1.2s"
    assert redact_secrets(msg) == msg


def test_emit_event_redacts_on_write(tmp_path, monkeypatch):
    log_file = tmp_path / "telemetry.ndjson"
    monkeypatch.setattr(telemetry, "_PATH", log_file)

    emit_event(
        agent_id="tool-agent",
        trace_id="abc",
        message="caller passed OPENAI_API_KEY=sk-real-leak-1234567890 in env",
    )

    written = log_file.read_text(encoding="utf-8").strip()
    parsed = json.loads(written)
    assert "sk-real-leak-1234567890" not in parsed["message"]


def test_rotates_when_over_size(tmp_path, monkeypatch):
    log_file = tmp_path / "telemetry.ndjson"
    rotated = tmp_path / "telemetry.ndjson.1"
    monkeypatch.setattr(telemetry, "_PATH", log_file)
    monkeypatch.setattr(telemetry, "_MAX_BYTES", 100)

    log_file.write_text("x" * 200, encoding="utf-8")

    emit_event(agent_id="tool-agent", trace_id="t", message="next event")

    assert rotated.exists(), "log file should have been rotated"
    assert log_file.exists(), "fresh log file should exist after rotation"
    assert "next event" in log_file.read_text(encoding="utf-8")
