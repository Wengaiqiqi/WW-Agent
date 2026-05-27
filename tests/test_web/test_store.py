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


def test_conversation_crud_and_isolation(db_path):
    store.init_db(db_path)
    alice = store.create_user(db_path, "alice", "h", "s")
    bob = store.create_user(db_path, "bob", "h", "s")

    c1 = store.create_conversation(db_path, alice, "first")
    c2 = store.create_conversation(db_path, alice, "second")
    store.create_conversation(db_path, bob, "bob-only")

    # Alice sees only her two, newest-updated first.
    convs = store.list_conversations(db_path, alice)
    assert [c["id"] for c in convs] == [c2, c1]

    # Ownership: get_conversation returns the row; the route layer compares user_id.
    assert store.get_conversation(db_path, c1)["user_id"] == alice
    assert store.get_conversation(db_path, "missing") is None

    # Rename + delete.
    store.rename_conversation(db_path, c1, "renamed")
    assert store.get_conversation(db_path, c1)["title"] == "renamed"
    store.delete_conversation(db_path, c1)
    assert store.get_conversation(db_path, c1) is None


def test_messages_append_and_list(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "h", "s")
    cid = store.create_conversation(db_path, uid, "chat")

    store.add_message(db_path, cid, "user", "hello", "[]")
    store.add_message(db_path, cid, "assistant", "hi there",
                      '[{"type":"thinking","text":"..."}]')

    msgs = store.list_messages(db_path, cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["events_json"] == '[{"type":"thinking","text":"..."}]'


def test_delete_conversation_cascades_messages(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "h", "s")
    cid = store.create_conversation(db_path, uid, "chat")
    store.add_message(db_path, cid, "user", "hello", "[]")
    store.delete_conversation(db_path, cid)
    assert store.list_messages(db_path, cid) == []
