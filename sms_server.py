"""AgentPhone SMS webhook (corrections, photos, CALL ME) plus the mock listing pages.

Run: uv run uvicorn sms_server:app --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import logging
import os
import time

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response

load_dotenv(".env")

from opendoor import db, photos  # noqa: E402
from opendoor.calls import start_outbound_call  # noqa: E402
from opendoor.corrections import Interpretation, interpret_text  # noqa: E402
from opendoor.messaging import (  # noqa: E402
    is_textable_us_number, listing_url, portal_url, send_sms, send_summary, short_address,
)
from opendoor.web import router as web_router  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sms")

app = FastAPI()
app.include_router(web_router)
db.init()

PHOTO_BATCH_SECONDS = 6  # wait for the rest of a multi-photo send before replying once

CALL_ME = {"call me", "callme", "call me please", "please call me"}
FIELD_LABELS = {"name": "name", "address": "address", "condition": "condition", "timeline": "timeline"}


def verify_signature(raw: bytes, signature: str | None, timestamp: str | None) -> bool:
    secret = os.environ.get("OPENDOOR_WEBHOOK_SECRET")
    if not secret:
        log.warning("OPENDOOR_WEBHOOK_SECRET not set; skipping signature check")
        return True
    if not signature or not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@app.post("/webhooks/agentphone")
async def agentphone_webhook(request: Request, background: BackgroundTasks) -> Response:
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("x-webhook-signature"), request.headers.get("x-webhook-timestamp")):
        log.warning("rejected webhook with bad signature")
        return Response(status_code=401)

    event = json.loads(raw)
    data = event.get("data") or {}
    if event.get("event") != "agent.message" or data.get("direction") != "inbound" or event.get("channel") == "voice":
        return Response(status_code=200)

    phone, body = data.get("from", ""), (data.get("message") or "").strip()
    media_url = data.get("mediaUrl") or next(iter(data.get("mediaUrls") or []), None)
    content_key = hashlib.sha256(f"{phone}|{data.get('receivedAt')}|{body}|{media_url}".encode()).hexdigest()
    webhook_id = request.headers.get("x-webhook-id") or content_key
    log.info("<- %s: %r%s  (delivery %s)", phone, body, f" + media {media_url}" if media_url else "", webhook_id)

    if not is_textable_us_number(phone):
        log.info("   ignored: %s is not a real US number (carrier bounce?)", phone)
        return Response(status_code=200)
    if not db.claim_message(webhook_id, content_key, phone, body):
        log.info("   duplicate delivery, already processed; ignoring")
        return Response(status_code=200)

    # Acknowledge immediately so AgentPhone doesn't time out and retry; do the work after responding.
    background.add_task(process_message, webhook_id, phone, body, media_url)
    return Response(status_code=200)


async def process_message(webhook_id: str, phone: str, body: str, media_url: str | None) -> None:
    try:
        parts = []
        # Text first, so "these are for the brooklyn place" routes the photo that came with it.
        if body:
            parts.append("text: " + await handle_text(webhook_id, phone, body, with_photo=bool(media_url)))
        if media_url:
            parts.append(await handle_photo(webhook_id, phone, media_url))
        outcome = " | ".join(parts) or "empty message"
    except Exception:
        log.exception("   failed to process message; releasing it so a retry can reprocess")
        db.release_message(webhook_id)
        return
    db.finish_message(webhook_id, outcome)
    log.info("   %s", outcome)


def home_label(home: dict) -> str:
    return short_address(home) if home.get("address") else "your new home"


def which_home_question(homes: list[dict], lead: str) -> str:
    options = "\n".join(f"{n}. {home_label(h)}" for n, h in enumerate(homes, start=1))
    return f"{lead} which home is this for?\n{options}\n\njust reply with the number"


async def reply(phone: str, text: str, inquiry_id: int | None, purpose: str) -> str:
    sent = await send_sms(phone, text, inquiry_id=inquiry_id, purpose=purpose)
    if not sent.ok:
        log.error("   REPLY NOT SENT to %s (%s): %s", phone, purpose, sent.error)
    return f"{purpose} {sent.status} {sent.error or ''}".strip()


async def handle_text(webhook_id: str, phone: str, body: str, with_photo: bool = False) -> str:
    if body.lower().strip(" .!") in CALL_ME:
        return await call_back(phone)

    seller = db.get_seller(phone)
    homes = db.list_homes(seller["id"]) if seller else []
    pending = seller["pending"] if seller else None

    choice = body.strip().rstrip(".")
    if pending and choice.isdigit() and 1 <= int(choice) <= len(homes):
        result = Interpretation(intent="select_home", home=int(choice))  # "2" answering "which home?"
    else:
        photos_by_home = {h["id"]: db.list_photos(h["id"]) for h in homes}
        result = await interpret_text(body, homes, photos_by_home, with_photo=with_photo, pending=pending)
    target = homes[result.home - 1] if result.home else None
    log.info("   interpreted as %s home=%s %s", result.intent, target and home_label(target), result.changes or "")

    if not homes and result.intent != "new_home":
        if result.intent == "update" and result.changes.get("address"):
            result.intent = "new_home"  # a stranger texting an address wants to start a sale
        else:
            text = (
                "hey! i'm olivia from opendoor. looks like we haven't chatted about your home yet. "
                f"call me at {os.environ['AGENTPHONE_NUMBER']}, text \"call me\", or just text me the address"
            )
            return await reply(phone, text, None, "no_inquiry")

    if target:
        db.set_active(seller["id"], target["id"])

    if result.intent == "new_home":
        seller = seller or db.get_or_create_seller(phone)
        home = db.create_inquiry(seller["id"])
        db.update_inquiry(home["id"], result.changes, source="sms", message_key=webhook_id)
        home = db.get_inquiry(home["id"])
        db.set_pending(seller["id"], None)
        missing = [f for f in ("address", "condition", "timeline") if not home.get(f)]
        text = f"nice, added {home_label(home)} 🏡" + (" as another home" if homes else "")
        if missing:
            text += f"\nwhat's the {missing[0]}?" if missing[0] != "condition" else "\nwhat condition is it in?"
        text += "\nyou can text me pics of it too"
        if len(homes) >= 1 and (portal := portal_url(home)):
            text += f"\n\nall your homes: {portal}"
        return await reply(phone, text, home["id"], "new_home")

    if result.intent == "select_home":
        if pending and target:
            db.set_pending(seller["id"], None)
            return await run_pending(webhook_id, phone, pending, target)
        if with_photo:
            return f"photo is for {target and home_label(target)}"
        if target:
            return await reply(phone, f"got it, we're talking about {home_label(target)} now 👍 what's up?", target["id"], "select_home")
        return await reply(phone, which_home_question(homes, "sure!"), None, "which_home")

    if with_photo and result.intent in ("question", "other"):
        # e.g. "here's the kitchen" / "add this to my listing": the photo reply already answers it.
        return f"{result.intent} text sent with a photo; covered by the photo reply"

    if result.intent == "call_me":
        return await call_back(phone)

    if result.intent == "summary":
        if target:
            return await summary_for(phone, target)
        lines = [f"{n}. {home_label(h)}: {h.get('timeline') or 'timeline tbd'}" for n, h in enumerate(homes, start=1)]
        text = f"you've got {len(homes)} homes with us:\n" + "\n".join(lines)
        if portal := portal_url(homes[0]):
            text += f"\n\nsee them all: {portal}"
        return await reply(phone, text + "\nwant the full details for one? just say which", None, "summary_overview")

    if result.intent == "update":
        target = target or guess_home_for_blanks(homes, result.changes)
        if not target and set(result.changes) <= set(db.SELLER_FIELDS):
            target = db.get_inquiry(seller["active_inquiry_id"]) or homes[-1]  # a name change applies to every home
        if not target:
            db.set_pending(seller["id"], {"type": "update", "changes": result.changes, "message_key": webhook_id})
            return await reply(phone, which_home_question(homes, "happy to update that!"), None, "which_home")
        return await apply_update(webhook_id, phone, target, result.changes)

    # question / other: the LLM's short answer, grounded in the homes on file.
    text = result.reply or "i can update your details, resend your summary, add another home, or give you a call. what would you like?"
    return await reply(phone, text, target and target["id"], result.intent)


def guess_home_for_blanks(homes: list[dict], changes: dict) -> dict | None:
    """Filling in a field only one home is missing (e.g. the new home's timeline) is unambiguous."""
    fields = [f for f in changes if f not in db.SELLER_FIELDS]
    if not fields:
        return None
    candidates = [h for h in homes if all(not h.get(f) for f in fields)]
    return candidates[0] if len(candidates) == 1 else None


async def run_pending(webhook_id: str, phone: str, pending: dict, target: dict) -> str:
    if pending.get("type") == "update":
        return await apply_update(pending.get("message_key") or webhook_id, phone, target, pending["changes"])
    return await summary_for(phone, target)


async def apply_update(message_key: str, phone: str, target: dict, changes: dict) -> str:
    applied = db.update_inquiry(target["id"], changes, source="sms", message_key=message_key)
    many = len(db.list_homes(target["seller_id"])) > 1
    where = f" for {home_label(db.get_inquiry(target['id']))}" if many else ""
    if not applied:
        lines = [f"{FIELD_LABELS[f]} is already {target[f]}" for f in changes]
        text = "all good, " + " and ".join(lines) + where + ". nothing to change 👍"
    else:
        lines = [f"{FIELD_LABELS[f]}: {new}" + (f" (was {old})" if old else "") for f, (old, new) in applied.items()]
        text = f"done ✅ updated{where}:\n" + "\n".join(lines) + "\n\nanything else, just text me"
    return f"applied {applied or 'nothing'} to home {target['id']}; " + await reply(phone, text, target["id"], "confirmation")


async def summary_for(phone: str, home: dict) -> str:
    sent = await send_summary(phone, db.get_inquiry(home["id"]))
    return f"summary for home {home['id']} {sent.status} {sent.error or ''}".strip()


async def call_back(phone: str) -> str:
    inquiry = db.active_inquiry(phone)
    room = await start_outbound_call(phone, inquiry["id"])
    sent = await send_sms(phone, "on it, calling you now 📞", inquiry_id=inquiry["id"], purpose="call_me_ack")
    return f"CALL ME: dispatched agent to {room}; ack text {sent.status} {sent.error or ''}".strip()


# --- photos --------------------------------------------------------------------

# phone -> pending results and the timer that sends one combined reply
_photo_batches: dict[str, dict] = {}


async def handle_photo(webhook_id: str, phone: str, media_url: str) -> str:
    inquiry = db.active_inquiry(phone)  # the home the conversation is about (set by "these are for ...")
    try:
        stored = await photos.store(media_url, inquiry["listing_token"])
    except photos.PhotoError as e:
        _queue_photo_result(phone, {"error": str(e), "inquiry_id": inquiry["id"]})
        return f"photo rejected: {e}"
    analysis = await photos.analyze(stored)
    db.add_photo(inquiry["id"], stored.filename, media_url, webhook_id, analysis)
    _queue_photo_result(phone, {**analysis, "inquiry_id": inquiry["id"]})
    return f"photo saved to home {inquiry['id']}: {stored.filename} {analysis}"


def _queue_photo_result(phone: str, result: dict) -> None:
    batch = _photo_batches.setdefault(phone, {"results": [], "timer": None})
    batch["results"].append(result)
    if batch["timer"]:
        batch["timer"].cancel()
    batch["timer"] = asyncio.create_task(_send_photo_reply_later(phone))


async def _send_photo_reply_later(phone: str) -> None:
    await asyncio.sleep(PHOTO_BATCH_SECONDS)
    batch = _photo_batches.pop(phone, None)
    if not batch:
        return
    inquiry = db.get_inquiry(batch["results"][-1]["inquiry_id"])
    many = len(db.list_homes(inquiry["seller_id"])) > 1
    text = photo_reply(inquiry, batch["results"], db.list_photos(inquiry["id"]), name_home=many)
    sent = await send_sms(phone, text, inquiry_id=inquiry["id"], purpose="photo_ack")
    log.info("   photo reply for %d photo(s) %s %s", len(batch["results"]), sent.status, sent.error or "")


def photo_reply(inquiry: dict, results: list[dict], all_photos: list[dict], name_home: bool = False) -> str:
    added = [r for r in results if "error" not in r and r.get("is_home", True)]
    not_home = [r for r in results if "error" not in r and not r.get("is_home", True)]
    errors = [r["error"] for r in results if "error" in r]

    parts = []
    if added:
        rooms = sorted({r["room"] for r in added if r.get("room")})
        count = f"{len(added)} pic{'s' if len(added) > 1 else ''}"
        where = f" for {home_label(inquiry)}" if name_home else ""
        parts.append(f"got {count}{where} 📸" + (f" ({', '.join(rooms)})" if rooms else ""))
        link = listing_url(inquiry)
        if link:
            parts.append(f"they're live on your listing: {link}")
    if not_home:
        parts.append(f"{'one' if len(not_home) == 1 else len(not_home)} didn't look like part of your home so i left it off")
    if errors:
        parts.append(f"couldn't use {'one' if len(errors) == 1 else len(errors)}: {errors[0]}. can you send it as a regular photo?")

    have = {p["room"] for p in all_photos if p.get("room")}
    wanted = [r for r in ("kitchen", "living room", "exterior") if r not in have]
    if added and wanted:
        parts.append(f"if you can, grab one of the {wanted[0]} too")
    missing = [f for f in db.FIELDS if not inquiry.get(f)]
    if missing:
        parts.append(f"still need the {' and '.join(missing)} btw")
    return "\n".join(parts) if parts else "hmm, nothing came through. mind sending that again?"
