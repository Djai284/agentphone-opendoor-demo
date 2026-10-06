import sqlite3

from conftest import OTHER, SELLER
from opendoor import db


def test_callers_are_kept_separate():
    a = db.active_inquiry(SELLER)
    b = db.active_inquiry(OTHER)
    db.update_inquiry(a["id"], {"name": "Jay", "timeline": "December"}, source="call")
    db.update_inquiry(b["id"], {"name": "Sam", "timeline": "ASAP"}, source="call")
    assert db.get_inquiry(a["id"])["timeline"] == "December"
    assert db.get_inquiry(b["id"])["name"] == "Sam"
    assert a["seller_id"] != b["seller_id"]


def test_one_seller_many_homes_share_the_name():
    first = db.active_inquiry(SELLER)
    db.update_inquiry(first["id"], {"name": "Jay", "address": "1 Main St, Austin, TX"}, source="call")
    second = db.create_inquiry(first["seller_id"])
    db.update_inquiry(second["id"], {"address": "12 Bergen St, Brooklyn, NY"}, source="sms")

    homes = db.list_homes(first["seller_id"])
    assert [h["address"] for h in homes] == ["1 Main St, Austin, TX", "12 Bergen St, Brooklyn, NY"]
    assert all(h["name"] == "Jay" for h in homes)  # name lives on the seller
    assert db.active_inquiry(SELLER)["id"] == second["id"]  # newest home becomes the active one


def test_updates_are_logged_and_noops_are_not():
    home = db.active_inquiry(SELLER)
    assert db.update_inquiry(home["id"], {"timeline": "spring"}, source="call") == {"timeline": (None, "spring")}
    assert db.update_inquiry(home["id"], {"timeline": "spring"}, source="sms") == {}
    events = db.list_events(home["id"])
    assert len(events) == 1 and events[0]["source"] == "call"


def test_claim_message_is_idempotent():
    assert db.claim_message("wh_1", "content_1", SELLER, "hi") is True
    assert db.claim_message("wh_1", "content_1", SELLER, "hi") is False  # same delivery again
    assert db.claim_message("wh_2", "content_1", SELLER, "hi") is False  # same message, new delivery id
    db.release_message("wh_1")  # processing crashed: a retry may try again
    assert db.claim_message("wh_1", "content_1", SELLER, "hi") is True


def test_migrates_v1_single_inquiry_per_phone(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE inquiries (id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE, name TEXT, address TEXT,
            condition TEXT, timeline TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, listing_token TEXT);
        INSERT INTO inquiries VALUES (7, '+14155560123', 'Jay', '400 W 59th St', 'new', 'December',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'keepThisToken');
        CREATE TABLE photos (id INTEGER PRIMARY KEY, inquiry_id INTEGER NOT NULL REFERENCES inquiries(id),
            filename TEXT NOT NULL, source_url TEXT NOT NULL, webhook_id TEXT, room TEXT, caption TEXT, notes TEXT,
            is_home INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        INSERT INTO photos (inquiry_id, filename, source_url, created_at) VALUES (7, 'abc.jpg', 'x', '2026-01-01');
    """)
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    db.init()  # running twice is safe

    home = db.get_inquiry_by_token("keepThisToken")  # old listing links keep working
    assert home["id"] == 7 and home["name"] == "Jay" and home["phone"] == SELLER
    assert len(db.list_photos(7)) == 1
    assert db.get_seller(SELLER)["active_inquiry_id"] == 7
