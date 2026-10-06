"""Read-only health check of the live setup. Changes nothing.

Usage: uv run python scripts/doctor.py

Checks: env vars, local server, public tunnel, AgentPhone number routing + SMS + webhook,
LiveKit inbound/outbound trunks + dispatch rule, and whether the voice agent worker is running.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit import api

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

REQUIRED = [
    "AGENTPHONE_API_KEY", "AGENTPHONE_NUMBER", "AGENTPHONE_NUMBER_ID", "AGENTPHONE_SIP_TRUNK_ID",
    "AGENTPHONE_SIP_TERMINATION_URI", "AGENTPHONE_SIP_USERNAME", "AGENTPHONE_SIP_PASSWORD",
    "OPENDOOR_AGENTPHONE_AGENT_ID", "OPENDOOR_WEBHOOK_SECRET", "PUBLIC_BASE_URL",
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "LIVEKIT_SIP_INBOUND_TRUNK_ID", "LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "LIVEKIT_SIP_DISPATCH_RULE_ID",
]
AGENTPHONE = "https://api.agentphone.ai/v1"
failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global failures
    failures += 0 if ok else 1
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    return ok


async def main() -> None:
    print("\nEnvironment")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    check(not missing, "all required .env values present", f"missing: {', '.join(missing)}" if missing else "")
    if missing:
        return
    env = os.environ
    base = env["PUBLIC_BASE_URL"].rstrip("/")

    async with httpx.AsyncClient(timeout=15) as http:
        print("\nLocal processes")
        try:
            r = await http.get("http://127.0.0.1:8000/api/pipeline")
            check(r.status_code == 200, "SMS + web server on :8000", f"{r.json()['stats']}" if r.status_code == 200 else str(r.status_code))
        except httpx.HTTPError:
            check(False, "SMS + web server on :8000", "not running: uv run uvicorn sms_server:app --port 8000")
        workers = subprocess.run(["pgrep", "-f", "agent.py (dev|start)"], capture_output=True, text=True).stdout.split()
        check(bool(workers), "voice agent worker running", "start it: uv run python agent.py dev" if not workers else f"pid {workers[0]}")

        print("\nPublic tunnel")
        try:
            r = await http.get(f"{base}/static/app.css")
            check(r.status_code == 200, f"{base} reachable", "" if r.status_code == 200 else f"HTTP {r.status_code}")
            r = await http.get(f"{base}/api/pipeline")
            check(r.status_code == 404, "ops pipeline hidden from the internet")
        except httpx.HTTPError as e:
            check(False, f"{base} reachable", f"{type(e).__name__}: tunnel down or URL changed")

        print("\nAgentPhone")
        h = {"Authorization": f"Bearer {env['AGENTPHONE_API_KEY']}"}
        number = (await http.get(f"{AGENTPHONE}/numbers/{env['AGENTPHONE_NUMBER_ID']}", headers=h)).json()
        check(number.get("phoneNumber") == env["AGENTPHONE_NUMBER"], f"number {env['AGENTPHONE_NUMBER']} exists")
        routing = number.get("voiceRouting") or {}
        check(routing.get("method") == "sip_trunk" and routing.get("sipTrunkId") == env["AGENTPHONE_SIP_TRUNK_ID"],
              "calls route to our SIP trunk", str(routing))
        check(number.get("outboundSms") == "enabled", "outbound SMS enabled", number.get("outboundSms", "?"))
        check(number.get("agentId") == env["OPENDOOR_AGENTPHONE_AGENT_ID"], "texts go to the Opendoor agent",
              f"agentId={number.get('agentId')}")
        trunk = (await http.get(f"{AGENTPHONE}/sip-trunks/{env['AGENTPHONE_SIP_TRUNK_ID']}", headers=h)).json()
        origins = [u["sip_uri"] for u in trunk.get("originationUris", [])]
        check(any(".sip.livekit.cloud" in u and "transport=tls" in u for u in origins), "trunk sends calls to LiveKit over TLS", ", ".join(origins))
        hook = (await http.get(f"{AGENTPHONE}/agents/{env['OPENDOOR_AGENTPHONE_AGENT_ID']}/webhook", headers=h)).json()
        expected = f"{base}/webhooks/agentphone"
        check(hook.get("url") == expected and hook.get("status") == "active", "webhook points at the tunnel",
              f"{hook.get('url')} ({hook.get('status')})" if hook.get("url") != expected else "")

    print("\nLiveKit")
    lk = api.LiveKitAPI()
    try:
        inbound = {t.sip_trunk_id: t for t in (await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items}
        t = inbound.get(env["LIVEKIT_SIP_INBOUND_TRUNK_ID"])
        check(bool(t) and env["AGENTPHONE_NUMBER"] in t.numbers, "inbound trunk accepts our number", str(list(t.numbers)) if t else "missing")
        outbound = {t.sip_trunk_id: t for t in (await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())).items}
        o = outbound.get(env["LIVEKIT_SIP_OUTBOUND_TRUNK_ID"])
        expected_addr = env["AGENTPHONE_SIP_TERMINATION_URI"].split(":", 1)[1]
        check(bool(o) and o.address == expected_addr and o.auth_username == env["AGENTPHONE_SIP_USERNAME"],
              "outbound trunk dials AgentPhone with our credentials", o.address if o else "missing")
        rules = {r.sip_dispatch_rule_id: r for r in (await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items}
        r = rules.get(env["LIVEKIT_SIP_DISPATCH_RULE_ID"])
        agents = [a.agent_name for a in r.room_config.agents] if r else []
        check(bool(r) and "opendoor-agent" in agents, "dispatch rule sends calls to opendoor-agent", str(agents))
    finally:
        await lk.aclose()


asyncio.run(main())
print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}\n")
sys.exit(1 if failures else 0)
