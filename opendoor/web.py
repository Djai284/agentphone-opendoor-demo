"""Mock Opendoor web app, served next to the SMS webhook. Every page polls its JSON every 2s, so calls
and texts show up live.

/                      ops pipeline: every seller and home (local machine only, never through the tunnel)
/s/<portal_token>      a seller's "your homes" page (texted to them)
/l/<listing_token>     one home's listing page
/media/<token>/<file>  stored photos (public so MMS can attach them)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import db
from .photos import MEDIA_DIR

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
FIELD_LABELS = {"name": "Name", "address": "Address", "condition": "Condition", "timeline": "Timeline"}
STAGES = ["Collecting details", "Needs photos", "Specialist review", "Offer sent"]
PHOTOS_NEEDED = 3


def _page(name: str, token: str = "") -> str:
    return (TEMPLATES / name).read_text().replace("__TOKEN__", token)


def _local_only(request: Request) -> None:
    # The pipeline shows every caller's phone number: only answer requests made on this machine, not via the tunnel.
    if request.headers.get("cf-connecting-ip") or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404)


def _format_number(e164: str) -> str:
    digits = (e164 or "").removeprefix("+1")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else e164


def _short(home: dict) -> str:
    return (home.get("address") or "New home · address pending").split(",")[0].strip()


def _stage(home: dict, photo_count: int) -> int:
    if not all(home.get(f) for f in db.FIELDS):
        return 0
    if photo_count < PHOTOS_NEEDED:
        return 1
    return 2  # offers are made by a specialist; nothing here moves a home to "Offer sent"


def _home_card(home: dict, include_contact: bool = False) -> dict:
    photos = db.list_photos(home["id"])
    stage = _stage(home, len(photos))
    card = {
        "id": home["id"],
        "short": _short(home),
        "address": home.get("address"),
        "condition": home.get("condition"),
        "timeline": home.get("timeline"),
        "photos": len(photos),
        "cover": f"/media/{home['listing_token']}/{photos[0]['filename']}" if photos else None,
        "stage": stage,
        "stage_label": STAGES[stage],
        "missing": [FIELD_LABELS[f].lower() for f in db.FIELDS if not home.get(f)],
        "url": f"/l/{home['listing_token']}",
        "updated_at": home["updated_at"],
        "is_active": bool(home.get("is_active")),
    }
    if include_contact:
        card |= {"seller_id": home["seller_id"], "seller": home.get("name") or "Unknown seller",
                 "phone": _format_number(home["phone"]), "portal": f"/s/{home['portal_token']}"}
    return card


# --- ops pipeline (local only) ---------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def pipeline_page(request: Request) -> str:
    _local_only(request)
    return _page("pipeline.html")


@router.get("/api/pipeline")
async def pipeline_data(request: Request) -> dict:
    _local_only(request)
    homes = [_home_card(h, include_contact=True) for h in db.list_all_inquiries()]
    sellers = [
        {"id": s["id"], "name": s["name"] or "Unknown seller", "phone": _format_number(s["phone"]),
         "homes": sum(1 for h in homes if h["seller_id"] == s["id"]), "portal": f"/s/{s['portal_token']}"}
        for s in db.list_sellers()
    ]
    return {
        "stages": STAGES,
        "stats": {
            "sellers": len(sellers),
            "homes": len(homes),
            "photos": sum(h["photos"] for h in homes),
            "ready": sum(1 for h in homes if h["stage"] >= 2),
        },
        "sellers": sellers,
        "homes": homes,
    }


# --- seller portal ----------------------------------------------------------------


@router.get("/s/{token}", response_class=HTMLResponse)
async def portal_page(token: str) -> str:
    _seller_or_404(token)
    return _page("portal.html", token)


@router.get("/s/{token}/data")
async def portal_data(token: str) -> dict:
    seller = _seller_or_404(token)
    return {
        "first_name": (seller.get("name") or "").split(" ")[0] or None,
        "agent_number": _format_number(os.environ.get("AGENTPHONE_NUMBER", "")),
        "stages": STAGES,
        "homes": [_home_card(h) for h in db.list_homes(seller["id"])],
    }


def _seller_or_404(token: str) -> dict:
    seller = db.get_seller_by_token(token) if TOKEN_RE.match(token) else None
    if not seller:
        raise HTTPException(status_code=404)
    return seller


# --- listing ----------------------------------------------------------------------


@router.get("/l/{token}", response_class=HTMLResponse)
async def listing_page(token: str) -> str:
    _inquiry_or_404(token)
    return _page("listing.html", token)


@router.get("/l/{token}/data")
async def listing_data(token: str) -> dict:
    inquiry = _inquiry_or_404(token)
    photos = db.list_photos(inquiry["id"])
    activity = [
        {
            "text": f"{FIELD_LABELS.get(e['field'], e['field'])} {'updated to' if e['old_value'] else 'set to'} {e['new_value']}",
            "via": "via text" if e["source"] == "sms" else "on a call",
            "at": e["at"],
        }
        for e in db.list_events(inquiry["id"])
    ]
    activity += [
        {"text": f"Photo added: {p['room'] or 'home photo'}", "via": "via text", "at": p["created_at"]} for p in photos
    ]
    activity.sort(key=lambda a: a["at"], reverse=True)
    stage = _stage(inquiry, len(photos))
    siblings = db.list_homes(inquiry["seller_id"])
    return {
        # Public page: no phone number or full name, just what a listing would show.
        "first_name": (inquiry.get("name") or "").split(" ")[0] or None,
        "address": inquiry.get("address"),
        "short": _short(inquiry),
        "condition": inquiry.get("condition"),
        "timeline": inquiry.get("timeline"),
        "agent_number": _format_number(os.environ.get("AGENTPHONE_NUMBER", "")),
        "stage": stage,
        "stages": STAGES,
        "photos_needed": PHOTOS_NEEDED,
        "missing": [FIELD_LABELS[f].lower() for f in db.FIELDS if not inquiry.get(f)],
        "photos": [
            {"url": f"/media/{token}/{p['filename']}", "room": p["room"], "caption": p["caption"], "notes": p["notes"]}
            for p in photos
        ],
        "activity": activity[:12],
        "portal": f"/s/{inquiry['portal_token']}",
        "homes": [{"short": _short(h), "url": f"/l/{h['listing_token']}", "current": h["id"] == inquiry["id"]} for h in siblings],
    }


def _inquiry_or_404(token: str) -> dict:
    inquiry = db.get_inquiry_by_token(token) if TOKEN_RE.match(token) else None
    if not inquiry:
        raise HTTPException(status_code=404)
    return inquiry


# --- static ------------------------------------------------------------------------


@router.get("/static/app.css")
async def stylesheet() -> FileResponse:
    return FileResponse(TEMPLATES / "app.css", media_type="text/css")


@router.get("/media/{token}/{filename}")
async def media(token: str, filename: str) -> FileResponse:
    if not TOKEN_RE.match(token) or not re.fullmatch(r"[0-9a-f]{12}\.(jpg|png|gif|webp)", filename):
        raise HTTPException(status_code=404)
    path = MEDIA_DIR / token / filename
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)
