"""Start an outbound call: dispatch the voice agent into a fresh room with the
number to dial. The agent itself places the SIP call (see agent.py), so it can
report a failed dial accurately.
"""

from __future__ import annotations

import json
import os
import time

from livekit import api

AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "opendoor-agent")


async def start_outbound_call(phone: str, inquiry_id: int | None = None) -> str:
    room = f"outbound-{phone.lstrip('+')}-{int(time.time())}"
    lk = api.LiveKitAPI()
    try:
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME, room=room, metadata=json.dumps({"dial": phone, "inquiry_id": inquiry_id})
            )
        )
    finally:
        await lk.aclose()
    return room
