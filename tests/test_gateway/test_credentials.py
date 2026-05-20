"""Tests for :mod:`gateway.credentials`.

The credentials store keeps platform configs in ``gateways.json`` keyed by
platform. The store is responsible for:
* creating a sibling ``.gitignore`` so check-ins don't leak secrets;
* round-tripping unicode and nested dicts;
* providing a ``mask`` helper for safe display;
* clearing a single platform without nuking others.
"""

from __future__ import annotations

import json

import pytest

from gateway import credentials as gw_creds


class TestSaveLoad:
    def test_save_then_load_roundtrip(self, tmp_config_dir):
        creds = {
            "app_id": "cli_xxx",
            "app_secret": "secret-value",
            "verify_token": "tok",
            "domain": "open.feishu.cn",
        }
        gw_creds.save("feishu", creds)
        loaded = gw_creds.load("feishu")
        assert loaded == creds

    def test_load_missing_platform_returns_empty(self, tmp_config_dir):
        assert gw_creds.load("nonexistent") == {}

    def test_load_missing_file_returns_empty(self, tmp_config_dir):
        # No file written yet -- must not crash.
        assert gw_creds.load("feishu") == {}

    def test_save_multiple_platforms(self, tmp_config_dir):
        gw_creds.save("feishu", {"app_id": "F"})
        gw_creds.save("qq", {"app_id": "Q"})
        assert gw_creds.load("feishu") == {"app_id": "F"}
        assert gw_creds.load("qq") == {"app_id": "Q"}

    def test_save_overwrites_same_platform(self, tmp_config_dir):
        gw_creds.save("feishu", {"app_id": "old"})
        gw_creds.save("feishu", {"app_id": "new"})
        assert gw_creds.load("feishu") == {"app_id": "new"}

    def test_save_doesnt_disturb_other_platforms(self, tmp_config_dir):
        gw_creds.save("feishu", {"app_id": "F"})
        gw_creds.save("qq", {"app_id": "Q"})
        # Overwrite feishu -- qq's entry must survive.
        gw_creds.save("feishu", {"app_id": "F2"})
        assert gw_creds.load("qq") == {"app_id": "Q"}


class TestGitignoreProtection:
    def test_save_creates_gitignore(self, tmp_config_dir):
        # First save into a fresh config_dir should drop a .gitignore so the
        # credentials file is never accidentally committed.
        gw_creds.save("feishu", {"app_id": "x"})
        gi = tmp_config_dir / ".gitignore"
        assert gi.exists()
        # The ignore pattern catches everything in this dir, which is what
        # we want for a private state directory.
        assert "*" in gi.read_text(encoding="utf-8")

    def test_existing_gitignore_is_preserved(self, tmp_config_dir):
        gi = tmp_config_dir / ".gitignore"
        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text("# user customized\nfoo\n", encoding="utf-8")
        gw_creds.save("feishu", {"app_id": "x"})
        # Original content untouched.
        assert "# user customized" in gi.read_text(encoding="utf-8")


class TestClear:
    def test_clear_removes_one_platform(self, tmp_config_dir):
        gw_creds.save("feishu", {"app_id": "F"})
        gw_creds.save("qq", {"app_id": "Q"})
        gw_creds.clear("feishu")
        assert gw_creds.load("feishu") == {}
        # QQ should be untouched.
        assert gw_creds.load("qq") == {"app_id": "Q"}

    def test_clear_missing_platform_is_noop(self, tmp_config_dir):
        # Idempotent: clearing what was never set must not raise.
        gw_creds.clear("nonexistent")

    def test_clear_keeps_other_data_intact(self, tmp_config_dir):
        path = gw_creds.gateways_path()
        gw_creds.save("feishu", {"a": "1"})
        gw_creds.save("qq", {"b": "2"})
        gw_creds.clear("feishu")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "feishu" not in data
        assert data["qq"] == {"b": "2"}


class TestMask:
    def test_mask_short_string(self):
        # Strings shorter than ``keep`` are fully starred so we never reveal
        # partial of a tiny secret.
        assert gw_creds.mask("abc") == "***"

    def test_mask_keeps_prefix(self):
        # Default keep=4
        out = gw_creds.mask("abcdefghij")
        assert out.startswith("abcd")
        assert "*" in out
        # No tail of the original value should show.
        assert "ij" not in out

    def test_mask_empty(self):
        assert gw_creds.mask("") == ""

    def test_mask_custom_keep(self):
        out = gw_creds.mask("abcdefghij", keep=2)
        assert out.startswith("ab")
        assert "cd" not in out
