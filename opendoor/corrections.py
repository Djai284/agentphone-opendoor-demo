"""Interpret a homeowner's free-form text about their home(s).

The LLM (LiveKit Inference) classifies the text, picks which home it's about,
and extracts structured changes. Code validates the result and decides what to
store; summaries and update confirmations are built from the database, not from
LLM prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from livekit.agents import inference, llm

from .db import FIELDS

MODEL = "openai/gpt-4.1-mini"
INTENTS = {"update", "summary", "question", "call_me", "new_home", "select_home", "other"}

PROMPT = """You are Olivia, Opendoor's texting assistant. A homeowner is texting about selling their home(s) to Opendoor.

Their homes on file, numbered (null means not collected yet; "active" is the home the conversation is currently about):
{homes}

{pending}Their text:
"{message}"

Reply with JSON only, no prose:
{{"intent": "...", "home": <number or null>, "changes": {{}}, "reply": "..."}}

intent is one of:
- "update": they're changing/correcting name, address, condition, or timeline of a home. Put new values in "changes".
- "summary": they want details/summary/listing link sent or shown again.
- "question": they're asking about their home(s) or about selling.
- "call_me": they want a phone call.
- "new_home": they want to sell an additional/different home not in the list. Put any details they gave in "changes".
- "select_home": they're only saying which home they mean (e.g. "the brooklyn one", "2", "these pics are for bergen st").
- "other": anything else (thanks, greetings, unclear).

home: the number of the home the text is about, ONLY if the text clearly identifies it (by number, street, neighborhood, or
city) or there is only one home. Otherwise null. Never guess between multiple homes.

changes (for "update" and "new_home"): keys from name, address, condition, timeline. Only fields the text clearly gives.
- timeline: phrased naturally (e.g. "December", "within 3 months", "ASAP").
- address: always a full address. If they only give a new street for an existing home, keep its city/state.
- name is the person's name and applies to all their homes.

reply (only for "question" and "other", otherwise ""): a short text back, written casually in all lowercase like a friend
texting, 1-2 sentences, no markdown.
- Only state facts that appear in the homes on file. If something isn't on file, say you don't have it yet.
- If they have several homes and the question is about one, say which home (by street) you're talking about.
- You can't quote prices or offers; say an opendoor specialist will follow up with an offer.
- Next steps if asked: once we have all the details and a few photos, an opendoor specialist reviews the home and follows
  up with an offer.
- For unclear texts, say you can update their details, resend their summary, add another home, or call them."""


@dataclass
class Interpretation:
    intent: str
    home: int | None = None  # 1-based index into the homes list
    changes: dict[str, str] = field(default_factory=dict)
    reply: str = ""


def describe_homes(homes: list[dict], photos_by_home: dict[int, list[dict]]) -> str:
    lines = []
    for n, h in enumerate(homes, start=1):
        info = {f: h.get(f) for f in FIELDS}
        info["photos"] = [p.get("room") or "photo" for p in photos_by_home.get(h["id"], [])]
        lines.append(f"{n}. {json.dumps(info)}{'  <- active' if h.get('is_active') else ''}")
    return "\n".join(lines) if lines else "(none yet)"


async def interpret_text(
    message: str,
    homes: list[dict],
    photos_by_home: dict[int, list[dict]] | None = None,
    *,
    with_photo: bool = False,
    pending: dict | None = None,
) -> Interpretation:
    pending_note = ""
    if pending:
        pending_note = (
            f"Context: we just asked them which home they meant for this earlier request: {json.dumps(pending)}. "
            "If this text answers that, use intent \"select_home\" with the home number.\n\n"
        )
    prompt = PROMPT.format(homes=describe_homes(homes, photos_by_home or {}), pending=pending_note, message=message)
    if with_photo:
        prompt += (
            "\n\nThis text arrived with a photo attached. The photo is handled separately, so if the text is just about "
            "the photo (e.g. \"add this to my listing\"), use intent \"other\" (or \"select_home\" if it says which home)."
        )
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content=prompt)

    model = inference.LLM(model=MODEL)
    text = ""
    try:
        async with model.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    text += chunk.delta.content
    finally:
        await model.aclose()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    intent = data.get("intent") if data.get("intent") in INTENTS else "other"
    home = data.get("home")
    home = home if isinstance(home, int) and 1 <= home <= len(homes) else None
    if len(homes) == 1 and intent != "new_home":
        home = 1
    raw_changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
    changes = {k: v.strip() for k, v in raw_changes.items() if k in FIELDS and isinstance(v, str) and v.strip()}
    if intent == "update" and not changes:
        intent = "select_home" if home else "other"
    reply = data.get("reply").strip() if isinstance(data.get("reply"), str) else ""
    return Interpretation(intent=intent, home=home, changes=changes, reply=reply)
