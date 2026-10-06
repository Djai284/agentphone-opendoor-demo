"""Outbound SMS through AgentPhone, with honest delivery reporting.

`send_sms` never raises: it records the attempt, calls AgentPhone, and returns a
SendResult the caller can relay to the homeowner truthfully.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

from . import db

AGENTPHONE_API = "https://api.agentphone.ai/v1"
FAILED_STATUSES = {"failed", "undelivered", "rejected", "error"}
MAX_PER_MINUTE = 5  # loop/flood guard per destination


@dataclass
class SendResult:
    ok: bool
    status: str
    error: str | None = None
    message_id: str | None = None


def is_textable_us_number(number: str) -> bool:
    """Real US/Canada (NANP) numbers only. Rejects carrier error senders like +1121611611 and 555 test numbers."""
    return bool(re.fullmatch(r"\+1[2-9]\d{2}[2-9]\d{6}", number)) and number[5:8] != "555"


def public_url(path: str) -> str | None:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{path}" if base else None


def listing_url(inquiry: dict) -> str | None:
    return public_url(f"/l/{inquiry['listing_token']}")


def portal_url(inquiry_or_seller: dict) -> str | None:
    return public_url(f"/s/{inquiry_or_seller['portal_token']}")


def short_address(inquiry: dict) -> str:
    """'400 West 59th Street, New York, NY' -> '400 West 59th Street'."""
    return (inquiry.get("address") or "your new home").split(",")[0].strip()


def photo_url(inquiry: dict, photo: dict) -> str | None:
    return public_url(f"/media/{inquiry['listing_token']}/{photo['filename']}")


def format_summary(inquiry: dict, photos: list[dict] | None = None, home_count: int = 1) -> str:
    def val(field: str) -> str:
        return inquiry.get(field) or "still need this"

    photos = photos or []
    first = (inquiry.get("name") or "").split(" ")[0].lower()
    link = listing_url(inquiry)
    lines = [
        f"hey{' ' + first if first else ''}! here's what i've got for "
        + (short_address(inquiry) if home_count > 1 else "your home sale") + ":\n",
        f"name: {val('name')}",
        f"address: {val('address')}",
        f"condition: {val('condition')}",
        f"timeline: {val('timeline')}",
        f"photos: {len(photos)}" + (f" ({', '.join(sorted({p['room'] for p in photos if p.get('room')}))})" if photos else ""),
    ]
    if link:
        lines.append(f"\nyour listing: {link}")
    if home_count > 1 and (portal := portal_url(inquiry)):
        lines.append(f"all {home_count} of your homes: {portal}")
    if len(photos) < 3:
        lines.append("\ntext me a few pics (kitchen, living room, outside) and i'll add them to your listing 📸")
    lines.append("anything off? just text me the fix. or text \"call me\" and i'll ring you")
    return "\n".join(lines)


async def send_sms(
    to_number: str, body: str, *, inquiry_id: int | None, purpose: str, media_urls: list[str] | None = None
) -> SendResult:
    if not is_textable_us_number(to_number):
        return SendResult(ok=False, status="skipped", error=f"{to_number} is not a textable US number")
    if db.recent_outbound_count(to_number, 60) >= MAX_PER_MINUTE:
        return SendResult(ok=False, status="skipped", error="rate limit: too many texts to this number in the last minute")

    outbound_id = db.record_outbound(inquiry_id, to_number, body, purpose)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{AGENTPHONE_API}/messages",
                headers={"Authorization": f"Bearer {os.environ['AGENTPHONE_API_KEY']}"},
                json={
                    "number_id": os.environ["AGENTPHONE_NUMBER_ID"],
                    "to_number": to_number,
                    "body": body,
                    **({"media_urls": media_urls} if media_urls else {}),
                },
            )
    except httpx.HTTPError as e:
        result = SendResult(ok=False, status="failed", error=f"network error reaching AgentPhone: {e}")
    else:
        if resp.is_success:
            data = resp.json()
            status = str(data.get("status", "")).lower()
            if status in FAILED_STATUSES:
                result = SendResult(False, "failed", f"AgentPhone reported status '{status}'", data.get("id"))
            else:
                result = SendResult(True, "sent", None, data.get("id"))
        else:
            try:
                detail = resp.json().get("detail") or resp.text
            except ValueError:
                detail = resp.text
            result = SendResult(False, "failed", f"AgentPhone returned {resp.status_code}: {detail}")

    db.mark_outbound(outbound_id, result.status, result.error, result.message_id)
    return result


async def send_summary(phone: str, inquiry: dict) -> SendResult:
    """Text the inquiry summary, with the cover photo attached (MMS) once there is one."""
    photos = db.list_photos(inquiry["id"])
    cover = photo_url(inquiry, photos[0]) if photos else None
    return await send_sms(
        phone,
        format_summary(inquiry, photos, home_count=len(db.list_homes(inquiry["seller_id"]))),
        inquiry_id=inquiry["id"],
        purpose="summary",
        media_urls=[cover] if cover else None,
    )
