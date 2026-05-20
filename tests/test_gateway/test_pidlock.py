"""Tests for :mod:`gateway._pidlock`.

The lock prevents two simultaneous gateway processes for the same platform
from connecting to the same bot WS (which causes duplicate replies in
Feishu / QQ since both clients receive every event). The tricky bits are:
stale PID files (process died without releasing), and self-owned locks
(same process re-acquires when re-entering Start from REPL).
"""

from __future__ import annotations

import os

import pytest

from gateway._pidlock import (
    AlreadyRunning,
    _is_pid_alive,
    acquire,
    release,
)


PLATFORM = "test_platform"


class TestAcquireRelease:
    def test_acquire_writes_pid_file(self, tmp_config_dir):
        path = acquire(PLATFORM)
        assert path.exists()
        assert path.read_text(encoding="utf-8") == str(os.getpid())

    def test_release_removes_file(self, tmp_config_dir):
        acquire(PLATFORM)
        release(PLATFORM)
        assert not (tmp_config_dir / f"{PLATFORM}.pid").exists()

    def test_release_idempotent(self, tmp_config_dir):
        # Calling release twice (or on a never-acquired lock) must not raise.
        release(PLATFORM)
        release(PLATFORM)

    def test_self_owned_overwrites_silently(self, tmp_config_dir):
        # Same PID re-acquiring is normal (REPL Stop -> Start cycle).
        acquire(PLATFORM)
        # Should not raise -- we own the lock.
        path = acquire(PLATFORM)
        assert path.exists()


class TestStaleLockRecovery:
    def test_stale_pid_overwritten(self, tmp_config_dir):
        # Simulate a dead process that never released.
        lock = tmp_config_dir / f"{PLATFORM}.pid"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999999", encoding="utf-8")  # PID very unlikely to exist
        assert not _is_pid_alive(999999999)
        # New acquire should detect stale + take over.
        path = acquire(PLATFORM)
        assert path.read_text(encoding="utf-8") == str(os.getpid())

    def test_malformed_pid_file_treated_as_stale(self, tmp_config_dir):
        # A garbled lock file (e.g. truncated write) should not block startup.
        lock = tmp_config_dir / f"{PLATFORM}.pid"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-an-int", encoding="utf-8")
        path = acquire(PLATFORM)
        assert path.read_text(encoding="utf-8") == str(os.getpid())


class TestContention:
    def test_alive_other_pid_raises(self, tmp_config_dir):
        # Write a known-alive PID (our own) but a DIFFERENT one than ours.
        # We achieve that by writing our own pid + ensuring acquire checks
        # for "PID != my own". Pre-write the lock with our pid -- self-owned,
        # which should NOT raise. To force contention, fake a different live
        # pid by patching _is_pid_alive.
        from gateway import _pidlock

        # Pretend PID 12345 is alive.
        original = _pidlock._is_pid_alive
        _pidlock._is_pid_alive = lambda pid: pid == 12345
        try:
            lock = tmp_config_dir / f"{PLATFORM}.pid"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("12345", encoding="utf-8")
            with pytest.raises(AlreadyRunning) as exc:
                acquire(PLATFORM)
            # Error message should include the offending pid so the user
            # can ``Stop-Process -Id 12345``.
            assert "12345" in str(exc.value)
        finally:
            _pidlock._is_pid_alive = original


class TestIsPidAlive:
    def test_own_pid_is_alive(self):
        # Our own process is obviously alive while the test runs.
        assert _is_pid_alive(os.getpid()) is True

    def test_negative_or_zero_dead(self):
        assert _is_pid_alive(0) is False
        assert _is_pid_alive(-1) is False

    def test_huge_pid_unlikely_to_exist(self):
        # 4-byte PID max on most systems is ~2^32; 999999999 is realistically
        # never assigned. Test verifies the "not alive" path doesn't raise.
        assert _is_pid_alive(999999999) is False
