import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import sms_server
from conftest import SELLER
from opendoor import db
from opendoor.corrections import Interpretation



def seed_home(**fields):
    home = db.active_inquiry(SELLER)
    db.update_inquiry(home["id"], fields, source="call")
    return db.get_inquiry(home["id"])


# --- webhook endpoint: signature, filtering, duplicates -------------------------------


def signed_post(client, payload, webhook_id="del_1", secret="whsec_test"):
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/agentphone", content=raw, headers={
        "x-webhook-signature": sig, "x-webhook-timestamp": ts, "x-webhook-id": webhook_id,
        "content-type": "application/json",
    })


def inbound(text, sender=SELLER, media=None):
    data = {"from": sender, "to": "+14155560199", "message": text, "direction": "inbound",
            "receivedAt": "2026-10-06T18:00:00Z", "mediaUrl": media}
    return {"event": "agent.message", "channel": "sms", "data": data}


async def test_bad_signature_is_rejected(sent, llm):
    client = TestClient(sms_server.app)
    assert signed_post(client, inbound("hi"), secret="wrong").status_code == 401
    assert sent == []


async def test_duplicate_delivery_is_applied_once(sent, llm):
    seed_home(name="Jay", timeline="next month")
    llm["actually, december"] = Interpretation(intent="update", home=1, changes={"timeline": "December"})
    client = TestClient(sms_server.app)
    for _ in range(3):  # AgentPhone retries the same delivery
        assert signed_post(client, inbound("actually, december"), webhook_id="del_same").status_code == 200

    home = db.active_inquiry(SELLER)
    assert home["timeline"] == "December"
    assert len([e for e in db.list_events(home["id"]) if e["source"] == "sms"]) == 1  # applied once, not three times
    assert [p for _, p, _, _ in sent] == ["confirmation"]


async def test_carrier_bounces_are_ignored(sent, llm):
    client = TestClient(sms_server.app)
    signed_post(client, inbound("Error Invalid Number", sender="+1121611611"))
    assert sent == []  # the reply loop from the first demo can't happen


# --- text handling ------------------------------------------------------------------


async def test_correction_updates_same_inquiry_and_confirms(sent, llm):
    home = seed_home(name="Jay", address="1 Main St, Austin, TX", condition="good", timeline="spring")
    llm["sell in december"] = Interpretation(intent="update", home=1, changes={"timeline": "December"})
    await sms_server.handle_text("wh_1", SELLER, "sell in december")
    assert db.get_inquiry(home["id"])["timeline"] == "December"
    assert "timeline: December (was spring)" in sent[0][2]


async def test_summary_includes_listing_link(sent, llm):
    seed_home(name="Jay", address="1 Main St, Austin, TX")
    llm["text me the details again"] = Interpretation(intent="summary", home=1)
    await sms_server.handle_text("wh_1", SELLER, "text me the details again")
    to, purpose, body, _ = sent[0]
    assert purpose == "summary" and "address: 1 Main St, Austin, TX" in body
    assert "https://demo.example.test/l/" in body


async def test_unknown_number_gets_onboarding_reply(sent, llm):
    await sms_server.handle_text("wh_1", "+13105550142".replace("555", "558"), "hello")
    assert sent[0][1] == "no_inquiry"


async def test_call_me_starts_outbound_call_for_active_home(sent, llm, calls):
    home = seed_home(name="Jay")
    await sms_server.handle_text("wh_1", SELLER, "CALL ME")
    assert calls == [(SELLER, home["id"])]
    assert sent[0][1] == "call_me_ack"


# --- multiple homes -------------------------------------------------------------------


async def test_new_home_then_fill_blanks_goes_to_new_home(sent, llm):
    first = seed_home(name="Jay", address="1 Main St, Austin, TX", condition="good", timeline="spring")
    llm["also selling 12 bergen st"] = Interpretation(intent="new_home", changes={"address": "12 Bergen St, Brooklyn, NY"})
    llm["roof is old"] = Interpretation(intent="update", changes={"condition": "roof is old"})  # no home named

    await sms_server.handle_text("wh_1", SELLER, "also selling 12 bergen st")
    await sms_server.handle_text("wh_2", SELLER, "roof is old")

    homes = db.list_homes(first["seller_id"])
    assert len(homes) == 2
    assert homes[0]["condition"] == "good"  # untouched
    assert homes[1]["condition"] == "roof is old"  # the only home missing a condition
    assert sent[0][1] == "new_home" and "/s/" in sent[0][2]  # links to "your homes"


async def test_ambiguous_update_asks_which_home_then_applies(sent, llm):
    first = seed_home(name="Jay", address="1 Main St, Austin, TX", timeline="spring")
    second = db.create_inquiry(first["seller_id"])
    db.update_inquiry(second["id"], {"address": "12 Bergen St, Brooklyn, NY", "timeline": "March"}, source="sms")
    llm["change the timeline to april"] = Interpretation(intent="update", changes={"timeline": "April"})

    await sms_server.handle_text("wh_1", SELLER, "change the timeline to april")
    assert sent[-1][1] == "which_home" and "1. 1 Main St" in sent[-1][2]
    assert db.get_inquiry(first["id"])["timeline"] == "spring"  # nothing applied yet

    await sms_server.handle_text("wh_2", SELLER, "1")  # answered without an LLM call
    assert db.get_inquiry(first["id"])["timeline"] == "April"
    assert db.get_inquiry(second["id"])["timeline"] == "March"
    assert db.get_seller(SELLER)["pending"] is None


async def test_photo_with_text_naming_a_home_goes_to_that_home(sent, llm, vision, monkeypatch):
    from conftest import jpeg_bytes
    from opendoor import photos

    first = seed_home(name="Jay", address="1 Main St, Austin, TX")
    second = db.create_inquiry(first["seller_id"])
    db.update_inquiry(second["id"], {"address": "12 Bergen St, Brooklyn, NY"}, source="sms")
    db.set_active(first["seller_id"], first["id"])
    llm["these are for bergen"] = Interpretation(intent="select_home", home=2)

    async def fake_download(url):
        return jpeg_bytes()

    monkeypatch.setattr(photos, "download", fake_download)
    await sms_server.process_message("wh_1", SELLER, "these are for bergen", "https://example.test/p.jpg")
    await __import__("asyncio").sleep(0.05)  # let the batched photo reply fire

    assert len(db.list_photos(second["id"])) == 1 and db.list_photos(first["id"]) == []
    assert [p for _, p, _, _ in sent] == ["photo_ack"]
    assert "for 12 Bergen St" in sent[0][2]
