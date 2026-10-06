"""Print stored state: sellers, homes, change history, processed texts, outbound texts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from opendoor import db  # noqa: E402

db.init()
with db.connect() as conn:
    for title, query in [
        ("SELLERS", "SELECT id, phone, name, active_inquiry_id, pending FROM sellers"),
        ("HOMES (inquiries)", "SELECT i.id, i.seller_id, i.address, i.condition, i.timeline,"
         " (SELECT COUNT(*) FROM photos p WHERE p.inquiry_id = i.id AND p.is_home = 1) AS photos, i.updated_at FROM inquiries i"),
        ("CHANGE HISTORY", "SELECT inquiry_id, source, field, old_value, new_value, at FROM inquiry_events ORDER BY id"),
        ("INBOUND TEXTS (dedup log)", "SELECT webhook_id, phone, body, status, outcome FROM processed_messages ORDER BY received_at"),
        ("OUTBOUND TEXTS", "SELECT id, inquiry_id, to_number, purpose, status, error, substr(replace(body, char(10), ' | '), 1, 60) AS body FROM outbound_messages ORDER BY id"),
    ]:
        rows = conn.execute(query).fetchall()
        print(f"\n== {title} ({len(rows)})")
        for r in rows:
            print("  " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
