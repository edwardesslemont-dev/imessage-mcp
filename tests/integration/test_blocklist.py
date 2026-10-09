"""Tests for the read blocklist (blocklist.py and the filters in chatdb.py).

Every test builds a synthetic chat.db in tmp_path with three people: a friend
in a 1:1 chat, a blocked person in a 1:1 chat, and a group with both of them.
Nothing touches the real ~/Library/Messages.

What each test pins down:
  1. Parsing: only backticked bullet entries count, phones compare on digits
  2. The blocked 1:1 chat is hidden whole: not listed, not fetchable, no messages
  3. In the group, only the blocked person's messages are dropped
  4. Context, unread, thread, attachments and contact search all filter too
  5. The search index never holds or returns a blocked message, even if stale
  6. A blocklist that exists but cannot be read fails closed
  7. No blocklist file means no filtering
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import blocklist
import chatdb
import search_index

FRIEND = "+15555550100"
BLOCKED = "+15555550199"

# chat ROWIDs
DM_FRIEND, DM_BLOCKED, GROUP = 1, 2, 3


def _build_chat_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER,
            service_name TEXT, chat_identifier TEXT, display_name TEXT, room_name TEXT);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, date INTEGER, text TEXT,
            attributedBody BLOB, is_from_me INTEGER, is_read INTEGER, date_read INTEGER,
            date_delivered INTEGER, is_audio_message INTEGER, handle_id INTEGER, service TEXT,
            item_type INTEGER, is_system_message INTEGER, associated_message_guid TEXT,
            associated_message_type INTEGER, associated_message_emoji TEXT,
            thread_originator_guid TEXT, reply_to_guid TEXT, date_edited INTEGER,
            cache_has_attachments INTEGER, is_finished INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT,
            mime_type TEXT, uti TEXT, transfer_name TEXT, total_bytes INTEGER,
            is_outgoing INTEGER, is_sticker INTEGER, created_date INTEGER,
            hide_attachment INTEGER, transfer_state INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        """
    )
    c.executemany("INSERT INTO handle VALUES (?, ?, 'iMessage')", [(1, FRIEND), (2, BLOCKED)])
    c.executemany(
        "INSERT INTO chat VALUES (?, ?, ?, 'iMessage', ?, '', '')",
        [
            (DM_FRIEND, "g-friend", 45, FRIEND),
            (DM_BLOCKED, "g-blocked", 45, BLOCKED),
            (GROUP, "g-group", 43, "chat-group"),
        ],
    )
    c.executemany(
        "INSERT INTO chat_handle_join VALUES (?, ?)",
        [(DM_FRIEND, 1), (DM_BLOCKED, 2), (GROUP, 1), (GROUP, 2)],
    )
    # (rowid, chat, handle_id, is_from_me, text, unread, thread_originator)
    msgs = [
        (1, DM_FRIEND, 1, 0, "friend hello", 0, None),
        (2, DM_FRIEND, 1, 1, "me to friend", 0, None),
        (3, DM_BLOCKED, 2, 0, "blocked secret", 1, None),
        (4, DM_BLOCKED, 2, 1, "me to blocked", 0, None),
        (5, GROUP, 1, 0, "friend in group", 1, None),
        (6, GROUP, 2, 0, "blocked in group", 1, "m5"),
        (7, GROUP, 0, 1, "me in group", 0, "m5"),
    ]
    for rowid, chat, handle, me, text, unread, thread in msgs:
        c.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, NULL, ?, ?, 0, 0, 0, ?, 'iMessage', 0, 0,"
            " '', 0, '', ?, '', 0, ?, 1)",
            (rowid, f"m{rowid}", rowid * 10**9, text, me, 0 if unread else 1, handle, thread,
             1 if rowid in (3, 6, 1) else 0),
        )
        c.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat, rowid))
    for att, msg in [(1, 1), (2, 3), (3, 6)]:
        c.execute(
            "INSERT INTO attachment VALUES (?, ?, ?, 'image/jpeg', '', ?, 1, 0, 0, ?, 0, 5)",
            (att, f"a{att}", f"f{att}.jpg", f"f{att}.jpg", att * 10**9),
        )
        c.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (msg, att))
    c.commit()
    c.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "chat.db"
    _build_chat_db(path)
    monkeypatch.setattr(chatdb, "CHAT_DB", path)
    monkeypatch.setattr(search_index, "INDEX_DB", tmp_path / "search.db")
    bl = tmp_path / "blocklist.md"
    bl.write_text(f"# Blocklist\n\nFree text {FRIEND} is ignored.\n\n- `(555) 555-0199` someone\n")
    monkeypatch.setattr(blocklist, "BLOCKLIST_PATH", bl)
    return bl


def _texts(msgs):
    return {m["text"] for m in msgs}


def test_parse_and_normalize(db):
    assert blocklist.load() == frozenset({"5555550199"})
    assert blocklist.is_blocked("+1 555 555 0199", blocklist.load())
    assert not blocklist.is_blocked(FRIEND, blocklist.load())
    assert blocklist.normalize("Some@One.com") == "some@one.com"


def test_blocked_dm_hidden_whole(db):
    assert DM_BLOCKED not in {c["chat_rowid"] for c in chatdb.list_chats(limit=50)}
    assert chatdb.get_chat(DM_BLOCKED) is None
    assert chatdb.get_chat("g-blocked") is None
    assert chatdb.is_blocked_chat(DM_BLOCKED) and chatdb.is_blocked_chat("g-blocked")
    assert chatdb.list_messages(DM_BLOCKED) == []
    assert chatdb.list_messages_chronological(DM_BLOCKED) == []
    assert _texts(chatdb.list_messages(DM_FRIEND)) == {"friend hello", "me to friend"}


def test_group_drops_only_blocked_sender(db):
    assert _texts(chatdb.list_messages(GROUP)) == {"friend in group", "me in group"}
    assert _texts(chatdb.list_messages_chronological(GROUP)) == {"friend in group", "me in group"}


def test_other_read_paths(db):
    assert chatdb.get_message_context(3) == []
    assert chatdb.get_message_context(6) == []
    assert _texts(chatdb.get_message_context(5)) == {"friend in group", "me in group"}
    assert _texts(chatdb.get_unread()) == {"friend in group"}
    assert _texts(chatdb.get_thread("m5")) == {"friend in group", "me in group"}
    assert {a["guid"] for a in chatdb.list_attachments()} == {"a1"}
    assert {a["guid"] for a in chatdb.list_attachments(chat_id=GROUP)} == set()
    assert [r["handle"] for r in chatdb.search_contacts_by_handle("555")] == [FRIEND]
    hc = chatdb.healthcheck()
    assert hc["blocklist_entries"] == 1 and hc["blocklist_matched_handles"] == 1


def test_search_index_purges_and_filters(db, monkeypatch):
    # Index everything first, as an index built before the blocklist would be.
    monkeypatch.setattr(blocklist, "BLOCKLIST_PATH", db.parent / "absent.md")
    search_index.refresh_index()
    with search_index._open_index() as idx:
        assert idx.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 7

    # Stale index, blocklist back on: search must not return blocked rows.
    monkeypatch.setattr(blocklist, "BLOCKLIST_PATH", db)
    hits = search_index.search("blocked")
    assert _texts(hits) == set()

    # A refresh purges them from disk.
    search_index.refresh_index()
    with search_index._open_index() as idx:
        left = {r[0] for r in idx.execute("SELECT text FROM messages_fts")}
    assert left == {"friend hello", "me to friend", "friend in group", "me in group"}


def test_unreadable_blocklist_fails_closed(db):
    db.unlink()
    db.symlink_to(db.parent / "missing-target.md")
    with pytest.raises(blocklist.BlocklistUnreadable):
        chatdb.list_messages(DM_FRIEND)
    with pytest.raises(blocklist.BlocklistUnreadable):
        search_index.search("friend")
    assert "cannot be read" in chatdb.healthcheck()["error"]


def test_no_blocklist_means_no_filter(db, monkeypatch):
    monkeypatch.setattr(blocklist, "BLOCKLIST_PATH", db.parent / "absent.md")
    assert len(chatdb.list_chats(limit=50)) == 3
    assert "blocked secret" in _texts(chatdb.list_messages(DM_BLOCKED))
