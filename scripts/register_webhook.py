"""Point the Opendoor AgentPhone agent's webhook at a public URL and save its secret.

Usage: uv run python scripts/register_webhook.py https://<tunnel>/webhooks/agentphone
"""

import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV)

url = sys.argv[1]
agent_id = os.environ["OPENDOOR_AGENTPHONE_AGENT_ID"]
resp = httpx.post(
    f"https://api.agentphone.ai/v1/agents/{agent_id}/webhook",
    headers={"Authorization": f"Bearer {os.environ['AGENTPHONE_API_KEY']}"},
    json={"url": url, "contextLimit": 0, "timeout": 30},
    timeout=30,
)
resp.raise_for_status()
data = resp.json()
print(f"webhook {data['id']} -> {data['url']} ({data['status']})")

secret = data.get("secret")
if secret:
    # .env is a symlink to the project root's .env; edit the real file.
    real = ENV.resolve()
    text = real.read_text()
    line = f"OPENDOOR_WEBHOOK_SECRET={secret}"
    text = re.sub(r"^OPENDOOR_WEBHOOK_SECRET=.*$", line, text, flags=re.M) if "OPENDOOR_WEBHOOK_SECRET=" in text else text + line + "\n"
    real.write_text(text)
    print("saved OPENDOOR_WEBHOOK_SECRET to .env (restart sms_server to pick it up)")
