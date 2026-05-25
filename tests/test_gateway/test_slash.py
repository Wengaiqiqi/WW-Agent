from __future__ import annotations

import json

import pytest

from gateway import slash


def test_platform_from_session_key():
    assert slash._platform_from_session_key("qq:123") == "qq"
    assert slash._platform_from_session_key("feishu:abc") == "feishu"
    assert slash._platform_from_session_key("") == ""
    assert slash._platform_from_session_key("nokey") == ""


def test_is_authorized_reads_allowlist(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x", "allowed_users": "ou_a,ou_b"})
    assert slash._is_authorized("qq:123", "ou_a") is True
    assert slash._is_authorized("qq:123", "ou_b") is True
    assert slash._is_authorized("qq:123", "ou_other") is False


def test_is_authorized_empty_allowlist_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x"})  # no allowed_users
    assert slash._is_authorized("qq:123", "ou_a") is False


def test_is_authorized_no_user_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"allowed_users": "ou_a"})
    assert slash._is_authorized("qq:123", "") is False


def test_is_authorized_accepts_json_list(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("feishu", {"allowed_users": ["ou_a", "ou_b"]})
    assert slash._is_authorized("feishu:c", "ou_b") is True
