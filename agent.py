"""Opendoor seller-intake voice agent (LiveKit Agents + LiveKit Inference).

Inbound: AgentPhone -> SIP trunk -> LiveKit dispatch rule -> this agent.
Outbound: the SMS server dispatches this agent with {"dial": "+1..."} metadata
and the agent places the call through LiveKit's outbound trunk to AgentPhone.
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from opendoor import db
from opendoor.messaging import send_sms, send_summary

load_dotenv(".env")
logger = logging.getLogger("opendoor-agent")

AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "opendoor-agent")

INSTRUCTIONS = """You are Olivia, a friendly phone assistant for Opendoor, helping homeowners start selling their home(s) to Opendoor.

For each home, collect these, one at a time, in a natural conversation:
1. The homeowner's full name (once; it applies to all their homes)
2. The property address (street, city, state)
3. The home's condition (for example: move-in ready, needs minor repairs, needs major work; note anything notable like a new roof or an old kitchen)
4. Their desired selling timeline (for example: ASAP, within 3 months, in December)

Homes:
- A homeowner can sell more than one home. You always work on one "current" home at a time; save_details writes to it.
- If they have several homes and it's unclear which one they mean, ask, then call select_home. Use list_homes if you need the numbers.
- If they want to sell another home that isn't on file, call add_new_home, then collect its address, condition, and timeline.
- When talking about a specific home, refer to it by its street so they know which one.

Rules:
- As soon as you learn or the caller corrects any detail, call save_details with just the fields you learned. Don't wait until the end.
- Confirm an address by reading it back once.
- If the caller asks what we have on file or about any detail, call get_inquiry and answer from it. Never guess.
- If the caller asks you to text them the details (including "again"), call text_details and tell them exactly what it reports. Never say a text was sent unless the tool says it was sent.
- This is a phone call: keep each reply to one or two short sentences, no lists or formatting, and ask one question at a time.
- Photos: homeowners text photos to this same number and they appear on the current home's listing page. Near the end, ask them to text a few photos (kitchen, living room, exterior) and offer to text them their listing link (text_details sends it).
- You can't give an offer price on this call. If asked, say an Opendoor specialist will follow up with an offer.
- When everything is collected, briefly recap, offer to text the details, and say goodbye."""


def home_line(n: int, home: dict, current_id: int) -> str:
    details = {f: home.get(f) for f in db.HOME_FIELDS}
    return f"home {n}{' (current)' if home['id'] == current_id else ''}: {json.dumps(details)}"


class SellerIntakeAgent(Agent):
    def __init__(self, phone: str, seller: dict, current: dict) -> None:
        homes = db.list_homes(seller["id"])
        known_homes = [h for h in homes if any(h.get(f) for f in db.HOME_FIELDS)]
        if not known_homes and not seller.get("name"):
            context = "\n\nThis is a new caller; we have no details yet."
        else:
            listing = "\n".join(home_line(n, h, current["id"]) for n, h in enumerate(homes, start=1))
            context = (
                f"\n\nThis caller is {seller.get('name') or 'name not collected yet'}. Their homes on file:\n{listing}\n"
                + (
                    "They have more than one home: greet them by name and ask which home they're calling about "
                    "(or whether it's a new one) before changing anything."
                    if len(known_homes) > 1
                    else "Greet them by name, mention what you have, and only ask for what's missing or changed."
                )
            )
        super().__init__(instructions=INSTRUCTIONS + context)
        self.phone = phone
        self.seller_id = seller["id"]
        self.inquiry_id = current["id"]

    def _homes(self) -> list[dict]:
        return db.list_homes(self.seller_id)

    @function_tool()
    async def save_details(
        self,
        context: RunContext,
        name: str | None = None,
        address: str | None = None,
        condition: str | None = None,
        timeline: str | None = None,
    ) -> str:
        """Save details the homeowner just gave or corrected for the current home. Pass only the fields you learned.

        Args:
            name: Homeowner's full name.
            address: Property address including city and state.
            condition: Home condition in a short phrase.
            timeline: When they want to sell, as they said it (e.g. "in December", "within 3 months").
        """
        changes = {"name": name, "address": address, "condition": condition, "timeline": timeline}
        applied = db.update_inquiry(self.inquiry_id, {k: v for k, v in changes.items() if v}, source="call")
        inquiry = db.get_inquiry(self.inquiry_id)
        logger.info("saved %s on home %s for %s", list(applied), self.inquiry_id, self.phone)
        missing = [f for f in db.FIELDS if not inquiry.get(f)]
        return f"Saved. Still missing: {', '.join(missing)}." if missing else "Saved. All details for this home are collected."

    @function_tool()
    async def list_homes(self, context: RunContext) -> str:
        """List all of this homeowner's homes with their numbers, marking the current one."""
        return "\n".join(home_line(n, h, self.inquiry_id) for n, h in enumerate(self._homes(), start=1))

    @function_tool()
    async def select_home(self, context: RunContext, home_number: int) -> str:
        """Switch the current home to one already on file.

        Args:
            home_number: The home's number from list_homes (1-based).
        """
        homes = self._homes()
        if not 1 <= home_number <= len(homes):
            return f"There is no home {home_number}; they have {len(homes)}."
        self.inquiry_id = homes[home_number - 1]["id"]
        db.set_active(self.seller_id, self.inquiry_id)
        return f"Now working on {home_line(home_number, homes[home_number - 1], self.inquiry_id)}"

    @function_tool()
    async def add_new_home(self, context: RunContext) -> str:
        """Start a new home for this homeowner (another property they want to sell) and make it the current home."""
        home = db.create_inquiry(self.seller_id)
        self.inquiry_id = home["id"]
        logger.info("new home %s for %s", home["id"], self.phone)
        return "Created a new home and made it current. Ask for its address, then condition and timeline."

    @function_tool()
    async def get_inquiry(self, context: RunContext) -> str:
        """Look up the current home's details and photos. Use before answering questions about what we have on file
        (it may have been updated by text since the call started)."""
        inquiry = db.get_inquiry(self.inquiry_id)
        photos = db.list_photos(self.inquiry_id)
        details = {f: inquiry.get(f) or "not collected yet" for f in db.FIELDS}
        details["photos"] = [f"{p['room'] or 'photo'}: {p['notes'] or p['caption'] or ''}" for p in photos] or "none yet"
        details["total_homes_on_file"] = len(self._homes())
        return json.dumps(details)

    @function_tool()
    async def text_details(self, context: RunContext) -> str:
        """Text the homeowner a summary of the current home (with its listing link). Use when they ask to be texted the details."""
        result = await send_summary(self.phone, db.get_inquiry(self.inquiry_id))
        logger.info("summary text to %s: %s %s", self.phone, result.status, result.error or "")
        if result.ok:
            return "The text was sent successfully. Tell them it's on its way and they can reply with corrections."
        return (
            f"The text could NOT be sent ({result.error}). Tell the caller honestly that the text didn't go through, "
            "and offer to read the details back to them instead."
        )


def prewarm(proc) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=prewarm)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    db.init()
    metadata = json.loads(ctx.job.metadata or "{}")
    dial = metadata.get("dial")

    if dial:
        # Outbound "CALL ME": place the call through AgentPhone and wait for an answer.
        phone = dial
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=os.environ["LIVEKIT_SIP_OUTBOUND_TRUNK_ID"],
                    sip_call_to=phone,
                    participant_identity=f"phone-{phone}",
                    wait_until_answered=True,
                )
            )
        except api.TwirpError as e:
            logger.error("outbound call to %s failed: %s (%s)", phone, e.message, e.metadata.get("sip_status"))
            inquiry = db.active_inquiry(phone)
            await send_sms(
                phone,
                "tried calling you back but couldn't get through :( text \"call me\" whenever you want me to try again",
                inquiry_id=inquiry["id"],
                purpose="callback_failed",
            )
            ctx.shutdown()
            return
        participant = await ctx.wait_for_participant(identity=f"phone-{phone}")
    else:
        participant = await ctx.wait_for_participant()
        phone = participant.attributes.get("sip.phoneNumber") or participant.identity

    seller = db.get_or_create_seller(phone)
    focus = metadata.get("inquiry_id")
    if focus and (inquiry := db.get_inquiry(focus)) and inquiry["seller_id"] == seller["id"]:
        db.set_active(seller["id"], inquiry["id"])
    else:
        inquiry = db.active_inquiry(phone)
    seller = db.get_seller(phone)
    logger.info("%s call with %s (home %s of %d)", "outbound" if dial else "inbound", phone, inquiry["id"],
                len(db.list_homes(seller["id"])))

    session = AgentSession(
        stt="deepgram/nova-3",
        llm="openai/gpt-4.1-mini",
        tts="cartesia/sonic-3",
        vad=ctx.proc.userdata["vad"],
        turn_handling={"turn_detection": MultilingualModel()},
    )
    await session.start(agent=SellerIntakeAgent(phone, seller, inquiry), room=ctx.room)

    # Speak right away: AgentPhone's trunk needs media flowing as soon as the call is answered.
    if dial:
        await session.generate_reply(
            instructions="You're calling them back because they texted CALL ME. Greet them, say you're Olivia from "
            "Opendoor, recap what we have on file for the current home (by street), and ask what they'd like to update or discuss."
        )
    else:
        await session.generate_reply(
            instructions="Greet the caller warmly as Olivia from Opendoor and ask how you can help with selling their home."
        )


if __name__ == "__main__":
    cli.run_app(server)
