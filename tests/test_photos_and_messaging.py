import httpx
import pytest

from conftest import SELLER, SMIL, heic_bytes, jpeg_bytes
from opendoor import db, messaging, photos



# --- photos -------------------------------------------------------------------------


async def test_mms_skips_smil_part_and_finds_the_jpeg(monkeypatch):
    parts = {0: SMIL, 1: jpeg_bytes()}
    requested = []

    async def fake_download(url):
        requested.append(url)
        index = int(url.rsplit("=", 1)[1])
        if index not in parts:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url), response=httpx.Response(404))
        return parts[index]

    monkeypatch.setattr(photos, "download", fake_download)
    data, kind = await photos.fetch_image("https://api.agentphone.ai/v1/messages/msg123/media")
    assert kind == "jpg" and data == parts[1]
    assert requested[0].endswith("?index=0") and requested[1].endswith("?index=1")


async def test_heic_is_converted_to_jpeg(monkeypatch):
    async def fake_download(url):
        return heic_bytes()

    monkeypatch.setattr(photos, "download", fake_download)
    data, kind = await photos.fetch_image("https://example.test/IMG_1.HEIC")
    assert kind == "jpg" and photos.sniff(data) == "jpg"


async def test_non_images_are_rejected_with_a_reason(monkeypatch):
    async def fake_download(url):
        return b"<html>not a photo</html>"

    monkeypatch.setattr(photos, "download", fake_download)
    with pytest.raises(photos.PhotoError):
        await photos.fetch_image("https://example.test/page")


def test_api_key_only_sent_to_agentphone():
    assert photos.candidate_urls("https://storage.example.com/a.jpg") == ["https://storage.example.com/a.jpg"]
    assert photos.AGENTPHONE_MEDIA.match("https://api.agentphone.ai/v1/messages/abc/media")


# --- outbound texts -----------------------------------------------------------------


def mock_agentphone(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(messaging.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


async def test_send_failure_is_reported_and_recorded(monkeypatch):
    mock_agentphone(monkeypatch, lambda req: httpx.Response(400, json={"detail": "number not enabled"}))
    home = db.active_inquiry(SELLER)
    result = await messaging.send_sms(SELLER, "hi", inquiry_id=home["id"], purpose="summary")
    assert not result.ok and "number not enabled" in result.error
    with db.connect() as conn:
        row = conn.execute("SELECT status, error FROM outbound_messages").fetchone()
    assert row["status"] == "failed" and "400" in row["error"]


async def test_send_success_records_message_id_and_attaches_media(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(__import__("json").loads(req.content))
        return httpx.Response(200, json={"id": "msg_1", "status": "queued"})

    mock_agentphone(monkeypatch, handler)
    home = db.active_inquiry(SELLER)
    result = await messaging.send_sms(SELLER, "hi", inquiry_id=home["id"], purpose="summary",
                                      media_urls=["https://demo.example.test/media/x/y.jpg"])
    assert result.ok and result.message_id == "msg_1"
    assert seen["number_id"] == "num_test" and seen["media_urls"] == ["https://demo.example.test/media/x/y.jpg"]


async def test_never_texts_invalid_numbers_and_rate_limits(monkeypatch):
    mock_agentphone(monkeypatch, lambda req: httpx.Response(200, json={"id": "m", "status": "queued"}))
    assert (await messaging.send_sms("+1121611611", "x", inquiry_id=None, purpose="t")).status == "skipped"
    results = [await messaging.send_sms(SELLER, "x", inquiry_id=None, purpose="t") for _ in range(7)]
    assert [r.ok for r in results].count(True) == messaging.MAX_PER_MINUTE
