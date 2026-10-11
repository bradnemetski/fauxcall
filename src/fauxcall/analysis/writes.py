"""Learn what writes really do: persisted or not, server-assigned fields, id strategy.

Plan a write followed by a read-back of the same resource (``purpose: write_readback``); this module
compares them. Real APIs differ wildly — FakeRESTApi echoes a POST but never stores it; Petstore
stores it; many APIs add ids, timestamps and defaults the client never sent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from fauxcall import jsonpath
from fauxcall.model.common import Model, RawJson
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fauxcall.model.recording import CapturedResponse, Exchange
    from fauxcall.types import JsonValue

_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
RANDOM_INT_THRESHOLD: Final = 10**7
"""Server-assigned integer ids above this look random rather than incrementing."""
_GONE: Final = frozenset({404, 410})
_SUCCESS: Final = range(200, 300)

Echo = Literal["record", "request", "empty", "message", "unknown"]
IdStrategy = Literal["increment", "uuid4", "random_int", "client", "unknown"]


class WriteEvidence(Model):
    """What one write + read-back pair shows."""

    write: str = Field(description="Exchange id of the write.")
    readback: str | None = Field(description="Exchange id of the read-back.")
    write_status: int | None
    readback_status: int | None
    persisted: bool | None = Field(
        description=(
            "None without a conclusive read-back: none was made, it got no response, or it "
            "failed for another reason (401, 5xx …). For a DELETE, True means the resource is gone."
        )
    )
    echo: Echo
    server_fields: list[str] = Field(default_factory=list, description="Fields the server added.")
    changed_fields: list[str] = Field(
        default_factory=list, description="Fields the server altered."
    )
    id_field: str | None = None
    id_value: RawJson = None
    id_strategy: IdStrategy = "unknown"
    note: str = ""


def _top_level(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _same(a: JsonValue, b: JsonValue) -> bool:
    """JSON equality: like ``==``, except that ``true`` is not ``1`` and ``false`` is not ``0``."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    return a == b


def _contains(record: Mapping[str, JsonValue], fields: Mapping[str, JsonValue]) -> bool:
    return all(key in record and _same(record[key], value) for key, value in fields.items())


def _is_message(
    echoed: Mapping[str, JsonValue], sent: Mapping[str, JsonValue], id_field: str | None
) -> bool:
    """A small status object (Petstore's ``{"code":200,"type":"unknown","message":"10"}``).

    It shares no field with the request, has no id and holds only scalars — unlike the written
    record or an envelope around it (``{"data": {...}}``).
    """
    if set(echoed) & set(sent) or (id_field is not None and id_field in echoed):
        return False
    return not any(isinstance(value, (dict, list)) for value in echoed.values())


def _echo(
    response: CapturedResponse | None,
    sent: Mapping[str, JsonValue],
    echoed: Mapping[str, JsonValue],
    id_field: str | None,
) -> Echo:
    if response is None or response.body.kind == "empty":
        return "empty"
    if echoed and sent and set(echoed) == set(sent) and _contains(echoed, sent):
        return "request"
    if echoed and not _is_message(echoed, sent, id_field):
        return "record"
    if response.body.kind in {"text", "json"}:
        return "message"
    return "unknown"


def _id_strategy(
    sent: Mapping[str, JsonValue], echoed: Mapping[str, JsonValue], id_field: str | None
) -> tuple[IdStrategy, JsonValue]:
    if id_field is None or id_field not in echoed:
        return "unknown", None
    value = echoed[id_field]
    if id_field in sent and _same(sent[id_field], value):
        return "client", value
    if isinstance(value, str) and _UUID.match(value):
        return "uuid4", value
    if isinstance(value, int) and not isinstance(value, bool):
        return ("random_int" if value > RANDOM_INT_THRESHOLD else "increment"), value
    return "unknown", value


Verdict = tuple[bool | None, int | None, str]
"""Persisted?, the read-back's status and a note."""

_NOT_FOUND: Final = "the read-back did not find the written resource: writes are NOT persisted"


def _inconclusive(status: int) -> Verdict:
    return None, status, f"the read-back failed with HTTP {status}: inconclusive"


def _delete_persisted(status: int) -> Verdict:
    """After a DELETE, the resource should be gone."""
    if status in _GONE:
        return True, status, "the deleted resource is gone: deletes are persisted"
    if status in _SUCCESS:
        return False, status, "the deleted resource is still there: deletes are NOT persisted"
    return _inconclusive(status)


def _write_persisted(response: CapturedResponse, expected: Mapping[str, JsonValue]) -> Verdict:
    """After a create/update, the read-back should hold the written fields."""
    status = response.status
    if status in _GONE:
        return False, status, _NOT_FOUND
    if status not in _SUCCESS:
        return _inconclusive(status)
    body = response.body.parsed()
    if isinstance(body, list):
        if any(isinstance(item, dict) and _contains(item, expected) for item in body):
            return True, status, "the read-back list contains the written record"
        return False, status, _NOT_FOUND
    stored = _top_level(body)
    if not stored:
        return False, status, _NOT_FOUND
    if _contains(stored, expected):
        return True, status, "read-back matches the write"
    return False, status, "read-back differs from the write"


def _persistence(
    method: HttpMethod, readback: Exchange | None, expected: Mapping[str, JsonValue]
) -> Verdict:
    """Judge persistence (``expected``: the fields a stored record must hold)."""
    if readback is None:
        return None, None, ""
    if readback.response is None:
        return None, None, "the read-back got no response: inconclusive"
    if method is HttpMethod.DELETE:
        return _delete_persisted(readback.response.status)
    return _write_persisted(readback.response, expected)


def compare_write(
    write: Exchange, readback: Exchange | None, *, id_field: str | None = "id"
) -> WriteEvidence:
    """Compare a write with its read-back."""
    sent = _top_level(write.request.body.parsed())
    response = write.response
    echoed = _top_level(response.body.parsed() if response is not None else None)
    server_fields = [key for key in echoed if key not in sent]
    changed = [
        key for key, value in sent.items() if key in echoed and not _same(echoed[key], value)
    ]
    strategy, id_value = _id_strategy(sent, echoed, id_field)
    expected = {key: value for key, value in sent.items() if key not in changed}
    persisted, readback_status, note = _persistence(write.request.method, readback, expected)
    return WriteEvidence(
        write=write.id,
        readback=readback.id if readback is not None else None,
        write_status=response.status if response is not None else None,
        readback_status=readback_status,
        persisted=persisted,
        echo=_echo(response, sent, echoed, id_field),
        server_fields=server_fields,
        changed_fields=changed,
        id_field=id_field if id_field is not None and id_field in echoed else None,
        id_value=id_value,
        id_strategy=strategy,
        note=note,
    )


def describe(evidence: WriteEvidence) -> str:
    """One readable paragraph for the agent."""
    parts = [
        (
            f"write {evidence.write} -> HTTP {evidence.write_status}, "
            f"response echoes the {evidence.echo}"
        ),
        f"server-added fields: {', '.join(evidence.server_fields) or 'none'}",
    ]
    if evidence.changed_fields:
        parts.append(f"server changed: {', '.join(evidence.changed_fields)}")
    if evidence.id_field:
        parts.append(f"id `{evidence.id_field}` = {evidence.id_value!r} ({evidence.id_strategy})")
    if evidence.readback is not None:
        parts.append(
            f"read-back {evidence.readback} -> HTTP {evidence.readback_status}: {evidence.note}"
        )
    return "; ".join(parts)


def leaf_diff(a: JsonValue, b: JsonValue) -> list[str]:
    """Paths whose values differ between two documents (for reports).

    A leaf that is ``null`` on one side and missing on the other differs, and so do ``true``/``1``.
    """
    left = dict(jsonpath.leaf_paths(a, generalize_lists=False))
    right = dict(jsonpath.leaf_paths(b, generalize_lists=False))
    return sorted(
        path
        for path in left.keys() | right.keys()
        if path not in left or path not in right or not _same(left[path], right[path])
    )
