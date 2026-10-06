from fastapi.testclient import TestClient

import sms_server
from conftest import SELLER
from opendoor import db

local = TestClient(sms_server.app, client=("127.0.0.1", 50000))


def two_homes():
    first = db.active_inquiry(SELLER)
    db.update_inquiry(first["id"], {"name": "Jamie Rivera", "address": "1 Main St, Austin, TX",
                                    "condition": "good", "timeline": "spring"}, source="call")
    second = db.create_inquiry(first["seller_id"])
    db.update_inquiry(second["id"], {"address": "12 Bergen St, Brooklyn, NY"}, source="sms")
    return db.get_inquiry(first["id"]), db.get_inquiry(second["id"])


def test_pipeline_groups_homes_by_stage():
    two_homes()
    data = local.get("/api/pipeline").json()
    assert data["stats"] == {"sellers": 1, "homes": 2, "photos": 0, "ready": 0}
    stages = {h["short"]: h["stage_label"] for h in data["homes"]}
    assert stages == {"1 Main St": "Needs photos", "12 Bergen St": "Collecting details"}


def test_pipeline_is_hidden_through_the_tunnel():
    assert local.get("/api/pipeline", headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 404
    remote = TestClient(sms_server.app, client=("203.0.113.9", 50000))
    assert remote.get("/").status_code == 404


def test_portal_and_listing_pages():
    first, second = two_homes()
    portal = local.get(f"/s/{first['portal_token']}/data").json()
    assert [h["short"] for h in portal["homes"]] == ["1 Main St", "12 Bergen St"]

    listing = local.get(f"/l/{second['listing_token']}/data").json()
    assert listing["first_name"] == "Jamie" and "phone" not in listing  # public page: no phone number
    assert [h["current"] for h in listing["homes"]] == [False, True]
    assert listing["missing"] == ["condition", "timeline"]
    assert local.get(f"/l/{second['listing_token']}").status_code == 200


def test_unknown_tokens_and_bad_media_paths_404():
    assert local.get("/l/doesNotExist1").status_code == 404
    assert local.get("/s/doesNotExist1").status_code == 404
    assert local.get("/media/abcdefgh/../../.env").status_code == 404
