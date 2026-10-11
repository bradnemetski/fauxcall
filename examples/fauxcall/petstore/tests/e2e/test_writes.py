"""Write scenarios: create, change and delete only our own ``fauxcall-probe`` records.

Against the real sandbox these need ``--allow-real-writes`` and the user's up-front approval
(25 calls per write endpoint for the whole build). Every scenario first checks that the ids it is
about to use are free in the shared sandbox (and skips if not), and cleans up in ``finally``.
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
LOGGED = CompareOptions(policies=[RandomPolicy(path="message", scope="body", generator="hex")])
UPLOADED = CompareOptions(values=False)
"""Upload responses carry a fresh server temp path in ``photoUrls``."""
OCTET = {"Content-Type": "application/octet-stream"}
P1, P2, P3, P4 = 9837410001, 9837410002, 9837410003, 9837410004
O1, O2 = 9837420001, 9837420002
TEMP = re.compile(r"/tmp/inflector\d+\.tmp")


async def _require_free(api: ApiClient, kind: str, *ids: int) -> None:
    """Skip when another sandbox user holds one of our probe ids: never touch their records."""
    for record_id in ids:
        response = await api.get(f"/api/v3/{kind}/{record_id}", label=f"guard {kind}")
        if response.status_code != 404:
            pytest.skip(f"{kind} {record_id} exists in the shared sandbox")


# -- pets ------------------------------------------------------------------------------------------


async def test_pet_lifecycle(api: ApiClient) -> None:
    await _require_free(api, "pet", P1)
    created = False
    try:
        body = {
            "id": P1,
            "category": {"id": 1, "name": "Dogs"},
            "name": "fauxcall-probe-1",
            "photoUrls": [],
            "tags": [{"id": 98374, "name": "fauxcall-tag"}],
            "status": "pending",
        }
        response = await api.post("/api/v3/pet", json=body)
        created = response.status_code == 200
        assert response.json() == body
        assert (await api.get(f"/api/v3/pet/{P1}")).json() == body

        replaced = await api.put(
            "/api/v3/pet",
            json={
                "id": P1,
                "name": "fauxcall-probe-1-put",
                "photoUrls": ["p"],
                "status": "available",
            },
        )
        assert replaced.json() == {
            "id": P1,
            "name": "fauxcall-probe-1-put",
            "photoUrls": ["p"],
            "tags": [],
            "status": "available",
        }

        both = await api.post(f"/api/v3/pet/{P1}", params={"name": "renamed", "status": "sold"})
        assert (both.json()["name"], both.json()["status"]) == ("renamed", "sold")
        name_only = await api.post(f"/api/v3/pet/{P1}", params={"name": "renamed-2"})
        assert "status" not in name_only.json()
        no_name = await api.post(f"/api/v3/pet/{P1}", params={"status": "pending"})
        assert (no_name.status_code, no_name.text) == (400, "No Name provided. Try again?")

        uploaded = await api.post(
            f"/api/v3/pet/{P1}/uploadImage",
            params={"additionalMetadata": "fauxcall"},
            content=b"FAKEPNGBYTES",
            headers=OCTET,
            compare=UPLOADED,
        )
        photos = uploaded.json()["photoUrls"]
        assert photos[0] == "p"
        assert TEMP.fullmatch(photos[1])
        assert (await api.get(f"/api/v3/pet/{P1}", compare=UPLOADED)).json() == uploaded.json()

        deleted = await api.delete(f"/api/v3/pet/{P1}")
        created = False
        assert (deleted.status_code, deleted.text) == (200, "Pet deleted")
        assert (await api.get(f"/api/v3/pet/{P1}")).status_code == 404
        again = await api.delete(f"/api/v3/pet/{P1}")
        assert (again.status_code, again.text) == (200, "Pet deleted")
    finally:
        if created:
            await api.delete(f"/api/v3/pet/{P1}", label="cleanup")


async def test_pet_write_quirks(api: ApiClient) -> None:
    await _require_free(api, "pet", P2, P3)
    try:
        loose = await api.post(
            "/api/v3/pet", json={"id": P2, "photoUrls": [], "status": "bogus", "color": "brown"}
        )
        assert loose.json() == {"id": P2, "photoUrls": [], "tags": [], "status": "bogus"}

        replaced = await api.post(
            "/api/v3/pet", json={"id": P2, "name": "fauxcall-probe-2", "status": "sold"}
        )
        assert replaced.json() == {
            "id": P2,
            "name": "fauxcall-probe-2",
            "photoUrls": [],
            "tags": [],
            "status": "sold",
        }
        sold = await api.get("/api/v3/pet/findByStatus", params={"status": "sold"}, compare=LIVE)
        assert sold.json()[-1]["id"] == P2  # the replaced pet moved to the end

        formed = await api.post(
            "/api/v3/pet", data={"id": str(P3), "name": "fauxcall-probe-3", "status": "pending"}
        )
        assert formed.json() == {"id": P3, "name": "fauxcall-probe-3", "status": "pending"}
        as_xml = await api.get(f"/api/v3/pet/{P3}", headers=XML)
        assert as_xml.text == (
            f"<Pet><id>{P3}</id><category/><name>fauxcall-probe-3</name>"
            "<status>pending</status></Pet>"
        )
        no_list = await api.post(
            f"/api/v3/pet/{P3}/uploadImage", content=b"x", headers=OCTET, compare=LOGGED
        )
        assert no_list.status_code == 500

        no_id = await api.post(
            "/api/v3/pet", json={"name": "fauxcall-probe-noid", "photoUrls": []}, compare=LOGGED
        )
        assert no_id.status_code == 500
    finally:
        for pet_id in (P2, P3):
            await api.delete(f"/api/v3/pet/{pet_id}", label="cleanup")


async def test_pet_writes_on_missing_pets(api: ApiClient) -> None:
    await _require_free(api, "pet", P4)
    put = await api.put("/api/v3/pet", json={"id": P4, "name": "x", "photoUrls": []})
    assert (put.status_code, put.text) == (404, "Pet not found")
    assert put.headers["content-type"] == "application/xml"
    form = await api.post(f"/api/v3/pet/{P4}", params={"name": "x"})
    assert (form.status_code, form.text) == (404, "Pet not found")
    upload = await api.post(f"/api/v3/pet/{P4}/uploadImage", content=b"x", headers=OCTET)
    assert (upload.status_code, upload.headers["content-type"]) == (404, "application/json")
    deleted = await api.delete(f"/api/v3/pet/{P4}")
    assert (deleted.status_code, deleted.text) == (200, "Pet deleted")
    bad = await api.delete("/api/v3/pet/abc")
    assert bad.status_code == 400


# -- orders ----------------------------------------------------------------------------------------


async def test_order_lifecycle_and_inventory(api: ApiClient) -> None:
    await _require_free(api, "store/order", O1, O2)
    try:
        before = await api.get("/api/v3/store/inventory", compare=LIVE)
        placed = await api.post(
            "/api/v3/store/order",
            json={
                "id": O1,
                "petId": 198772,
                "quantity": 3,
                "shipDate": "2026-10-12T12:30:45+02:00",
                "status": "placed",
            },
        )
        assert placed.json() == {
            "id": O1,
            "petId": 198772,
            "quantity": 3,
            "shipDate": "2026-10-12T10:30:45.000+00:00",
            "status": "placed",
            "complete": False,
        }
        after = await api.get("/api/v3/store/inventory", compare=LIVE)
        assert after.json()["placed"] == before.json().get("placed", 0) + 3

        as_xml = await api.get(f"/api/v3/store/order/{O1}", headers=XML)
        assert as_xml.text == (
            f"<Order><complete>false</complete><id>{O1}</id><petId>198772</petId>"
            "<quantity>3</quantity><shipDate>1791801045000</shipDate><status>placed</status>"
            "</Order>"
        )

        no_status = await api.post("/api/v3/store/order", json={"id": O2, "quantity": 1})
        assert no_status.json() == {"id": O2, "petId": 0, "quantity": 1, "complete": False}
        broken = await api.get("/api/v3/store/inventory", compare=LOGGED)
        assert broken.status_code == 500
        removed = await api.delete(f"/api/v3/store/order/{O2}")
        assert (removed.status_code, removed.content) == (200, b"")
        healed = await api.get("/api/v3/store/inventory", compare=LIVE)
        assert healed.status_code == 200

        deleted = await api.delete(f"/api/v3/store/order/{O1}")
        assert (deleted.status_code, deleted.content) == (200, b"")
        assert (await api.get(f"/api/v3/store/order/{O1}")).text == "Order not found"
        again = await api.delete(f"/api/v3/store/order/{O1}")
        assert again.status_code == 200
    finally:
        for order_id in (O2, O1):
            await api.delete(f"/api/v3/store/order/{order_id}", label="cleanup")


async def test_order_errors(api: ApiClient) -> None:
    bad_quantity = await api.post(
        "/api/v3/store/order", json={"id": O1, "petId": 1, "quantity": "abc"}
    )
    assert bad_quantity.json() == {
        "code": 400,
        "message": "Input error: unable to convert input to io.swagger.petstore.model.Order",
    }
    bad_date = await api.post("/api/v3/store/order", json={"id": O1, "shipDate": "tomorrow"})
    assert bad_date.status_code == 400
    bad_id = await api.delete("/api/v3/store/order/abc")
    assert bad_id.status_code == 400


# -- users (the sandbox's user store is broken: every write fails) ---------------------------------


async def test_user_writes_fail(api: ApiClient) -> None:
    probe = {
        "id": 9837430001,
        "username": "fauxcall-probe-1",
        "firstName": "Probe",
        "lastName": "Fauxcall",
        "email": "fauxcall-probe-1@example.com",
        "password": "fauxcall-pw",
        "phone": "555-0100",
        "userStatus": 1,
    }
    created = await api.post("/api/v3/user", json=probe, compare=LOGGED)
    assert created.status_code == 500
    listed = await api.post("/api/v3/user/createWithList", json=[probe], compare=LOGGED)
    assert listed.status_code == 500
    updated = await api.put("/api/v3/user/fauxcall-probe-1", json=probe, compare=LOGGED)
    assert updated.status_code == 500
    deleted = await api.delete("/api/v3/user/fauxcall-probe-1", compare=LOGGED)
    assert deleted.status_code == 500


async def test_user_write_validation(api: ApiClient) -> None:
    empty = await api.post("/api/v3/user/createWithList", json=[])
    assert (empty.status_code, empty.text) == (400, "No User provided. Try again?")
    bad = await api.post("/api/v3/user", json={"username": "fauxcall-probe-1", "userStatus": "x"})
    assert bad.json() == {
        "code": 400,
        "message": "Input error: unable to convert input to io.swagger.petstore.model.User",
    }
