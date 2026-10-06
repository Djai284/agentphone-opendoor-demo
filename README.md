# Opendoor home-selling assistant (LiveKit + AgentPhone)

A phone and text assistant that helps a homeowner start selling their home(s) to Opendoor.
Call the number, talk to **Olivia** (a LiveKit voice agent), and she collects your name, address,
home condition, and selling timeline. Ask her to text you the details. Reply by text with
corrections, photos, or **CALL ME**, and everything lands on the same seller inquiry. A mock
Opendoor web app shows each listing updating live.

| | |
|---|---|
| **Phone number** | AgentPhone number, SIP-trunked to LiveKit (calls) + webhook to this app (texts) |
| **Voice** | LiveKit Agents, LiveKit Inference: `deepgram/nova-3` STT · `openai/gpt-4.1-mini` LLM · `cartesia/sonic-3` TTS |
| **Texts** | FastAPI webhook, LLM intent extraction via LiveKit Inference, templated replies |
| **State** | SQLite (`opendoor.db`), photos on disk (`media/`) |
| **UI** | Seller pipeline (local), "Your homes" portal, live listing pages |

---

## Run the demo

Already set up (see [Setup](#setup) for first-time configuration)? Then:

```bash
git clone git@github.com:Djai284/agentphone-opendoor-demo.git && cd agentphone-opendoor-demo
cp .env.example .env                                  # fill in (first time only)
uv sync && uv run python agent.py download-files      # first time only

# three terminals
uv run python agent.py dev                            # 1. voice agent
uv run uvicorn sms_server:app --port 8000             # 2. webhook + web app
cloudflared tunnel --url http://localhost:8000        # 3. public URL

# each time the tunnel URL changes: set PUBLIC_BASE_URL in .env, then
uv run python scripts/register_webhook.py https://<tunnel>.trycloudflare.com/webhooks/agentphone
# ...and restart terminal 2

uv run python scripts/doctor.py                       # every check should be ✅
```

Open the **seller pipeline** at http://127.0.0.1:8000 and keep it on screen, then:

1. **Call** your AgentPhone number. Olivia asks for your name, address, the home's condition, and your timeline. Watch the card appear and fill in.
2. Say **"text me the details."** The summary arrives with a link to your live listing page.
3. **Reply** "Actually, I'd like to sell in December." You get a confirmation and the listing flashes the new timeline.
4. **Text 3 photos** (kitchen, living room, outside). They're tagged and added to the listing; the card moves to *Specialist review*.
5. Text **"I also want to sell my place at 12 Bergen St, Brooklyn."** A second home appears, and the reply links to *Your homes*.
6. Text **"CALL ME."** Olivia calls you back and already knows your homes.
7. `uv run python scripts/show_db.py` shows the stored state: sellers, homes, every change and its source, deduped inbound texts, outbound texts with their real status.

---

## How it fits together

```
                     ┌─────────────────────── AgentPhone ───────────────────────┐
 homeowner's phone ──┤  your AgentPhone number                                   │
                     │   calls → SIP trunk (TLS/SRTP) ─────────────┐            │
                     │   texts → webhook (HMAC-signed) ──────┐     │            │
                     └───────────────────────────────────────┼─────┼────────────┘
                                                             │     │
                         cloudflared tunnel                  │     │  sip:<project>.sip.livekit.cloud:5061
                                                             ▼     ▼
 ┌──────────── sms_server.py (FastAPI :8000) ───────────┐   ┌──────── LiveKit Cloud ────────┐
 │ /webhooks/agentphone  verify → dedupe → interpret    │   │ inbound trunk → dispatch rule │
 │   update / summary / question / photo / CALL ME      │   │  → room "call-…" + agent      │
 │ /  /s/<token>  /l/<token>   live web pages           │   └──────────────┬────────────────┘
 └───────┬──────────────────────────────┬───────────────┘                  │
         │ CALL ME: dispatch agent with │ reads/writes                     ▼
         │ {"dial": "+1…"} ─────────────┼──────────────────────►  agent.py (Olivia)
         ▼                              ▼                         tools: save_details, select_home,
   LiveKit outbound trunk ──► AgentPhone ──► homeowner           add_new_home, get_inquiry, text_details
                                   ┌──────────────┐                        │
                                   │ opendoor.db  │◄───────────────────────┘
                                   └──────────────┘
```

### A call
1. AgentPhone routes the number's calls to its SIP trunk, which points at LiveKit's SIP endpoint.
2. LiveKit's inbound trunk accepts the call; the dispatch rule creates a fresh `call-…` room and
   dispatches the `opendoor-agent` worker into it. **One room per call.**
3. `agent.py` reads the caller's number from the SIP participant (`sip.phoneNumber`). **The phone
   number is the seller's identity**, which keeps every caller's data separate.
4. Olivia greets immediately (AgentPhone needs audio flowing on answer), loads any homes on file
   into her instructions, and calls `save_details` as soon as she learns each field.
5. "Text me the details" → `text_details` → AgentPhone `POST /v1/messages`. The tool returns
   whether the send actually succeeded, and Olivia is instructed to report exactly that.

### A text
1. AgentPhone POSTs a signed webhook. The server checks the HMAC signature (`X-Webhook-Signature`),
   ignores non-US/carrier-error senders, then **claims** the delivery in `processed_messages`
   (keyed on `X-Webhook-ID`, plus a hash of sender + time + body). A repeated delivery is a no-op.
2. It returns `200` immediately and processes in the background, so AgentPhone never retries
   because of a slow LLM call. If processing crashes, the claim is released so a retry can redo it.
3. The LLM (LiveKit Inference) **only classifies and extracts** JSON: intent, which home, changed
   fields. Code validates it, writes to SQLite, and builds the reply from what actually changed.
   - `update` → apply + "done ✅ updated: timeline: December (was next month)"
   - `summary` → resend the summary (with the cover photo attached as MMS)
   - `question` → short answer grounded only in stored data (never a price)
   - `call_me` / literal `CALL ME` → outbound call (below)
   - `new_home` / `select_home` → add another home / switch which home we're talking about
4. Photos: the MMS parts are fetched until one is an image (carriers put a SMIL layout file
   first), HEIC is converted to JPEG, and a vision model tags the room + condition notes. A burst
   of photos gets one combined reply.

### CALL ME (outbound)
`sms_server` creates a LiveKit agent dispatch with metadata `{"dial": "+1…", "inquiry_id": …}`.
The agent places the call with `CreateSIPParticipant` over the outbound trunk (AgentPhone's
termination URI + digest credentials), waits for an answer, and opens with what's on file for that
home. If the dial fails, it texts the homeowner that it couldn't connect.

### Stored state (`opendoor/db.py`)
| Table | What it holds |
|---|---|
| `sellers` | one row per phone number: name, portal token, **active home**, pending "which home?" question |
| `inquiries` | one row per home: address, condition, timeline, listing token |
| `inquiry_events` | every field change: old → new, source (`call`/`sms`), triggering delivery id |
| `processed_messages` | inbound SMS dedupe log and outcome |
| `outbound_messages` | every text we tried to send: `pending` → `sent` / `failed` + error |
| `photos` | stored photo file, room tag, caption, condition notes |

A seller can have several homes. Texts are routed to the home they name ("the bergen place"),
the only home missing the field being filled in, or the **active** home (the one the conversation
is about). When it's truly ambiguous, the reply asks "which home is this for? 1. … 2. …" and
applies the change once they answer.

---

## Setup

**Prereqs:** Python 3.12+, [uv](https://docs.astral.sh/uv/), `cloudflared` (`brew install cloudflared`), `lk` (`brew install livekit-cli`),
an AgentPhone account with SIP trunking enabled, and a LiveKit Cloud project (free tier is fine).

```bash
uv sync
uv run python agent.py download-files      # VAD + turn-detector models
```

### 1. Credentials (`.env`)
`cp .env.example .env` and fill it in. `.env` is gitignored; never commit it.

| Variable | Where it comes from |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | `lk cloud auth` then `lk app env -w`, or LiveKit dashboard → Settings → API keys |
| `AGENTPHONE_API_KEY` | AgentPhone dashboard |
| `AGENTPHONE_NUMBER`, `AGENTPHONE_NUMBER_ID` | the number (E.164) and its id from `GET /v1/numbers` |
| `AGENTPHONE_SIP_TRUNK_ID`, `AGENTPHONE_SIP_TERMINATION_URI`, `AGENTPHONE_SIP_USERNAME`, `AGENTPHONE_SIP_PASSWORD` | step 2 (the password is shown **once**) |
| `OPENDOOR_AGENTPHONE_AGENT_ID` | an AgentPhone agent in `webhook` mode that owns the number (receives its texts) |
| `LIVEKIT_SIP_INBOUND_TRUNK_ID`, `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`, `LIVEKIT_SIP_DISPATCH_RULE_ID` | printed by step 3 |
| `PUBLIC_BASE_URL`, `OPENDOOR_WEBHOOK_SECRET` | step 4 |

### 2. AgentPhone: SIP trunk → LiveKit
1. Create the trunk **in the AgentPhone dashboard** (SIP Trunks → New). Trunks created via
   `POST /v1/sip-trunks` came out incompatible with our number (`422 This SIP trunk isn't
   compatible with this number`). Save the username/password it shows.
2. Point it at LiveKit (TLS on 5061 is required):
   ```bash
   curl -X PATCH https://api.agentphone.ai/v1/sip-trunks/$AGENTPHONE_SIP_TRUNK_ID \
     -H "Authorization: Bearer $AGENTPHONE_API_KEY" -H "Content-Type: application/json" \
     -d '{"origination":{"uris":[{"sip_uri":"sip:<project-id>.sip.livekit.cloud:5061;transport=tls"}]}}'
   ```
3. Route the number's calls to the trunk (`PATCH /v1/numbers/<id>/voice-routing`
   `{"method":"sip_trunk","sipTrunkId":"…"}`, or in the dashboard), and assign the number to the
   webhook-mode agent (`POST /v1/agents/<agent_id>/numbers`) so its texts reach us.

### 3. LiveKit: trunks + dispatch rule
```bash
uv run python scripts/setup_livekit_sip.py    # idempotent; prints the three IDs for .env
```
Creates an inbound trunk for the number, a dispatch rule (one room per call → `opendoor-agent`),
and an outbound trunk that dials AgentPhone's termination URI over TLS with SRTP.

### 4. Run it
Three terminals:
```bash
uv run python agent.py dev                          # voice agent worker
uv run uvicorn sms_server:app --port 8000           # webhook + web app
cloudflared tunnel --url http://localhost:8000      # public URL for webhooks, links, MMS images
```
Then, with the tunnel URL:
```bash
echo 'PUBLIC_BASE_URL=https://<random>.trycloudflare.com' >> .env    # or edit the existing line
uv run python scripts/register_webhook.py https://<random>.trycloudflare.com/webhooks/agentphone
# restart sms_server so it picks up PUBLIC_BASE_URL and the new OPENDOOR_WEBHOOK_SECRET
```
> Quick tunnels get a new URL every restart. Re-run these two steps each time (old links in past
> texts stop working). A named Cloudflare tunnel or a deploy avoids this.

---

## Testing

### 1. Automated tests (fast, free, offline)
```bash
uv run pytest
```
26 tests. Texts, calls, and the LLM are stubbed, so nothing leaves the machine. Covers:
- **separation:** two callers never see each other's data; one seller with many homes
- **idempotency:** the same webhook delivered 3× applies once; a new delivery id for the same message is caught too
- **signatures:** a bad signature is rejected; carrier error senders are ignored (no reply loops)
- **corrections:** "sell in december" updates the same inquiry and confirms with old → new
- **multiple homes:** new home, filling blanks, "which home?" → "1", photo + "these are for bergen"
- **photos:** SMIL-first MMS, HEIC → JPEG, non-images rejected
- **send failures:** AgentPhone 4xx is reported as failed and recorded; invalid numbers + rate limit
- **web:** pipeline stages, pipeline hidden through the tunnel, public pages expose no phone numbers
- **migration:** the v1 one-inquiry-per-phone database upgrades in place and keeps listing links

### 2. Live LLM checks (a few LiveKit Inference credits)
```bash
RUN_LIVE_LLM=1 uv run pytest tests/test_llm_live.py -v
```
Runs the real prompt on sample texts: intent detection, picking the right home, never quoting a price.

### 3. Live health check (read-only)
```bash
uv run python scripts/doctor.py
```
Verifies every link in the chain: `.env`, local server and agent worker, tunnel reachability,
AgentPhone number routing / SMS / webhook URL, LiveKit trunks and dispatch rule. Run this first
whenever something "stopped working". It's usually a changed tunnel URL.

### 4. End-to-end on a real phone
| # | Do | Expect |
|---|---|---|
| 1 | Call the number | Olivia greets within ~2s; terminal shows `inbound call with +1…` |
| 2 | Give name, address, condition, timeline | Pipeline card fills in live at http://127.0.0.1:8000 |
| 3 | Say "text me the details" | Summary text arrives with listing link; Olivia says it was sent |
| 4 | Reply "Actually, I'd like to sell in December" | "done ✅ … timeline: December (was …)"; listing flashes |
| 5 | Text 3 photos (kitchen, living room, outside) | One "got 3 pics 📸" reply; card moves to *Specialist review* |
| 6 | Text "I also want to sell my place at 12 Bergen St Brooklyn" | New home card + "all your homes" link |
| 7 | Text "CALL ME" | "on it, calling you now 📞", then a call that recaps your home |
| 8 | Inspect state | `uv run python scripts/show_db.py` |

To demo a failed send honestly, point `AGENTPHONE_NUMBER_ID` at a number without outbound SMS,
restart, and ask Olivia to text you. She says the text didn't go through and offers to read it out.

---

## Project layout
```
agent.py                 LiveKit voice agent (inbound + outbound), tools write to SQLite
sms_server.py            AgentPhone webhook: verify, dedupe, interpret, reply; mounts the web app
opendoor/db.py           schema, migration, sellers/homes, idempotency, outbound log
opendoor/corrections.py  LLM text interpreter (intent, home, changes) via LiveKit Inference
opendoor/messaging.py    AgentPhone send (never raises, records real status), summary template
opendoor/photos.py       MMS fetch (multi-part), HEIC→JPEG, vision tagging
opendoor/calls.py        outbound "CALL ME" dispatch
opendoor/web.py          pipeline / portal / listing endpoints
opendoor/templates/      HTML + CSS for the mock Opendoor UI
scripts/                 setup_livekit_sip, register_webhook, doctor, show_db
tests/                   pytest suite (+ opt-in live LLM tests)
```

## Known limits
- Opendoor's systems are mocked: no real offers or pricing; "Specialist review" is the last automatic stage.
- Quick-tunnel URLs change on restart (see Run it).
- AgentPhone SIP trunks: US/Canada destinations only, no 911, caller ID must be the attached number.
- Photo tags come from a vision model and can be wrong (e.g. it called a brewery "a two-story house").
- Single-machine demo: SQLite + local files; photo batching state is in memory.
- The UI is Opendoor-*inspired* and labeled "demo"; it doesn't use Opendoor's logo or assets.
