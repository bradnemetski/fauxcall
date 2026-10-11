"""Read-only end-to-end scenarios: identical assertions on the fake and the real Petstore.

The real service is a shared public sandbox: other people add and remove pets while these run, so
live listings are compared by shape (``values=False``); single records and errors by value.
Writes live in ``test_writes.py`` (they need ``--allow-real-writes`` against the real API).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fauxcall.model import RandomPolicy
from fauxcall.testing import CompareOptions

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

XML = {"Accept": "application/xml"}
LIVE = CompareOptions(values=False, lengths=False)
"""A live listing of the shared sandbox: compare shape only."""
LOGGED = CompareOptions(policies=[RandomPolicy(path="message", scope="body", generator="hex")])
"""A 500: its message carries a fresh log id."""
LOG_ID = re.compile(
    r"There was an error processing your request\. It has been logged \(ID: [0-9a-f]{16}\)"
)
CONVERT = "Input error: couldn't convert `{}` to type `class java.lang.Long`"


# -- pets ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["available", "pending", "sold"])
async def test_find_by_status(api: ApiClient, status: str) -> None:
    response = await api.get("/api/v3/pet/findByStatus", params={"status": status}, compare=LIVE)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    for pet in response.json():
        assert pet["status"] == status


async def test_find_by_status_takes_the_first_value(api: ApiClient) -> None:
    both = await api.get(
        "/api/v3/pet/findByStatus",
        params=[("status", "available"), ("status", "sold")],
        compare=LIVE,
    )
    assert {p["status"] for p in both.json()} <= {"available"}


@pytest.mark.parametrize("value", ["Available", "bogus", "", "pending,sold"])
async def test_find_by_status_rejects_values(api: ApiClient, value: str) -> None:
    response = await api.get("/api/v3/pet/findByStatus", params={"status": value})
    assert response.status_code == 400
    assert response.json()["message"] == (
        f"Input error: query parameter `status value `{value}` is not in the allowable values "
        "`[available, pending, sold]`"
    )


async def test_find_by_status_requires_status(api: ApiClient) -> None:
    response = await api.get("/api/v3/pet/findByStatus")
    assert response.json() == {
        "code": 400,
        "message": "Input error: missing required query parameter `status`",
    }


async def test_find_by_status_as_xml(api: ApiClient) -> None:
    response = await api.get(
        "/api/v3/pet/findByStatus", params={"status": "available"}, headers=XML, compare=LIVE
    )
    assert response.headers["content-type"] == "application/xml"
    assert response.text.startswith(("<ArrayList>", "<ArrayList/>"))


async def test_listing_and_detail_agree(api: ApiClient) -> None:
    listing = await api.get(
        "/api/v3/pet/findByStatus", params={"status": "available"}, compare=LIVE
    )
    for pet in listing.json()[:3]:
        detail = await api.get(f"/api/v3/pet/{pet['id']}", compare=LIVE)
        assert detail.status_code == 200
        assert detail.json() == pet


@pytest.mark.parametrize("tags", [["tag1"], ["tag1", "tag2"], ["automation"], ["no-such-tag"]])
async def test_find_by_tags_is_broken_in_the_sandbox(api: ApiClient, tags: list[str]) -> None:
    response = await api.get(
        "/api/v3/pet/findByTags", params=[("tags", t) for t in tags], compare=LOGGED
    )
    assert response.status_code == 500
    assert LOG_ID.fullmatch(response.json()["message"])


async def test_find_by_tags_errors(api: ApiClient) -> None:
    missing = await api.get("/api/v3/pet/findByTags")
    assert missing.json() == {
        "code": 400,
        "message": "Input error: missing required query parameter `tags`",
    }
    empty = await api.get("/api/v3/pet/findByTags", params={"tags": ""})
    assert (empty.status_code, empty.text) == (400, "No tags provided. Try again?")
    assert empty.headers["content-type"] == "application/xml"
    xml = await api.get("/api/v3/pet/findByTags", params={"tags": "x"}, headers=XML, compare=LIVE)
    assert xml.status_code == 500
    assert xml.text.startswith("<ApiError><code>500</code>")


@pytest.mark.parametrize("pet_id", [1, 6, 9, 10])
async def test_get_pet(api: ApiClient, pet_id: int) -> None:
    response = await api.get(f"/api/v3/pet/{pet_id}", compare=LIVE)
    if response.status_code == 404:
        pytest.skip(f"pet {pet_id} was removed from the shared sandbox")
    assert response.json()["id"] == pet_id


@pytest.mark.parametrize("pet_id", [1, 6, 9])
async def test_get_pet_as_xml(api: ApiClient, pet_id: int) -> None:
    response = await api.get(f"/api/v3/pet/{pet_id}", headers=XML)
    assert response.headers["content-type"] == "application/xml"
    assert response.text.startswith(f"<Pet><id>{pet_id}</id>")


@pytest.mark.parametrize(
    "accept", ["application/json", "*/*", "text/plain", "application/xml, application/json"]
)
async def test_everything_but_exact_xml_gets_json(api: ApiClient, accept: str) -> None:
    response = await api.get("/api/v3/pet/1", headers={"Accept": accept})
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("pet_id", [2, 3, 0, -1, 99999, 9837410010])
async def test_missing_pet(api: ApiClient, pet_id: int) -> None:
    response = await api.get(f"/api/v3/pet/{pet_id}")
    assert (response.status_code, response.text) == (404, "Pet not found")
    assert response.headers["content-type"] == "application/xml"


@pytest.mark.parametrize("raw", ["abc", "1.5", "99999999999999999999", "%20", "findbystatus"])
async def test_malformed_pet_id(api: ApiClient, raw: str) -> None:
    response = await api.get(f"/api/v3/pet/{raw}")
    assert response.status_code == 400
    shown = " " if raw == "%20" else raw
    assert response.json() == {"code": 400, "message": CONVERT.format(shown)}


async def test_malformed_pet_id_as_xml(api: ApiClient) -> None:
    response = await api.get("/api/v3/pet/abc", headers=XML)
    assert response.text == (
        f"<ApiError><code>400</code><message>{CONVERT.format('abc')}</message></ApiError>"
    )


# -- routing ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/v3/Pet/1", "/api/v3/does-not-exist", "/api/v4/pet/1", "/api/v3/Store/inventory"]
)
async def test_unknown_routes(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.json() == {"code": 404, "message": "HTTP 404 Not Found"}


@pytest.mark.parametrize("path", ["/api/v3/pet/1/", "/api/v3/store/inventory/"])
async def test_trailing_slash_is_ignored(api: ApiClient, path: str) -> None:
    response = await api.get(path, compare=LIVE)
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/v3/pet", "/api/v3/store/order", "/api/v3/user"])
async def test_method_not_allowed(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.json() == {"code": 405, "message": "HTTP 405 Method Not Allowed"}


async def test_no_authentication_is_enforced(api: ApiClient) -> None:
    for headers in ({"api_key": "invalid"}, {"Authorization": "Bearer invalid"}, {}):
        response = await api.get("/api/v3/pet/1", headers=headers)
        assert response.status_code == 200


# -- store -----------------------------------------------------------------------------------------


async def test_inventory(api: ApiClient) -> None:
    response = await api.get("/api/v3/store/inventory", compare=LIVE)
    assert response.status_code == 200
    assert all(isinstance(v, int) for v in response.json().values())
    xml = await api.get("/api/v3/store/inventory", headers=XML, compare=LIVE)
    assert xml.text.startswith(("<HashMap>", "<HashMap/>"))


@pytest.mark.parametrize("order_id", [10, 0])
async def test_get_order(api: ApiClient, order_id: int) -> None:
    response = await api.get(f"/api/v3/store/order/{order_id}", compare=LIVE)
    assert response.status_code == 200
    order = response.json()
    assert order["id"] == order_id
    assert list(order)[:3] == ["id", "petId", "quantity"]
    assert isinstance(order["complete"], bool)


async def test_get_order_as_xml(api: ApiClient) -> None:
    response = await api.get("/api/v3/store/order/10", headers=XML)
    assert response.text == (
        "<Order><complete>true</complete><id>10</id><petId>198772</petId><quantity>7</quantity>"
        "<shipDate>1437432544000</shipDate><status>approved</status></Order>"
    )


@pytest.mark.parametrize("order_id", [1, 5, -1, 11, 9837420001])
async def test_missing_order(api: ApiClient, order_id: int) -> None:
    response = await api.get(f"/api/v3/store/order/{order_id}")
    assert (response.status_code, response.text) == (404, "Order not found")
    assert response.headers["content-type"] == "application/xml"


@pytest.mark.parametrize("raw", ["abc", "1.5", "99999999999999999999"])
async def test_malformed_order_id(api: ApiClient, raw: str) -> None:
    response = await api.get(f"/api/v3/store/order/{raw}")
    assert response.json() == {"code": 400, "message": CONVERT.format(raw)}


async def test_inventory_counts_the_orders_it_can_see(api: ApiClient) -> None:
    inventory = await api.get("/api/v3/store/inventory", compare=LIVE)
    order = await api.get("/api/v3/store/order/10", compare=LIVE)
    if order.status_code == 200:
        assert inventory.json().get(order.json()["status"]) is not None


# -- users -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("number", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
async def test_seeded_user(api: ApiClient, number: int) -> None:
    username = f"user{number}"  # public seed fixtures: not pseudonymized
    response = await api.get(f"/api/v3/user/{username}")
    assert response.status_code == 200
    user = response.json()
    assert user["id"] == number
    assert user["username"] == username
    assert user["password"] == "XXXXXXXXXXX"


async def test_seeded_user_as_xml(api: ApiClient) -> None:
    username = "user2"
    response = await api.get(f"/api/v3/user/{username}", headers=XML)
    assert response.headers["content-type"] == "application/xml"
    assert response.text.startswith(f"<User><id>2</id><username>{username}</username>")


@pytest.mark.parametrize("username", ["theUser", "nobody-here", "fauxcall-probe-1"])
async def test_unknown_user_hits_the_broken_record(api: ApiClient, username: str) -> None:
    response = await api.get(f"/api/v3/user/{username}", compare=LOGGED)
    assert response.status_code == 500
    assert LOG_ID.fullmatch(response.json()["message"])


async def test_login_always_succeeds(api: ApiClient) -> None:
    response = await api.get(
        "/api/v3/user/login",
        params={"username": "user1", "password": "wrong"},
    )
    assert response.status_code == 200
    assert re.fullmatch(r"Logged in user session: \d+", response.text)
    assert response.headers["x-rate-limit"] == "5000"
    assert re.fullmatch(
        r"\w{3} \w{3} \d{2} [\d:]{8} UTC \d{4}", response.headers["x-expires-after"]
    )


async def test_logout(api: ApiClient) -> None:
    for headers in ({}, XML):
        response = await api.get("/api/v3/user/logout", headers=headers)
        assert (response.status_code, response.text) == (200, "User logged out")


# -- negotiation and server behavior (recorded after review) ---------------------------------------


@pytest.mark.parametrize(
    ("accept", "media"),
    [
        ("application/json", "application/json"),
        ("application/xml, application/json", "application/json"),
        ("*/*", "application/xml"),
        ("application/xml", "application/xml"),
    ],
)
async def test_not_found_text_follows_accept(api: ApiClient, accept: str, media: str) -> None:
    pet = await api.get("/api/v3/pet/9837410004", headers={"Accept": accept})
    order = await api.get("/api/v3/store/order/9837420004", headers={"Accept": accept})
    assert (pet.status_code, pet.text) == (404, "Pet not found")
    assert (order.status_code, order.text) == (404, "Order not found")
    assert pet.headers["content-type"] == order.headers["content-type"] == media


async def test_find_by_tags_empty_as_json(api: ApiClient) -> None:
    response = await api.get(
        "/api/v3/pet/findByTags", params={"tags": ""}, headers={"Accept": "application/json"}
    )
    assert (response.status_code, response.headers["content-type"]) == (400, "application/json")


@pytest.mark.parametrize("path", ["/api/v3/does-not-exist", "/api/v3/store/order"])
async def test_framework_errors_as_xml(api: ApiClient, path: str) -> None:
    response = await api.get(path, headers=XML)
    assert response.headers["content-type"] == "application/xml"
    assert response.text.startswith("<ApiError><code>40")


async def test_head(api: ApiClient) -> None:
    response = await api.request("HEAD", "/api/v3/pet/9")
    assert (response.status_code, response.content) == (200, b"")
    assert response.headers["content-type"] == "application/json"


async def test_options_describes_the_resource(api: ApiClient) -> None:
    response = await api.request("OPTIONS", "/api/v3/pet/9", compare=LIVE)
    assert response.status_code == 200
    assert response.headers["allow"] == "HEAD,DELETE,POST,GET,OPTIONS"
    assert response.headers["content-type"] == "application/vnd.sun.wadl+xml"


async def test_yaml(api: ApiClient) -> None:
    response = await api.get("/api/v3/pet/9", headers={"Accept": "application/yaml"})
    assert response.headers["content-type"] == "application/yaml"
    assert response.text.startswith("---\nid: 9\ncategory: null\n")


async def test_openapi_documents(api: ApiClient) -> None:
    as_json = await api.get("/api/v3/openapi.json", compare=LIVE)
    assert as_json.json()["openapi"] == "3.0.4"
    as_yaml = await api.get("/api/v3/openapi.yaml", compare=LIVE)
    assert as_yaml.headers["content-type"] == "application/yaml"
