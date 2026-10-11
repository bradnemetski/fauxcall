"""The custom handlers, checked against the exact bytes the real server sent (recording ids)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx2
import pytest

from fauxcall_petstore import handlers
from fauxcall_petstore.app import build_app
from fauxcall_petstore.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

XML = {"Accept": "application/xml"}
PROBE = 9837410001
USER1 = "user1"  # a public seed fixture (UserData.java), recorded as is


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    app = build_app(Settings(fast=True, log_level="WARNING"))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as c:
        yield c


def _ct(response: httpx2.Response) -> str | None:
    return response.headers.get("content-type")


# -- pets: reads -----------------------------------------------------------------------------------


async def test_find_by_status_lists_in_server_order(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v3/pet/findByStatus", params={"status": "available"})
    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [1791717175310, 1791717177227, 1]  # 000001


async def test_find_by_status_uses_the_first_value(client: httpx2.AsyncClient) -> None:
    response = await client.get(
        "/api/v3/pet/findByStatus", params=[("status", "sold"), ("status", "available")]
    )
    assert [p["id"] for p in response.json()] == [4242]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({}, "Input error: missing required query parameter `status`"),
        (
            {"status": "Available"},
            (
                "Input error: query parameter `status value `Available` is not in the allowable "
                "values `[available, pending, sold]`"
            ),
        ),
        (
            {"status": ""},
            (
                "Input error: query parameter `status value `` is not in the allowable values "
                "`[available, pending, sold]`"
            ),
        ),
    ],
)
async def test_find_by_status_errors(
    client: httpx2.AsyncClient, query: dict[str, str], message: str
) -> None:
    response = await client.get("/api/v3/pet/findByStatus", params=query)
    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": message}


async def test_find_by_status_xml_matches_the_recording(client: httpx2.AsyncClient) -> None:
    response = await client.get(
        "/api/v3/pet/findByStatus", params={"status": "available"}, headers=XML
    )
    assert _ct(response) == "application/xml"
    assert response.text.startswith("<ArrayList><item><id>1791717175310</id><category><id>1</id>")
    assert response.text.endswith(  # 000302
        "<item><id>1</id><category/><name>Pet1</name><photoUrls><photoUrl>test1</photoUrl>"
        "<photoUrl>test2</photoUrl></photoUrls><tags/><status>available</status></item></ArrayList>"
    )
    empty = await client.get("/api/v3/pet/findByStatus", params={"status": "pending"}, headers=XML)
    assert empty.text == "<ArrayList/>"  # 000303


async def test_find_by_tags(client: httpx2.AsyncClient) -> None:
    missing = await client.get("/api/v3/pet/findByTags")
    assert missing.json() == {
        "code": 400,
        "message": "Input error: missing required query parameter `tags`",
    }
    empty = await client.get("/api/v3/pet/findByTags", params={"tags": ""})
    assert (empty.status_code, empty.text, _ct(empty)) == (
        400,
        "No tags provided. Try again?",
        "application/xml",
    )
    broken = await client.get("/api/v3/pet/findByTags", params={"tags": "tag1"})
    assert broken.status_code == 500
    assert re.fullmatch(
        r"There was an error processing your request\. It has been logged \(ID: [0-9a-f]{16}\)",
        broken.json()["message"],
    )


async def test_find_by_tags_when_healthy(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "TAGS_POISONED", False)
    found = await client.get("/api/v3/pet/findByTags", params=[("tags", "automation")])
    assert [p["id"] for p in found.json()] == [1791717175310, 1791717177227]
    xml = await client.get("/api/v3/pet/findByTags", params={"tags": "nope"}, headers=XML)
    assert xml.text == "<ArrayList/>"


async def test_get_pet_json_and_xml(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v3/pet/1")
    assert response.content == (  # 000023, byte for byte
        b'{"id":1,"name":"Pet1","photoUrls":["test1","test2"],"tags":[],"status":"available"}'
    )
    xml = await client.get("/api/v3/pet/9", headers=XML)
    assert xml.text == (  # 000305
        "<Pet><id>9</id><category/><name>friendly</name><photoUrls><photoUrl>"
        "/tmp/inflector701163699009286637.tmp</photoUrl></photoUrls><tags/><status/></Pet>"
    )
    mixed = await client.get(
        "/api/v3/pet/1", headers={"Accept": "application/xml, application/json"}
    )
    assert _ct(mixed) == "application/json"  # 000314


@pytest.mark.parametrize(
    ("raw", "status"), [("abc", 400), ("99999999999999999999", 400), ("1.5", 400), ("-1", 404)]
)
async def test_get_pet_bad_ids(client: httpx2.AsyncClient, raw: str, status: int) -> None:
    response = await client.get(f"/api/v3/pet/{raw}")
    assert response.status_code == status
    if status == 400:
        assert response.json()["message"] == (
            f"Input error: couldn't convert `{raw}` to type `class java.lang.Long`"
        )
    else:
        assert (response.text, _ct(response)) == ("Pet not found", "application/xml")


async def test_bad_id_as_xml_api_error(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v3/pet/abc", headers=XML)
    assert response.text == (  # 000308
        "<ApiError><code>400</code><message>Input error: couldn't convert `abc` to type "
        "`class java.lang.Long`</message></ApiError>"
    )


async def test_routing_quirks(client: httpx2.AsyncClient) -> None:
    slash = await client.get("/api/v3/pet/1/")
    assert slash.status_code == 200  # 000057
    lower = await client.get("/api/v3/pet/findbystatus", params={"status": "available"})
    assert lower.status_code == 400  # 000058: routed to /pet/{petId}
    assert "findbystatus" in lower.json()["message"]
    case = await client.get("/api/v3/Pet/1")
    assert case.json() == {"code": 404, "message": "HTTP 404 Not Found"}  # 000056
    method = await client.get("/api/v3/store/order")
    assert method.json() == {"code": 405, "message": "HTTP 405 Method Not Allowed"}  # 000061
    assert method.headers["server"] == "Jetty(9.4.53.v20231009)"


# -- pets: writes ----------------------------------------------------------------------------------


async def test_add_pet_defaults_and_drops_unknown_fields(client: httpx2.AsyncClient) -> None:
    body = {"id": PROBE, "name": "probe", "color": "brown"}
    created = await client.post("/api/v3/pet", json=body)
    assert created.content == f'{{"id":{PROBE},"name":"probe","photoUrls":[],"tags":[]}}'.encode()
    again = await client.get(f"/api/v3/pet/{PROBE}")
    assert again.json() == created.json()


async def test_add_pet_accepts_anything_and_replaces(client: httpx2.AsyncClient) -> None:
    await client.post("/api/v3/pet", json={"id": PROBE, "photoUrls": [], "status": "bogus"})
    replaced = await client.post(
        "/api/v3/pet", json={"id": PROBE, "name": "dup", "photoUrls": ["a"], "status": "sold"}
    )
    assert replaced.json()["status"] == "sold"
    sold = await client.get("/api/v3/pet/findByStatus", params={"status": "sold"})
    assert [p["id"] for p in sold.json()] == [4242, PROBE]


async def test_add_pet_from_form_and_xml(client: httpx2.AsyncClient) -> None:
    form = await client.post(
        "/api/v3/pet", data={"id": str(PROBE), "name": "formed", "status": "sold"}
    )
    assert form.json() == {"id": PROBE, "name": "formed", "status": "sold"}  # 000145
    form_xml = await client.get(f"/api/v3/pet/{PROBE}", headers=XML)
    assert form_xml.text == (  # 000355: null lists are left out
        f"<Pet><id>{PROBE}</id><category/><name>formed</name><status>sold</status></Pet>"
    )
    xml_body = (
        f"<Pet><id>{PROBE + 1}</id><name>x</name><photoUrls><photoUrl>p</photoUrl></photoUrls>"
        "<tags><tag><id>3</id><name>t</name></tag></tags><category><id>1</id><name>Dogs</name>"
        "</category><status>available</status></Pet>"
    )
    xml = await client.post(
        "/api/v3/pet", content=xml_body, headers={"Content-Type": "application/xml"}
    )
    assert xml.json() == {
        "id": PROBE + 1,
        "category": {"id": 1, "name": "Dogs"},
        "name": "x",
        "photoUrls": ["p"],
        "tags": [{"id": 3, "name": "t"}],
        "status": "available",
    }


@pytest.mark.parametrize(
    ("content", "content_type", "status"),
    [
        ('{"id": "abc", "name": "x"}', "application/json", 400),
        ('{"id": 1, "name": ', "application/json", 400),
        ('{"id": 1, "photoUrls": "nope"}', "application/json", 400),
        ('{"id": 1, "category": 5}', "application/json", 400),
        ('{"id": true}', "application/json", 400),
        ("[1]", "application/json", 400),
        ("<!DOCTYPE x><Pet/>", "application/xml", 400),
        ("<Pet><id>", "application/xml", 400),
        ("hello", "text/plain", 415),
        ('{"name": "noid", "photoUrls": []}', "application/json", 500),
        ("null", "application/json", 500),
    ],
)
async def test_add_pet_rejections(
    client: httpx2.AsyncClient, content: str, content_type: str, status: int
) -> None:
    response = await client.post(
        "/api/v3/pet", content=content, headers={"Content-Type": content_type}
    )
    assert response.status_code == status


async def test_add_pet_coerces_scalars(client: httpx2.AsyncClient) -> None:
    response = await client.post(
        "/api/v3/pet",
        json={"id": str(PROBE), "name": 12, "photoUrls": [True], "tags": [{"id": 2.9}]},
    )
    assert response.json() == {
        "id": PROBE,
        "name": "12",
        "photoUrls": ["true"],
        "tags": [{"id": 2}],
    }


async def test_update_pet(client: httpx2.AsyncClient) -> None:
    missing = await client.put("/api/v3/pet", json={"id": PROBE, "name": "x", "photoUrls": []})
    assert (missing.status_code, missing.text) == (404, "Pet not found")  # 000159
    await client.post("/api/v3/pet", json={"id": PROBE, "name": "a", "category": {"id": 1}})
    replaced = await client.put("/api/v3/pet", json={"id": PROBE, "name": "b"})
    assert replaced.json() == {"id": PROBE, "name": "b", "photoUrls": [], "tags": []}
    noid = await client.put("/api/v3/pet", json={"name": "b"})
    assert noid.status_code == 500
    bad = await client.put(
        "/api/v3/pet", content="{oops", headers={"Content-Type": "application/json"}
    )
    assert bad.json()["message"] == (
        "Input error: unable to convert input to io.swagger.petstore.model.Pet"
    )


async def test_update_pet_with_form(client: httpx2.AsyncClient) -> None:
    await client.post("/api/v3/pet", json={"id": PROBE, "name": "a", "status": "sold"})
    no_name = await client.post(f"/api/v3/pet/{PROBE}", params={"status": "pending"})
    assert (no_name.status_code, no_name.text) == (400, "No Name provided. Try again?")
    renamed = await client.post(f"/api/v3/pet/{PROBE}", params={"name": "b"})
    assert "status" not in renamed.json()  # 000168: status cleared
    both = await client.post(f"/api/v3/pet/{PROBE}", params={"name": "c", "status": "pending"})
    assert both.json()["status"] == "pending"
    missing = await client.post(f"/api/v3/pet/{PROBE + 9}", params={"name": "x"})
    assert missing.status_code == 404
    bad = await client.post("/api/v3/pet/abc", params={"name": "x"})
    assert bad.status_code == 400


async def test_updates_move_the_pet_to_the_end(client: httpx2.AsyncClient) -> None:
    await client.post("/api/v3/pet", json={"id": PROBE, "name": "a", "status": "available"})
    await client.post("/api/v3/pet/1", params={"name": "Pet1", "status": "available"})
    listing = await client.get("/api/v3/pet/findByStatus", params={"status": "available"})
    assert [p["id"] for p in listing.json()][-2:] == [PROBE, 1]


async def test_delete_pet_always_succeeds(client: httpx2.AsyncClient) -> None:
    for _ in range(2):
        response = await client.delete("/api/v3/pet/10")
        assert (response.status_code, response.text, _ct(response)) == (
            200,
            "Pet deleted",
            "application/json",
        )
    gone = await client.get("/api/v3/pet/10")
    assert gone.status_code == 404
    xml = await client.delete("/api/v3/pet/10", headers=XML)
    assert _ct(xml) == "application/xml"  # 000193
    bad = await client.delete("/api/v3/pet/abc")
    assert bad.status_code == 400


async def test_upload_image(client: httpx2.AsyncClient) -> None:
    octet = {"Content-Type": "application/octet-stream"}
    ok = await client.post("/api/v3/pet/1/uploadImage", content=b"PNG", headers=octet)
    photos = ok.json()["photoUrls"]
    assert photos[:2] == ["test1", "test2"]
    assert re.fullmatch(r"/tmp/inflector\d+\.tmp", photos[2])
    empty = await client.post("/api/v3/pet/1/uploadImage", content=b"", headers=octet)
    assert (empty.status_code, empty.text, _ct(empty)) == (
        400,
        "No file uploaded",
        "application/json",
    )
    missing = await client.post(f"/api/v3/pet/{PROBE}/uploadImage", content=b"x", headers=octet)
    assert (missing.status_code, _ct(missing)) == (404, "application/json")
    wrong = await client.post("/api/v3/pet/1/uploadImage", json={"a": 1})
    assert wrong.status_code == 415
    bad = await client.post("/api/v3/pet/abc/uploadImage", content=b"x", headers=octet)
    assert bad.status_code == 400
    await client.post("/api/v3/pet", data={"id": str(PROBE), "name": "formed"})
    no_list = await client.post(f"/api/v3/pet/{PROBE}/uploadImage", content=b"x", headers=octet)
    assert no_list.status_code == 500


# -- store -----------------------------------------------------------------------------------------


async def test_inventory_follows_orders(client: httpx2.AsyncClient) -> None:
    start = await client.get("/api/v3/store/inventory")
    assert start.content == b'{"approved":7,"placed":-19}'  # 000351
    order = {"id": 9837420001, "petId": 1, "quantity": 3, "status": "placed"}
    await client.post("/api/v3/store/order", json=order)
    after = await client.get("/api/v3/store/inventory")
    assert after.json() == {"approved": 7, "placed": -16}  # 000210
    await client.post("/api/v3/store/order", json={"id": 9837420002, "petId": 1, "quantity": 1})
    broken = await client.get("/api/v3/store/inventory")
    assert broken.status_code == 500  # 000213
    await client.delete("/api/v3/store/order/9837420002")
    await client.post("/api/v3/store/order", json={"id": 9837420003, "status": "delivered"})
    await client.post(
        "/api/v3/store/order", json={"id": 9837420004, "quantity": 2, "status": "delivered"}
    )
    summed = await client.get("/api/v3/store/inventory")
    assert summed.json()["delivered"] == 2  # a missing quantity is a primitive 0 (000357)


async def test_inventory_xml(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v3/store/inventory", headers=XML)
    assert response.text == "<HashMap><approved>7</approved><placed>-19</placed></HashMap>"


async def test_place_order_normalizes(client: httpx2.AsyncClient) -> None:
    sent = {
        "id": 9837420003,
        "petId": 198772,
        "quantity": 2,
        "status": "approved",
        "complete": True,
        "shipDate": "2026-10-12T12:30:45+02:00",
    }
    response = await client.post("/api/v3/store/order", json=sent)
    assert response.content == (  # 000214
        b'{"id":9837420003,"petId":198772,"quantity":2,'
        b'"shipDate":"2026-10-12T10:30:45.000+00:00","status":"approved","complete":true}'
    )
    xml = await client.get("/api/v3/store/order/9837420003", headers=XML)
    assert xml.text == (
        "<Order><complete>true</complete><id>9837420003</id><petId>198772</petId>"
        "<quantity>2</quantity><shipDate>1791801045000</shipDate><status>approved</status></Order>"
    )
    date_only = await client.post(
        "/api/v3/store/order", json={"id": 1, "shipDate": "2026-10-12", "quantity": 1}
    )
    assert date_only.json()["shipDate"] == "2026-10-12T00:00:00.000+00:00"  # 000217
    epoch = await client.post("/api/v3/store/order", json={"id": 2, "shipDate": 0})
    assert epoch.json()["shipDate"] == "1970-01-01T00:00:00.000+00:00"
    noid = await client.post("/api/v3/store/order", json={"quantity": 1, "status": "placed"})
    assert noid.json()["id"] == 0
    bare = await client.post("/api/v3/store/order", json={"id": 9837420002})
    assert bare.content == b'{"id":9837420002,"petId":0,"quantity":0,"complete":false}'  # 000357
    bare_xml = await client.get("/api/v3/store/order/9837420002", headers=XML)
    assert bare_xml.text == (  # 000358
        "<Order><complete>false</complete><id>9837420002</id><petId>0</petId>"
        "<quantity>0</quantity><shipDate/><status/></Order>"
    )


@pytest.mark.parametrize(
    ("content", "content_type", "status"),
    [
        ('{"id": 1, "quantity": "abc"}', "application/json", 400),
        ('{"id": 1, "shipDate": "tomorrow"}', "application/json", 400),
        ('{"id": 1, "shipDate": true}', "application/json", 400),
        ('{"id": 1, "complete": "maybe"}', "application/json", 400),
        ('{"id": ', "application/json", 400),
        ("x", "text/plain", 415),
        ("id=1", "application/x-www-form-urlencoded", 500),
        ("", "application/json", 500),
        ("null", "application/json", 500),
    ],
)
async def test_place_order_rejections(
    client: httpx2.AsyncClient, content: str, content_type: str, status: int
) -> None:
    response = await client.post(
        "/api/v3/store/order", content=content, headers={"Content-Type": content_type}
    )
    assert response.status_code == status


async def test_place_order_from_xml(client: httpx2.AsyncClient) -> None:
    body = "<Order><id>7</id><quantity>2</quantity><complete>true</complete></Order>"
    response = await client.post(
        "/api/v3/store/order", content=body, headers={"Content-Type": "application/xml"}
    )
    assert response.json() == {"id": 7, "petId": 0, "quantity": 2, "complete": True}


async def test_order_get_and_delete(client: httpx2.AsyncClient) -> None:
    found = await client.get("/api/v3/store/order/10")
    assert found.json()["shipDate"] == "2015-07-20T22:49:04.000+00:00"
    missing = await client.get("/api/v3/store/order/1")
    assert (missing.status_code, missing.text, _ct(missing)) == (
        404,
        "Order not found",
        "application/xml",
    )
    bad = await client.get("/api/v3/store/order/abc")
    assert bad.status_code == 400
    for _ in range(2):
        deleted = await client.delete("/api/v3/store/order/10")
        assert (deleted.status_code, deleted.content, _ct(deleted)) == (200, b"", None)
    assert (await client.get("/api/v3/store/order/10")).status_code == 404
    assert (await client.delete("/api/v3/store/order/abc")).status_code == 400


# -- users -----------------------------------------------------------------------------------------


async def test_seeded_user(client: httpx2.AsyncClient) -> None:
    response = await client.get(f"/api/v3/user/{USER1}")
    assert response.json()["password"] == "XXXXXXXXXXX"  # 000108
    assert list(response.json()) == [
        "id",
        "username",
        "firstName",
        "lastName",
        "email",
        "password",
        "phone",
        "userStatus",
    ]
    xml = await client.get(f"/api/v3/user/{USER1}", headers=XML)
    assert xml.text.startswith(f"<User><id>1</id><username>{USER1}</username>")


async def test_poisoned_user_store(client: httpx2.AsyncClient) -> None:
    unknown = await client.get("/api/v3/user/someone-else")
    assert unknown.status_code == 500  # 000100
    user = {"id": 5, "username": "probe", "firstName": "P"}
    assert (await client.post("/api/v3/user", json=user)).status_code == 500
    assert (await client.post("/api/v3/user", data={"username": "probe"})).status_code == 500
    assert (await client.post("/api/v3/user/createWithList", json=[user])).status_code == 500
    assert (await client.put(f"/api/v3/user/{USER1}", json=user)).status_code == 500
    assert (await client.delete(f"/api/v3/user/{USER1}")).status_code == 500
    assert (await client.get(f"/api/v3/user/{USER1}")).status_code == 200


async def test_user_body_errors(client: httpx2.AsyncClient) -> None:
    bad = await client.post("/api/v3/user", json={"username": "x", "userStatus": "x"})
    assert bad.json()["message"] == (
        "Input error: unable to convert input to io.swagger.petstore.model.User"
    )
    empty_list = await client.post("/api/v3/user/createWithList", json=[])
    assert (empty_list.status_code, empty_list.text) == (400, "No User provided. Try again?")
    not_list = await client.post("/api/v3/user/createWithList", json={"username": "x"})
    assert not_list.json()["message"].endswith("io.swagger.petstore.model.User[]")
    text = await client.post("/api/v3/user", content="x", headers={"Content-Type": "text/plain"})
    assert text.status_code == 415
    text_list = await client.post(
        "/api/v3/user/createWithList", content="x", headers={"Content-Type": "text/plain"}
    )
    assert text_list.status_code == 415
    nothing = await client.post(
        "/api/v3/user", content="null", headers={"Content-Type": "application/json"}
    )
    assert nothing.status_code == 500
    bad_form = await client.post("/api/v3/user", data={"username": "x", "userStatus": "high"})
    assert bad_form.status_code == 400
    bad_put = await client.put(
        f"/api/v3/user/{USER1}", content="{x", headers={"Content-Type": "application/json"}
    )
    assert bad_put.status_code == 400


async def test_healthy_user_store(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "USERS_POISONED", False)
    assert (await client.get("/api/v3/user/nobody")).text == "User not found"
    created = await client.post(
        "/api/v3/user", json={"username": "probe", "id": 1, "password": "pw"}
    )
    assert created.json() == {"id": 1, "username": "probe", "password": "pw", "userStatus": 0}
    assert (await client.get("/api/v3/user/probe")).status_code == 200
    form = await client.post(
        "/api/v3/user", data={"username": "formed", "userStatus": "2", "id": "3"}
    )
    assert form.json()["password"] == "XXXXXXXXXXX"
    assert (await client.post("/api/v3/user", json={"firstName": "x"})).status_code == 200
    listed = await client.post(
        "/api/v3/user/createWithList", json=[{"username": "a"}, {"username": "b"}]
    )
    assert [u["username"] for u in listed.json()] == ["a", "b"]
    xml_list = await client.post(
        "/api/v3/user/createWithList",
        content="<User><item><username>c</username></item></User>",
        headers={"Content-Type": "application/xml"},
    )
    assert xml_list.status_code == 415  # 000415
    nameless = await client.post("/api/v3/user/createWithList", json=[{"firstName": "x"}])
    assert nameless.status_code == 200
    updated = await client.put("/api/v3/user/probe", json={"username": "renamed"})
    assert updated.json() == {"id": 0, "username": "renamed", "userStatus": 0}
    assert (await client.get("/api/v3/user/probe")).status_code == 404
    assert (await client.put("/api/v3/user/probe", json={"username": "x"})).status_code == 404
    assert (await client.put("/api/v3/user/renamed", json={"id": 1})).status_code == 200
    deleted = await client.delete("/api/v3/user/a")
    assert (deleted.status_code, deleted.content) == (200, b"")
    empty = await client.post(
        "/api/v3/user", content=b"", headers={"Content-Type": "application/xml"}
    )
    assert empty.status_code == 500
    no_id = await client.post("/api/v3/user", data={"username": "x", "userStatus": "1"})
    assert no_id.status_code == 500  # createUser(long id, …) unboxes a null id


async def test_login_and_logout(client: httpx2.AsyncClient) -> None:
    login = await client.get("/api/v3/user/login", params={"username": "a", "password": "b"})
    assert re.fullmatch(r"Logged in user session: \d+", login.text)
    assert login.headers["x-rate-limit"] == "5000"
    assert re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{2} \d{2}:\d{2}:\d{2} UTC \d{4}",
        login.headers["x-expires-after"],
    )
    xml = await client.get("/api/v3/user/login", headers=XML)
    assert _ct(xml) == "application/xml"
    logout = await client.get("/api/v3/user/logout")
    assert (logout.text, _ct(logout)) == ("User logged out", "application/json")


# -- negotiation and dates (recorded after review) -------------------------------------------------


async def test_plain_errors_follow_accept(client: httpx2.AsyncClient) -> None:
    for accept, expected in [
        ("application/json", "application/json"),  # 000420
        ("application/xml, application/json", "application/json"),  # 000421
        ("*/*", "application/xml"),  # 000024
        ("application/xml", "application/xml"),  # 000307
    ]:
        response = await client.get("/api/v3/pet/2", headers={"Accept": accept})
        assert (response.status_code, _ct(response)) == (404, expected)
    upload = await client.post(
        f"/api/v3/pet/{PROBE}/uploadImage",
        content=b"x",
        headers={"Content-Type": "application/octet-stream", "Accept": "application/xml"},
    )
    assert _ct(upload) == "application/xml"


async def test_yaml(client: httpx2.AsyncClient) -> None:
    yaml = {"Accept": "application/yaml"}
    pet = await client.get("/api/v3/pet/9", headers=yaml)
    assert _ct(pet) == "application/yaml"
    assert pet.text == (  # 000431
        '---\nid: 9\ncategory: null\nname: "friendly"\nphotoUrls:\n'
        '- "/tmp/inflector701163699009286637.tmp"\ntags: []\nstatus: null\n'
    )
    pet6 = await client.get("/api/v3/pet/6", headers=yaml)
    assert 'category:\n  id: 1\n  name: "Dogs"\n' in pet6.text
    assert '\ntags:\n- id: 1\n  name: "tag3"\n- id: 2\n' in pet6.text
    listing = await client.get("/api/v3/pet/findByStatus?status=pending", headers=yaml)
    assert listing.text == "--- []\n"
    order = await client.get("/api/v3/store/order/10", headers=yaml)
    assert order.text.startswith("---\nid: 10\npetId: 198772\n")
    inventory = await client.get("/api/v3/store/inventory", headers=yaml)
    assert inventory.text == "---\napproved: 7\nplaced: -19\n"
    user = await client.get("/api/v3/user/user1", headers=yaml)
    assert 'password: "XXXXXXXXXXX"' in user.text
    both = await client.get(
        "/api/v3/pet/9", headers={"Accept": "application/yaml, application/json"}
    )
    assert _ct(both) == "application/json"


async def test_ship_date_spellings(client: httpx2.AsyncClient) -> None:
    digits = await client.post(
        "/api/v3/store/order", json={"id": 1, "shipDate": "1437432544000", "status": "placed"}
    )
    assert digits.json()["shipDate"] == "2015-07-20T22:49:04.000+00:00"  # 000434
    space = await client.post(
        "/api/v3/store/order", json={"id": 2, "shipDate": "2026-10-12 10:00:00"}
    )
    assert space.status_code == 400  # 000435


async def test_null_tag_breaks_find_by_tags_when_healthy(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "TAGS_POISONED", False)
    await client.post("/api/v3/pet", json={"id": PROBE, "name": "x", "tags": [None]})
    response = await client.get("/api/v3/pet/findByTags", params={"tags": "x"})
    assert response.status_code == 500


async def test_head_options_and_framework_errors(client: httpx2.AsyncClient) -> None:
    head = await client.head("/api/v3/pet/1")
    assert (head.status_code, head.content, head.headers["content-length"]) == (200, b"", "83")
    options = await client.options("/api/v3/pet/1")
    assert options.status_code == 200
    assert options.headers["allow"] == "HEAD,DELETE,POST,GET,OPTIONS"  # 000430
    assert _ct(options) == "application/vnd.sun.wadl+xml"
    assert '<resource path="v3/pet/1">' in options.text
    assert options.headers["server"] == "Jetty(9.4.53.v20231009)"
    route = await client.get("/api/v3/does-not-exist", headers=XML)
    assert (
        route.text == "<ApiError><code>404</code><message>HTTP 404 Not Found</message></ApiError>"
    )
    method = await client.get("/api/v3/store/order", headers=XML)
    assert (method.status_code, _ct(method)) == (405, "application/xml")  # 000428
    plain = await client.get("/api/v3/does-not-exist")
    assert _ct(plain) == "application/json"
    spec = await client.get("/api/v3/openapi.yaml")
    assert spec.text.startswith("openapi: 3.0.4")
    assert spec.headers["access-control-allow-origin"] == "*"
    admin = await client.get("/__fauxcall/health")
    assert "server" not in admin.headers
