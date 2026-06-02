from __future__ import annotations

import importlib


def test_max_concurrency_default_and_override(monkeypatch):
    from gateway import runner
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
    assert runner.max_concurrency() == 1
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "5")
    assert runner.max_concurrency() == 5
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENCY", "garbage")
    assert runner.max_concurrency() == 1
    monkeypatch.delenv("GATEWAY_MAX_CONCURRENCY", raising=False)
    importlib.reload(runner)
