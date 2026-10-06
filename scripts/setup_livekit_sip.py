"""Configure LiveKit's side of the SIP connection to AgentPhone.

Creates (or reuses, matched by name):
  - an inbound trunk that accepts calls to our AgentPhone number
  - a dispatch rule that puts each call in its own room and dispatches our agent
  - an outbound trunk that dials out through AgentPhone's termination URI

Safe to run repeatedly. Prints the IDs to add to .env.
"""

import asyncio
import os

from dotenv import load_dotenv
from livekit import api

load_dotenv()

PHONE_NUMBER = os.environ["AGENTPHONE_NUMBER"]
AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "opendoor-agent")
INBOUND_NAME = "AgentPhone inbound"
OUTBOUND_NAME = "AgentPhone outbound"
DISPATCH_NAME = "AgentPhone calls -> opendoor-agent"


async def main() -> None:
    lk = api.LiveKitAPI()
    try:
        inbound = await ensure_inbound_trunk(lk)
        rule = await ensure_dispatch_rule(lk, inbound.sip_trunk_id)
        outbound = await ensure_outbound_trunk(lk)
    finally:
        await lk.aclose()

    print(f"\nLIVEKIT_SIP_INBOUND_TRUNK_ID={inbound.sip_trunk_id}")
    print(f"LIVEKIT_SIP_DISPATCH_RULE_ID={rule.sip_dispatch_rule_id}")
    print(f"LIVEKIT_SIP_OUTBOUND_TRUNK_ID={outbound.sip_trunk_id}")


async def ensure_inbound_trunk(lk: api.LiveKitAPI) -> api.SIPInboundTrunkInfo:
    existing = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
    for t in existing.items:
        if t.name == INBOUND_NAME:
            print(f"inbound trunk exists: {t.sip_trunk_id}")
            return t
    trunk = await lk.sip.create_inbound_trunk(
        api.CreateSIPInboundTrunkRequest(
            trunk=api.SIPInboundTrunkInfo(
                name=INBOUND_NAME,
                numbers=[PHONE_NUMBER],
                krisp_enabled=True,
                # AgentPhone's trunk requires SRTP media.
                media_encryption=api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_ALLOW,
            )
        )
    )
    print(f"created inbound trunk: {trunk.sip_trunk_id}")
    return trunk


async def ensure_dispatch_rule(lk: api.LiveKitAPI, trunk_id: str) -> api.SIPDispatchRuleInfo:
    existing = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    for r in existing.items:
        if r.name == DISPATCH_NAME:
            print(f"dispatch rule exists: {r.sip_dispatch_rule_id}")
            return r
    rule = await lk.sip.create_dispatch_rule(
        api.CreateSIPDispatchRuleRequest(
            dispatch_rule=api.SIPDispatchRuleInfo(
                name=DISPATCH_NAME,
                trunk_ids=[trunk_id],
                # One room per call, so every caller gets their own agent session.
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")
                ),
                room_config=api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
                ),
            )
        )
    )
    print(f"created dispatch rule: {rule.sip_dispatch_rule_id}")
    return rule


async def ensure_outbound_trunk(lk: api.LiveKitAPI) -> api.SIPOutboundTrunkInfo:
    existing = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
    for t in existing.items:
        if t.name == OUTBOUND_NAME:
            print(f"outbound trunk exists: {t.sip_trunk_id}")
            return t
    # AgentPhone gives a URI like sips:host:5062; LiveKit wants host:port plus a transport.
    address = os.environ["AGENTPHONE_SIP_TERMINATION_URI"].split(":", 1)[1]
    trunk = await lk.sip.create_outbound_trunk(
        api.CreateSIPOutboundTrunkRequest(
            trunk=api.SIPOutboundTrunkInfo(
                name=OUTBOUND_NAME,
                address=address,
                transport=api.SIPTransport.SIP_TRANSPORT_TLS,
                numbers=[PHONE_NUMBER],
                auth_username=os.environ["AGENTPHONE_SIP_USERNAME"],
                auth_password=os.environ["AGENTPHONE_SIP_PASSWORD"],
                media_encryption=api.SIPMediaEncryption.SIP_MEDIA_ENCRYPT_REQUIRE,
            )
        )
    )
    print(f"created outbound trunk: {trunk.sip_trunk_id}")
    return trunk


if __name__ == "__main__":
    asyncio.run(main())
