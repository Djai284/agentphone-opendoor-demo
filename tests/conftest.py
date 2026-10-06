"""Test setup: an isolated database and media folder per test, fake credentials, and stubs for
everything that leaves the machine (texts, calls, LLM). Nothing here talks to AgentPhone or LiveKit.
"""

import io
import os
import sys
import tempfile
from pathlib import Path

# Must be set before the app is imported: sms_server loads .env (which never overrides) and initializes the DB.
_tmp = Path(tempfile.mkdtemp(prefix="opendoor-tests-"))
os.environ.update({
    "OPENDOOR_DB_PATH": str(_tmp / "import.db"),
    "AGENTPHONE_API_KEY": "sk_test_fake",
    "AGENTPHONE_NUMBER": "+14155560199",
    "AGENTPHONE_NUMBER_ID": "num_test",
    "OPENDOOR_WEBHOOK_SECRET": "whsec_test",
    "PUBLIC_BASE_URL": "https://demo.example.test",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import sms_server  # noqa: E402
from opendoor import db, messaging, photos, web  # noqa: E402
from opendoor.corrections import Interpretation  # noqa: E402

SELLER = "+14155560123"
OTHER = "+12125550123".replace("555", "556")  # a second, unrelated caller


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(photos, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(web, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(sms_server, "PHOTO_BATCH_SECONDS", 0)
    db.init()


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound texts instead of sending them. Each item: (to, purpose, body, media_urls)."""
    outbox = []

    async def fake_send(to, body, *, inquiry_id, purpose, media_urls=None):
        outbox.append((to, purpose, body, media_urls))
        return messaging.SendResult(True, "sent", None, f"msg_{len(outbox)}")

    monkeypatch.setattr(sms_server, "send_sms", fake_send)
    monkeypatch.setattr(messaging, "send_sms", fake_send)
    return outbox


@pytest.fixture
def calls(monkeypatch):
    placed = []

    async def fake_call(phone, inquiry_id=None):
        placed.append((phone, inquiry_id))
        return "outbound-test-room"

    monkeypatch.setattr(sms_server, "start_outbound_call", fake_call)
    return placed


@pytest.fixture
def llm(monkeypatch):
    """Script the text interpreter: llm["some text"] = Interpretation(...). Unknown texts become intent 'other'."""
    script = {}

    async def fake_interpret(message, homes, photos_by_home=None, *, with_photo=False, pending=None):
        return script.get(message, Interpretation(intent="other", reply="ok!"))

    monkeypatch.setattr(sms_server, "interpret_text", fake_interpret)
    return script


@pytest.fixture
def vision(monkeypatch):
    async def fake_analyze(stored):
        return {"is_home": True, "room": "kitchen", "caption": "Bright kitchen", "notes": "clean"}

    monkeypatch.setattr(photos, "analyze", fake_analyze)


def jpeg_bytes(color=(200, 120, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(out, "JPEG")
    return out.getvalue()


def heic_bytes() -> bytes:
    import pillow_heif

    out = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (32, 24), (10, 200, 10))).save(out, quality=60)
    return out.getvalue()


SMIL = b'<smil>\n<head>\n<layout>\n <root-layout/>\n</layout>\n</head>\n<body><par><img src="IMG_1.jpg"/></par></body>\n</smil>'
