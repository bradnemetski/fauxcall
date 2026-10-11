from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.analysis.writes import WriteEvidence, compare_write, describe, leaf_diff
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

RECORDED = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def body(value: JsonValue, kind: str) -> Body:
    if kind == "json":
        return Body(kind="json", content_type="application/json", json_value=value)
    if kind == "text":
        return Body(kind="text", content_type="text/plain", text=str(value))
    if kind == "binary":
        return Body(kind="binary", content_type="image/png", sha256="0" * 64, size=3)
    return Body()


def exchange(
    number: int,
    method: HttpMethod,
    *,
    sent: JsonValue = None,
    status: int | None = 200,
    reply: JsonValue = None,
    kind: str = "json",
) -> Exchange:
    return Exchange(
        id=f"{number:06d}",
        endpoint=f"{method.value} /v1/pets",
        purpose="write_readback",
        started_at=RECORDED,
        started_ns=0,
        request=CapturedRequest(
            method=method,
            url="https://api.test/v1/pets",
            path="/v1/pets",
            body=Body(kind="json", json_value=sent) if sent is not None else Body(),
        ),
        response=None
        if status is None
        else CapturedResponse(status=status, body=body(reply, kind)),
    )


def write(sent: JsonValue, reply: JsonValue, *, status: int = 200, kind: str = "json") -> Exchange:
    return exchange(1, HttpMethod.POST, sent=sent, status=status, reply=reply, kind=kind)


def readback(reply: JsonValue, *, status: int | None = 200, kind: str = "json") -> Exchange:
    return exchange(2, HttpMethod.GET, status=status, reply=reply, kind=kind)


PET: JsonObject = {"id": 10, "name": "Rex", "status": "available"}
STORED_PET: JsonObject = {
    "id": 10,
    "name": "Rex",
    "photoUrls": [],
    "tags": [],
    "status": "available",
}


def test_echo_of_the_request_that_is_never_stored() -> None:
    activity: JsonObject = {"id": 0, "title": "Walk", "completed": False}
    evidence = compare_write(
        write(activity, activity), readback({"title": "Not Found"}, status=404)
    )
    assert evidence == WriteEvidence(
        write="000001",
        readback="000002",
        write_status=200,
        readback_status=404,
        persisted=False,
        echo="request",
        server_fields=[],
        changed_fields=[],
        id_field="id",
        id_value=0,
        id_strategy="client",
        note="the read-back did not find the written resource: writes are NOT persisted",
    )


def test_stored_record_with_server_added_fields() -> None:
    evidence = compare_write(write(PET, STORED_PET), readback(STORED_PET))
    assert evidence.echo == "record"
    assert evidence.server_fields == ["photoUrls", "tags"]
    assert evidence.persisted is True
    assert evidence.note == "read-back matches the write"
    assert evidence.id_strategy == "client"


@pytest.mark.parametrize(
    ("assigned", "strategy"),
    [
        (4, "increment"),
        (10**7, "increment"),
        (9_223_372_036_854_775_807, "random_int"),
        (UUID, "uuid4"),
        ("pet-4", "unknown"),
        (4.5, "unknown"),
        (True, "unknown"),
        (None, "unknown"),
    ],
)
def test_id_strategies(assigned: JsonValue, strategy: str) -> None:
    evidence = compare_write(write({"name": "Rex"}, {"id": assigned, "name": "Rex"}), None)
    assert evidence.id_strategy == strategy
    assert evidence.id_field == "id"
    assert evidence.id_value == assigned
    assert evidence.server_fields == ["id"]


def test_custom_missing_and_disabled_id_fields() -> None:
    reply: JsonObject = {"petId": UUID, "name": "Rex"}
    custom = compare_write(write({"name": "Rex"}, reply), None, id_field="petId")
    assert (custom.id_field, custom.id_strategy) == ("petId", "uuid4")
    missing = compare_write(write({"name": "Rex"}, reply), None)
    assert (missing.id_field, missing.id_value, missing.id_strategy) == (None, None, "unknown")
    disabled = compare_write(write({"name": "Rex"}, {"id": 4, "name": "Rex"}), None, id_field=None)
    assert (disabled.id_field, disabled.id_strategy, disabled.echo) == (None, "unknown", "record")


def test_changed_fields_are_not_held_against_persistence() -> None:
    sent: JsonObject = {"name": "rex", "status": "available", "active": 1}
    echoed: JsonObject = {"id": 4, "name": "Rex", "status": "available", "active": True}
    evidence = compare_write(write(sent, echoed), readback(echoed))
    assert evidence.changed_fields == ["name", "active"]
    assert evidence.echo == "record"
    assert evidence.persisted is True
    differs = compare_write(write(sent, echoed), readback({**echoed, "status": "sold"}))
    assert (differs.persisted, differs.note) == (False, "read-back differs from the write")


def test_message_echoes() -> None:
    petstore: JsonObject = {"code": 200, "type": "unknown", "message": "10"}
    assert compare_write(write(PET, petstore), None).echo == "message"
    assert compare_write(write(PET, "Created", kind="text"), None).echo == "message"
    assert compare_write(write(PET, "ok"), None).echo == "message"
    assert compare_write(write(PET, [STORED_PET]), None).echo == "message"
    assert compare_write(write(PET, {}), None).echo == "message"
    assert compare_write(write(None, {"state": "queued"}), None).echo == "message"


def test_record_echoes_without_shared_fields() -> None:
    assert compare_write(write(None, {"id": 5, "state": "queued"}), None).echo == "record"
    envelope: JsonObject = {"data": {"id": 10, "name": "Rex"}}
    assert compare_write(write(PET, envelope), None).echo == "record"


def test_empty_and_unknown_echoes() -> None:
    empty = compare_write(write(PET, None, status=204, kind="empty"), None)
    assert (empty.echo, empty.write_status, empty.server_fields) == ("empty", 204, [])
    lost = exchange(1, HttpMethod.POST, sent=PET, status=None)
    no_response = compare_write(lost, None)
    assert (no_response.echo, no_response.write_status) == ("empty", None)
    assert compare_write(write(PET, None, kind="binary"), None).echo == "unknown"


def test_no_read_back_means_no_verdict() -> None:
    evidence = compare_write(write(PET, STORED_PET), None)
    assert (evidence.readback, evidence.readback_status, evidence.persisted, evidence.note) == (
        None,
        None,
        None,
        "",
    )


@pytest.mark.parametrize(
    ("status", "reply", "persisted", "note"),
    [
        (410, None, False, "writes are NOT persisted"),
        (401, {"error": "unauthorized"}, None, "the read-back failed with HTTP 401: inconclusive"),
        (503, None, None, "the read-back failed with HTTP 503: inconclusive"),
        (302, None, None, "the read-back failed with HTTP 302: inconclusive"),
        (200, None, False, "writes are NOT persisted"),
        (200, {}, False, "writes are NOT persisted"),
        (200, [STORED_PET], True, "the read-back list contains the written record"),
        (200, [{"id": 11, "name": "Max"}, "noise"], False, "writes are NOT persisted"),
    ],
)
def test_read_back_verdicts(
    status: int, reply: JsonValue, persisted: bool | None, note: str
) -> None:
    kind = "empty" if reply is None else "json"
    evidence = compare_write(write(PET, STORED_PET), readback(reply, status=status, kind=kind))
    assert evidence.readback_status == status
    assert evidence.persisted is persisted
    assert evidence.note.endswith(note)


def test_read_back_without_a_response_is_inconclusive() -> None:
    evidence = compare_write(write(PET, STORED_PET), readback(None, status=None))
    assert (evidence.persisted, evidence.readback_status) == (None, None)
    assert evidence.note == "the read-back got no response: inconclusive"


@pytest.mark.parametrize(
    ("status", "persisted", "note"),
    [
        (404, True, "the deleted resource is gone: deletes are persisted"),
        (410, True, "the deleted resource is gone: deletes are persisted"),
        (200, False, "the deleted resource is still there: deletes are NOT persisted"),
        (500, None, "the read-back failed with HTTP 500: inconclusive"),
    ],
)
def test_delete_read_backs(status: int, persisted: bool | None, note: str) -> None:
    delete = exchange(1, HttpMethod.DELETE, reply={"code": 200, "type": "unknown", "message": "10"})
    evidence = compare_write(delete, readback(STORED_PET, status=status))
    assert (evidence.persisted, evidence.note) == (persisted, note)
    assert evidence.echo == "message"


def test_describe() -> None:
    sent: JsonObject = {"name": "rex"}
    evidence = compare_write(
        write(sent, {"id": 4, "name": "Rex"}), readback({"id": 4, "name": "Rex"})
    )
    assert describe(evidence) == (
        "write 000001 -> HTTP 200, response echoes the record; server-added fields: id; "
        "server changed: name; id `id` = 4 (increment); "
        "read-back 000002 -> HTTP 200: read-back matches the write"
    )
    minimal = compare_write(write(None, "Created", status=201, kind="text"), None)
    assert describe(minimal) == (
        "write 000001 -> HTTP 201, response echoes the message; server-added fields: none"
    )


def test_leaf_diff() -> None:
    assert leaf_diff({"a": 1, "b": [1, 2]}, {"a": 1.0, "b": [1, 2]}) == []
    assert leaf_diff({"a": None}, {}) == ["$.a"]
    assert leaf_diff({"a": True}, {"a": 1}) == ["$.a"]
    assert leaf_diff({"a": False, "b": 0}, {"a": False, "b": False}) == ["$.b"]
    assert leaf_diff({"items": [1, 2, 3]}, {"items": [1, 5]}) == ["$.items[1]", "$.items[2]"]
    assert leaf_diff({"x": {"y": "z"}}, {"x": {"y": "z", "w": None}}) == ["$.x.w"]
    assert leaf_diff("same", "same") == []
    assert leaf_diff("a", "b") == ["$"]


def test_nested_values_compare_as_json() -> None:
    sent: JsonObject = {"tags": [{"name": "a", "on": True}], "meta": {"n": 1}}
    same = compare_write(write(sent, sent), None)
    assert same.echo == "request"
    flipped: JsonObject = {"tags": [{"name": "a", "on": 1}], "meta": {"n": 1}}
    assert compare_write(write(sent, flipped), None).changed_fields == ["tags"]
    resized: JsonObject = {
        "tags": [{"name": "a", "on": True}, {"name": "b", "on": True}],
        "meta": {"n": 1},
    }
    assert compare_write(write(sent, resized), None).changed_fields == ["tags"]
    rekeyed: JsonObject = {"tags": [{"name": "a", "on": True}], "meta": {"m": 1}}
    assert compare_write(write(sent, rekeyed), None).changed_fields == ["meta"]
