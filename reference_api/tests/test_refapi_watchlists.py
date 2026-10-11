from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .helpers import DEMO, READONLY, error_of

if TYPE_CHECKING:
    from starlette.testclient import TestClient

    from fauxcall_reference_api.clock import ManualClock

CREATED = "2026-10-09T15:00:00.000Z"
BAD_NAME = "Invalid value for parameter 'name': must be a string of 1 to 50 characters."
BAD_SYMBOLS = "Invalid value for parameter 'symbols': must be an array of strings."
NOPE = "Unknown symbol 'NOPE'."


def create(
    client: TestClient, name: str = "Tech", symbols: list[str] | None = None
) -> dict[str, object]:
    body: dict[str, object] = {"name": name}
    if symbols is not None:
        body["symbols"] = symbols
    response = client.post("/v1/watchlists", headers=DEMO, json=body)
    assert response.status_code == 201
    result = response.json()
    assert isinstance(result, dict)
    return result


def test_create_and_read(client: TestClient, clock: ManualClock) -> None:
    clock.advance(0.1234567)
    response = client.post(
        "/v1/watchlists",
        headers=DEMO,
        json={"name": "Tech", "symbols": ["aapl", "MSFT", "AAPL", "7203.t"]},
    )
    assert response.status_code == 201
    assert response.headers["location"] == "/v1/watchlists/1001"
    expected = {
        "id": 1001,
        "name": "Tech",
        "symbols": ["AAPL", "MSFT", "7203.T"],
        "created_at": "2026-10-09T15:00:00.123Z",
        "updated_at": "2026-10-09T15:00:00.123Z",
    }
    assert response.json() == expected
    assert client.get("/v1/watchlists/1001", headers=DEMO).json() == expected
    assert client.get("/v1/watchlists/1001", headers=READONLY).json() == expected  # shared


def test_ids_increment_and_are_never_reused(client: TestClient) -> None:
    assert create(client)["id"] == 1001
    assert create(client, symbols=[])["id"] == 1002
    assert client.delete("/v1/watchlists/1002", headers=DEMO).status_code == 204
    third = create(client, name="x" * 50)
    assert third["id"] == 1003
    assert third["symbols"] == []
    listed = client.get("/v1/watchlists", headers=DEMO).json()
    assert [w["id"] for w in listed] == [1001, 1003]


def test_put_replaces(client: TestClient, clock: ManualClock) -> None:
    create(client, symbols=["AAPL"])
    clock.advance(2)
    response = client.put(
        "/v1/watchlists/1001", headers=DEMO, json={"name": "Autos", "symbols": ["tsla", "F"]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": 1001,
        "name": "Autos",
        "symbols": ["TSLA", "F"],
        "created_at": CREATED,
        "updated_at": "2026-10-09T15:00:02.000Z",
    }
    missing_symbols = client.put("/v1/watchlists/1001", headers=DEMO, json={"name": "x"})
    assert error_of(missing_symbols.json())["message"] == "Missing required parameter 'symbols'."


def test_patch_merges(client: TestClient, clock: ManualClock) -> None:
    create(client, symbols=["AAPL"])
    clock.advance(1)
    renamed = client.patch("/v1/watchlists/1001", headers=DEMO, json={"name": "Renamed"}).json()
    assert (renamed["name"], renamed["symbols"], renamed["updated_at"]) == (
        "Renamed",
        ["AAPL"],
        "2026-10-09T15:00:01.000Z",
    )
    clock.advance(1)
    resymboled = client.patch(
        "/v1/watchlists/1001", headers=DEMO, json={"symbols": ["nvda"]}
    ).json()
    assert (resymboled["name"], resymboled["symbols"]) == ("Renamed", ["NVDA"])
    clock.advance(1)
    touched = client.patch("/v1/watchlists/1001", headers=DEMO, json={}).json()
    assert touched["updated_at"] == "2026-10-09T15:00:03.000Z"
    assert touched["created_at"] == CREATED


@pytest.mark.parametrize(
    ("body", "param", "message"),
    [
        ({"name": None}, "name", BAD_NAME),
        ({"symbols": None}, "symbols", BAD_SYMBOLS),
        ({"name": ""}, "name", BAD_NAME),
        ({"symbols": ["NOPE"]}, "symbols", "Unknown symbol 'NOPE'."),
    ],
)
def test_patch_rejects(
    client: TestClient, body: dict[str, object], param: str, message: str
) -> None:
    create(client)
    response = client.patch("/v1/watchlists/1001", headers=DEMO, json=body)
    assert response.status_code == 400
    error = error_of(response.json())
    assert (error["param"], error["message"]) == (param, message)


@pytest.mark.parametrize(
    ("body", "code", "param", "message"),
    [
        ({}, "missing_parameter", "name", "Missing required parameter 'name'."),
        ({"name": ""}, "invalid_parameter", "name", BAD_NAME),
        ({"name": "x" * 51}, "invalid_parameter", "name", BAD_NAME),
        ({"name": 5}, "invalid_parameter", "name", BAD_NAME),
        ({"name": "x", "symbols": "AAPL"}, "invalid_parameter", "symbols", BAD_SYMBOLS),
        ({"name": "x", "symbols": [1]}, "invalid_parameter", "symbols", BAD_SYMBOLS),
        ({"name": "x", "symbols": ["AAPL", "NOPE", "X"]}, "unknown_symbol", "symbols", NOPE),
    ],
)
def test_create_rejects(
    client: TestClient, body: dict[str, object], code: str, param: str, message: str
) -> None:
    response = client.post("/v1/watchlists", headers=DEMO, json=body)
    assert response.status_code == 400
    assert error_of(response.json()) == {
        "code": code,
        "message": message,
        "request_id": response.json()["error"]["request_id"],
        "param": param,
    }
    assert client.get("/v1/watchlists", headers=DEMO).json() == []


def test_unknown_ids(client: TestClient) -> None:
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        response = client.request(
            method, "/v1/watchlists/999", headers=DEMO, json={"name": "x", "symbols": []}
        )
        assert response.status_code == 404
        assert error_of(response.json()) == {
            "code": "watchlist_not_found",
            "message": "No watchlist with id 999.",
            "request_id": response.json()["error"]["request_id"],
        }
    bad = client.get("/v1/watchlists/abc", headers=DEMO)
    assert bad.status_code == 400
    assert (
        error_of(bad.json())["message"] == "Invalid value for parameter 'id': must be an integer."
    )


def test_unknown_id_is_checked_before_symbols(client: TestClient) -> None:
    response = client.put(
        "/v1/watchlists/999", headers=DEMO, json={"name": "x", "symbols": ["NOPE"]}
    )
    assert response.status_code == 404


def test_extra_fields_are_ignored_and_whitespace_names_count(client: TestClient) -> None:
    response = client.post(
        "/v1/watchlists", headers=DEMO, json={"name": " ", "symbols": [], "color": "red"}
    )
    assert response.status_code == 201
    assert response.json()["name"] == " "
    assert "color" not in response.json()


def test_delete(client: TestClient) -> None:
    create(client)
    response = client.delete("/v1/watchlists/1001", headers=DEMO)
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/v1/watchlists/1001", headers=DEMO).status_code == 404
    assert client.delete("/v1/watchlists/1001", headers=DEMO).status_code == 404
