from __future__ import annotations

import pytest

from web import store


def test_init_and_create_user(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "hash1", "salt1")
    assert uid
    row = store.get_user_by_username(db_path, "alice")
    assert row["id"] == uid
    assert row["username"] == "alice"
    assert row["pwd_hash"] == "hash1"
    assert row["salt"] == "salt1"
    assert row["role"] == "user"


def test_get_user_by_id(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "bob", "h", "s")
    assert store.get_user(db_path, uid)["username"] == "bob"
    assert store.get_user(db_path, "nope") is None


def test_duplicate_username_rejected(db_path):
    store.init_db(db_path)
    store.create_user(db_path, "alice", "h", "s")
    with pytest.raises(store.DuplicateUsername):
        store.create_user(db_path, "alice", "h2", "s2")


def test_missing_user_returns_none(db_path):
    store.init_db(db_path)
    assert store.get_user_by_username(db_path, "ghost") is None
