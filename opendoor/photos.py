"""Inbound MMS photos: download, validate, store, and tag with a vision model."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pillow_heif
from livekit.agents import inference, llm
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

log = logging.getLogger("photos")

MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
MAX_BYTES = 10 * 1024 * 1024
VISION_MODEL = "openai/gpt-4.1-mini"

# Formats the vision model and browsers both handle, identified by magic bytes (not the URL or headers).
SIGNATURES = {
    "jpg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
}

VISION_PROMPT = """This photo was texted by a homeowner who wants to sell their home to Opendoor.
Reply with JSON only:
{"is_home": true/false, "room": "...", "caption": "...", "notes": "..."}
- is_home: is this a photo of a home's interior, exterior, or yard?
- room: one or two lowercase words, e.g. "kitchen", "living room", "bathroom", "bedroom", "exterior", "backyard", "garage".
- caption: a short, neutral listing-style caption (max 8 words, sentence case).
- notes: brief condition observations a home inspector would care about (max 15 words), e.g. "updated cabinets, minor wear on floors"."""


class PhotoError(Exception):
    """A user-facing reason the photo couldn't be used."""


@dataclass
class StoredPhoto:
    filename: str
    path: Path
    kind: str


def sniff(data: bytes) -> str | None:
    for kind, sigs in SIGNATURES.items():
        if any(data.startswith(s) for s in sigs):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def is_heic(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


def heic_to_jpeg(data: bytes) -> bytes:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    image.thumbnail((2048, 2048))  # also keeps re-sent MMS attachments well under 5 MB
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return out.getvalue()


def is_mms_wrapper(data: bytes) -> bool:
    """Carrier MMS parts that aren't the photo: the SMIL layout file and text parts."""
    head = data[:200].lstrip().lower()
    return head.startswith(b"<smil") or head.startswith(b"<?xml") and b"<smil" in data[:500]


def describe_unsupported(data: bytes) -> str:
    head = data[:32]
    if b"ftyp" in head:
        return "a video"
    if data.startswith(b"%PDF"):
        return "a PDF"
    return "a file type i can't open"


async def download(url: str) -> bytes:
    headers = {}
    # Only ever send our API key to AgentPhone itself, never to third-party storage hosts.
    if urlparse(url).hostname == "api.agentphone.ai":
        headers["Authorization"] = f"Bearer {os.environ['AGENTPHONE_API_KEY']}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise PhotoError("that file is over 10 MB")
    return data


AGENTPHONE_MEDIA = re.compile(r"^https://api\.agentphone\.ai/v1/messages/([A-Za-z0-9_-]+)/media")
MAX_PARTS = 6


def candidate_urls(url: str) -> list[str]:
    """An MMS can hold several parts (SMIL layout, text, image...). For AgentPhone media URLs, try each part."""
    match = AGENTPHONE_MEDIA.match(url)
    if not match:
        return [url]
    base = f"https://api.agentphone.ai/v1/messages/{match.group(1)}/media"
    return [f"{base}?index={i}" for i in range(MAX_PARTS)]


async def fetch_image(url: str) -> tuple[bytes, str]:
    """Download the first usable image part. Returns (bytes, kind); HEIC is converted to JPEG."""
    reason = "i couldn't find a photo in that message"
    for candidate in candidate_urls(url):
        try:
            data = await download(candidate)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404 and candidate != url:
                break  # ran out of parts
            log.warning("media download failed for %s: %s", candidate, e)
            reason = "i couldn't download that one"
            continue
        except httpx.HTTPError as e:
            log.warning("media download failed for %s: %s", candidate, e)
            reason = "i couldn't download that one"
            continue
        if is_mms_wrapper(data):
            continue
        if kind := sniff(data):
            return data, kind
        if is_heic(data):
            try:
                return heic_to_jpeg(data), "jpg"
            except Exception:
                log.exception("HEIC conversion failed")
                reason = "i couldn't convert that iPhone photo"
                continue
        reason = f"that looks like {describe_unsupported(data)}"
    raise PhotoError(reason)


async def store(url: str, listing_token: str) -> StoredPhoto:
    data, kind = await fetch_image(url)
    folder = MEDIA_DIR / listing_token
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(6)}.{kind}"
    path = folder / filename
    path.write_bytes(data)
    return StoredPhoto(filename=filename, path=path, kind=kind)


async def analyze(photo: StoredPhoto) -> dict:
    mime = {"jpg": "image/jpeg"}.get(photo.kind, f"image/{photo.kind}")
    data_url = f"data:{mime};base64,{base64.b64encode(photo.path.read_bytes()).decode()}"
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content=[VISION_PROMPT, llm.ImageContent(image=data_url)])
    model = inference.LLM(model=VISION_MODEL)
    text = ""
    try:
        async with model.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    text += chunk.delta.content
    except Exception:
        log.exception("vision analysis failed; storing photo untagged")
        return {"is_home": True, "room": None, "caption": None, "notes": None}
    finally:
        await model.aclose()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        result = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        result = {}
    return {
        "is_home": bool(result.get("is_home", True)),
        "room": (result.get("room") or "").strip().lower() or None,
        "caption": (result.get("caption") or "").strip() or None,
        "notes": (result.get("notes") or "").strip() or None,
    }
