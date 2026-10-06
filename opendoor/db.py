"""SQLite storage shared by the voice agent and the SMS webhook server.

A seller (one phone number) can have several home inquiries. Each seller has an
"active" inquiry: the home the conversation is currently about. Every change is
written to `inquiry_events`, every inbound SMS is claimed in `processed_messages`
before it is applied (so duplicate webhook deliveries are no-ops), and every
outbound SMS is tracked in `outbound_messages` with its real delivery status.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("OPENDOOR_DB_PATH", Path(__file__).resolve().parent.parent / "opendoor.db"))

FIELDS = ("name", "address", "condition", "timeline")  # what a complete inquiry needs
SELLER_FIELDS = ("name",)  # stored on the seller, shared by all their homes
HOME_FIELDS = ("address", "condition", "timeline")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
    id                 INTEGER PRIMARY KEY,
    phone              TEXT NOT NULL UNIQUE,
    name               TEXT,
    portal_token       TEXT NOT NULL UNIQUE,   -- public "your homes" page
    active_inquiry_id  INTEGER,                -- the home the conversation is currently about
    pending            TEXT,                   -- JSON: an SMS action waiting on "which home?"
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inquiries (
    id             INTEGER PRIMARY KEY,
    seller_id      INTEGER NOT NULL REFERENCES sellers(id),
    address        TEXT,
    condition      TEXT,
    timeline       TEXT,
    listing_token  TEXT NOT NULL UNIQUE,       -- public listing page
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inquiry_events (
    id          INTEGER PRIMARY KEY,
    inquiry_id  INTEGER NOT NULL REFERENCES inquiries(id),
    source      TEXT NOT NULL,          -- 'call' or 'sms'
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    message_key TEXT,                   -- set when the change came from an SMS
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    webhook_id   TEXT PRIMARY KEY,      -- X-Webhook-ID header
    content_key  TEXT NOT NULL UNIQUE,  -- from + receivedAt + body, catches redelivery under a new ID
    phone        TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL,         -- 'processing' | 'done'
    outcome      TEXT,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    inquiry_id  INTEGER NOT NULL REFERENCES inquiries(id),
    filename    TEXT NOT NULL,          -- stored under media/<listing_token>/
    source_url  TEXT NOT NULL,
    webhook_id  TEXT,
    room        TEXT,                   -- vision tag, e.g. 'kitchen'
    caption     TEXT,
    notes       TEXT,                   -- condition observations from the photo
    is_home     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id                    INTEGER PRIMARY KEY,
    inquiry_id            INTEGER REFERENCES inquiries(id),
    to_number             TEXT NOT NULL,
    body                  TEXT NOT NULL,
    purpose               TEXT NOT NULL,   -- 'summary' | 'confirmation' | ...
    status                TEXT NOT NULL,   -- 'pending' | 'sent' | 'failed'
    error                 TEXT,
    agentphone_message_id TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
"""

# An inquiry joined with its seller, so callers can read inquiry["name"] / inquiry["phone"].
INQUIRY_VIEW = """
SELECT i.*, s.phone, s.name, s.portal_token, s.active_inquiry_id = i.id AS is_active
FROM inquiries i JOIN sellers s ON s.id = i.seller_id
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def token() -> str:
    return secrets.token_urlsafe(9)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        _migrate_to_multi_home(conn)
        conn.executescript(SCHEMA)


def _migrate_to_multi_home(conn: sqlite3.Connection) -> None:
    """v1 stored one inquiry per phone (inquiries.phone UNIQUE, name on the inquiry). Move to sellers + homes."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(inquiries)")}
    if not columns or "seller_id" in columns:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.execute(SCHEMA.split(";")[0])  # sellers table (execute, not executescript: that would COMMIT)
        conn.execute("""
            CREATE TABLE inquiries_v2 (
                id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES sellers(id),
                address TEXT, condition TEXT, timeline TEXT, listing_token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        for row in conn.execute("SELECT * FROM inquiries").fetchall():
            row = dict(row)
            conn.execute(
                "INSERT OR IGNORE INTO sellers (phone, name, portal_token, active_inquiry_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (row["phone"], row["name"], token(), row["id"], row["created_at"], row["updated_at"]),
            )
            seller_id = conn.execute("SELECT id FROM sellers WHERE phone = ?", (row["phone"],)).fetchone()["id"]
            conn.execute(
                "INSERT INTO inquiries_v2 (id, seller_id, address, condition, timeline, listing_token, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], seller_id, row["address"], row["condition"], row["timeline"],
                 row.get("listing_token") or token(), row["created_at"], row["updated_at"]),
            )
        conn.execute("DROP TABLE inquiries")
        conn.execute("ALTER TABLE inquiries_v2 RENAME TO inquiries")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# --- sellers -------------------------------------------------------------------


def get_seller(phone: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sellers WHERE phone = ?", (phone,)).fetchone()
    return _seller(row)


def get_or_create_seller(phone: str) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sellers (phone, portal_token, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (phone, token(), now(), now()),
        )
        return _seller(conn.execute("SELECT * FROM sellers WHERE phone = ?", (phone,)).fetchone())


def get_seller_by_token(portal_token: str) -> dict | None:
    with connect() as conn:
        return _seller(conn.execute("SELECT * FROM sellers WHERE portal_token = ?", (portal_token,)).fetchone())


def _seller(row) -> dict | None:
    if not row:
        return None
    seller = dict(row)
    seller["pending"] = json.loads(seller["pending"]) if seller["pending"] else None
    return seller


def set_pending(seller_id: int, pending: dict | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sellers SET pending = ?, updated_at = ? WHERE id = ?",
            (json.dumps(pending) if pending else None, now(), seller_id),
        )


def list_sellers() -> list[dict]:
    with connect() as conn:
        return [_seller(r) for r in conn.execute("SELECT * FROM sellers ORDER BY updated_at DESC").fetchall()]


# --- inquiries (homes) -----------------------------------------------------------


def get_inquiry(inquiry_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(INQUIRY_VIEW + " WHERE i.id = ?", (inquiry_id,)).fetchone()
    return dict(row) if row else None


def get_inquiry_by_token(listing_token: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(INQUIRY_VIEW + " WHERE i.listing_token = ?", (listing_token,)).fetchone()
    return dict(row) if row else None


def list_homes(seller_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(INQUIRY_VIEW + " WHERE i.seller_id = ? ORDER BY i.id", (seller_id,))]


def list_all_inquiries() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(INQUIRY_VIEW + " ORDER BY i.updated_at DESC")]


def create_inquiry(seller_id: int, make_active: bool = True) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO inquiries (seller_id, listing_token, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (seller_id, token(), now(), now()),
        )
        if make_active:
            conn.execute("UPDATE sellers SET active_inquiry_id = ?, updated_at = ? WHERE id = ?", (cur.lastrowid, now(), seller_id))
    return get_inquiry(cur.lastrowid)


def set_active(seller_id: int, inquiry_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sellers SET active_inquiry_id = ?, updated_at = ? WHERE id = ? AND EXISTS"
            " (SELECT 1 FROM inquiries WHERE id = ? AND seller_id = ?)",
            (inquiry_id, now(), seller_id, inquiry_id, seller_id),
        )


def active_inquiry(phone: str, create: bool = True) -> dict | None:
    """The home this phone number is currently talking about (creating the seller/home if needed)."""
    seller = get_or_create_seller(phone) if create else get_seller(phone)
    if not seller:
        return None
    if seller["active_inquiry_id"]:
        inquiry = get_inquiry(seller["active_inquiry_id"])
        if inquiry:
            return inquiry
    homes = list_homes(seller["id"])
    if homes:
        set_active(seller["id"], homes[-1]["id"])
        return get_inquiry(homes[-1]["id"])
    return create_inquiry(seller["id"]) if create else None


def update_inquiry(inquiry_id: int, changes: dict, source: str, message_key: str | None = None) -> dict:
    """Apply field changes and log each one. Returns {field: (old, new)} for fields that changed.

    `name` is stored on the seller (shared by all their homes); the rest on this home.
    """
    changes = {k: v.strip() for k, v in changes.items() if k in FIELDS and v and v.strip()}
    applied = {}
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(INQUIRY_VIEW + " WHERE i.id = ?", (inquiry_id,)).fetchone()
            for field, new in changes.items():
                old = row[field]
                if old == new:
                    continue
                if field in SELLER_FIELDS:
                    conn.execute(f"UPDATE sellers SET {field} = ?, updated_at = ? WHERE id = ?", (new, now(), row["seller_id"]))
                else:
                    conn.execute(f"UPDATE inquiries SET {field} = ? WHERE id = ?", (new, inquiry_id))
                conn.execute(
                    "INSERT INTO inquiry_events (inquiry_id, source, field, old_value, new_value, message_key, at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (inquiry_id, source, field, old, new, message_key, now()),
                )
                applied[field] = (old, new)
            if applied:
                conn.execute("UPDATE inquiries SET updated_at = ? WHERE id = ?", (now(), inquiry_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return applied


# --- inbound SMS idempotency -------------------------------------------------


def claim_message(webhook_id: str, content_key: str, phone: str, body: str) -> bool:
    """Record an inbound SMS as being processed. False if we've already seen it."""
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_messages (webhook_id, content_key, phone, body, status, received_at)"
                " VALUES (?, ?, ?, ?, 'processing', ?)",
                (webhook_id, content_key, phone, body, now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def finish_message(webhook_id: str, outcome: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE processed_messages SET status = 'done', outcome = ? WHERE webhook_id = ?", (outcome, webhook_id)
        )


def release_message(webhook_id: str) -> None:
    """Processing crashed before anything was applied: let a retry try again."""
    with connect() as conn:
        conn.execute("DELETE FROM processed_messages WHERE webhook_id = ? AND status = 'processing'", (webhook_id,))


# --- outbound SMS ------------------------------------------------------------


def record_outbound(inquiry_id: int | None, to_number: str, body: str, purpose: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO outbound_messages (inquiry_id, to_number, body, purpose, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (inquiry_id, to_number, body, purpose, now(), now()),
        )
        return cur.lastrowid


def mark_outbound(outbound_id: int, status: str, error: str | None = None, message_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE outbound_messages SET status = ?, error = ?, agentphone_message_id = ?, updated_at = ? WHERE id = ?",
            (status, error, message_id, now(), outbound_id),
        )


def recent_outbound_count(to_number: str, seconds: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM outbound_messages WHERE to_number = ? AND datetime(created_at) >= datetime('now', ?)",
            (to_number, f"-{seconds} seconds"),
        ).fetchone()[0]


# --- photos & activity ---------------------------------------------------------


def add_photo(inquiry_id: int, filename: str, source_url: str, webhook_id: str, analysis: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO photos (inquiry_id, filename, source_url, webhook_id, room, caption, notes, is_home, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inquiry_id, filename, source_url, webhook_id,
                analysis.get("room"), analysis.get("caption"), analysis.get("notes"),
                1 if analysis.get("is_home", True) else 0, now(),
            ),
        )
        conn.execute("UPDATE inquiries SET updated_at = ? WHERE id = ?", (now(), inquiry_id))
        return cur.lastrowid


def list_photos(inquiry_id: int, include_hidden: bool = False) -> list[dict]:
    query = "SELECT * FROM photos WHERE inquiry_id = ?" + ("" if include_hidden else " AND is_home = 1") + " ORDER BY id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, (inquiry_id,)).fetchall()]


def list_events(inquiry_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, field, old_value, new_value, at FROM inquiry_events WHERE inquiry_id = ? ORDER BY id DESC LIMIT ?",
            (inquiry_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
