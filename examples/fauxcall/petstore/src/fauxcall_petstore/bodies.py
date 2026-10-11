"""Request bodies read the way the real server (Jackson, default settings) reads them.

Recorded: unknown properties are dropped (000140, 000232); missing properties stay null, with no
validation of ``required`` or enums (000135, 000137, 000142, 000220); a value of the wrong type
fails the whole body (000139, 000222, 000223, 000253); malformed JSON fails it too (000144).
Jackson's default scalar coercions (numeric strings to numbers, numbers to strings, floats
truncated to integers) follow the library's documented defaults.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl
from xml.etree.ElementTree import Element, ParseError, fromstring

from fauxcall_petstore.wire import format_ship_date, parse_date

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.types import JsonValue

LONG_MIN = -(2**63)
LONG_MAX = 2**63 - 1
INT_MIN = -(2**31)
INT_MAX = 2**31 - 1

_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class Unreadable(Exception):  # noqa: N818 - mirrors Jackson's "unable to convert"
    """The body cannot be converted to the target model."""


class MissingPrimitive(Exception):  # noqa: N818 - mirrors a NullPointerException
    """A form left out a value the server unboxes into a primitive."""


ABSENT: object = object()
"""Marker for a body that holds JSON ``null`` (the controller's "No … provided" branch)."""


def parse_long(text: str, low: int = LONG_MIN, high: int = LONG_MAX) -> int | None:
    """``Long.parseLong`` (or ``Integer.parseInt`` with narrower bounds); ``None`` when invalid."""
    if not _INTEGER.match(text):
        return None
    value = int(text)
    return value if low <= value <= high else None


def _integer(value: JsonValue, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise Unreadable
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        result = int(value)
    elif isinstance(value, str):
        if value == "":
            return None
        if not _NUMBER.match(value):
            raise Unreadable
        result = int(float(value)) if not _INTEGER.match(value) else int(value)
    else:
        raise Unreadable
    if not low <= result <= high:
        raise Unreadable
    return result


def long_value(value: JsonValue) -> int | None:
    """A Java ``Long`` property."""
    return _integer(value, LONG_MIN, LONG_MAX)


def int_value(value: JsonValue) -> int | None:
    """A Java ``Integer`` property."""
    return _integer(value, INT_MIN, INT_MAX)


def string_value(value: JsonValue) -> str | None:
    """A Java ``String`` property (scalars are coerced, structures refused)."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    raise Unreadable


def bool_value(value: JsonValue) -> bool:
    """A Java ``boolean`` (primitive: null reads as false)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str) and value in {"true", "false", ""}:
        return value == "true"
    raise Unreadable


def date_value(value: JsonValue) -> str | None:
    """A ``java.util.Date`` property, normalized to the server's spelling."""
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        from datetime import UTC, datetime  # noqa: PLC0415 - rare path

        return format_ship_date(datetime.fromtimestamp(value / 1000, tz=UTC))
    if not isinstance(value, str):
        raise Unreadable
    moment = parse_date(value)
    if moment is None:
        raise Unreadable
    return format_ship_date(moment)


def _id_name(value: JsonValue) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Unreadable
    return {"id": long_value(value.get("id")), "name": string_value(value.get("name"))}


def _string_list(value: JsonValue) -> list[JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise Unreadable
    return [string_value(v) for v in value]


def _tag_list(value: JsonValue) -> list[JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise Unreadable
    return [_id_name(v) for v in value]


def pet_from(data: JsonValue) -> dict[str, JsonValue]:
    """A ``Pet`` from a decoded JSON/XML object (photoUrls and tags default to ``[]``)."""
    if not isinstance(data, dict):
        raise Unreadable
    photos = _string_list(data.get("photoUrls"))
    tags = _tag_list(data.get("tags"))
    return {
        "id": long_value(data.get("id")),
        "category": _id_name(data.get("category")),
        "name": string_value(data.get("name")),
        "photoUrls": [] if photos is None else photos,
        "tags": [] if tags is None else tags,
        "status": string_value(data.get("status")),
    }


def pet_from_form(form: dict[str, str]) -> dict[str, JsonValue]:
    """A ``Pet`` from form fields: only id, name and status are read; no defaults (000145)."""
    return {
        "id": _form_long(form.get("id")),
        "name": form.get("name"),
        "status": form.get("status"),
    }


def _form_long(text: str | None) -> int | None:
    if text is None or text == "":
        return None
    value = parse_long(text)
    if value is None:
        raise Unreadable
    return value


def order_from(data: JsonValue) -> dict[str, JsonValue]:
    """An ``Order``.

    The primitives ``id``, ``petId`` and ``quantity`` default to 0 and ``complete`` to false
    (000357); ``shipDate`` and ``status`` stay null.
    """
    if not isinstance(data, dict):
        raise Unreadable
    return {
        "id": long_value(data.get("id")) or 0,
        "petId": long_value(data.get("petId")) or 0,
        "quantity": int_value(data.get("quantity")) or 0,
        "shipDate": date_value(data.get("shipDate")),
        "status": string_value(data.get("status")),
        "complete": bool_value(data.get("complete")),
    }


def user_from(data: JsonValue) -> dict[str, JsonValue]:
    """A ``User`` (the primitives ``id`` and ``userStatus`` default to 0: ``User.java``)."""
    if not isinstance(data, dict):
        raise Unreadable
    return {
        "id": long_value(data.get("id")) or 0,
        "username": string_value(data.get("username")),
        "firstName": string_value(data.get("firstName")),
        "lastName": string_value(data.get("lastName")),
        "email": string_value(data.get("email")),
        "password": string_value(data.get("password")),
        "phone": string_value(data.get("phone")),
        "userStatus": int_value(data.get("userStatus")) or 0,
    }


def user_from_form(form: dict[str, str]) -> dict[str, JsonValue]:
    """A ``User`` from form fields (``UserData.createUser``: the password is not taken).

    ``createUser(long id, …, int userStatus)`` unboxes both, so a form without them fails
    (:class:`MissingPrimitive`).
    """
    status = form.get("userStatus")
    user_status = None if status in {None, ""} else parse_long(str(status), INT_MIN, INT_MAX)
    if status not in {None, ""} and user_status is None:
        raise Unreadable
    user_id = _form_long(form.get("id"))
    if user_id is None or user_status is None:
        raise MissingPrimitive
    return {
        "id": user_id,
        "username": form.get("username"),
        "firstName": form.get("firstName"),
        "lastName": form.get("lastName"),
        "email": form.get("email"),
        "password": "XXXXXXXXXXX",
        "phone": form.get("phone"),
        "userStatus": user_status,
    }


# -- decoding --------------------------------------------------------------------------------------


def decode_json(raw: bytes) -> JsonValue:
    """JSON text, or :class:`Unreadable`; ``null`` (or nothing) becomes :data:`ABSENT`."""
    if not raw.strip():
        return ABSENT  # type: ignore[return-value]
    try:
        value: JsonValue = json.loads(raw)
    except ValueError as exc:
        raise Unreadable from exc
    return ABSENT if value is None else value  # type: ignore[return-value]


def _xml_value(element: Element) -> JsonValue:
    children = list(element)
    if not children:
        return element.text if element.text is not None else None
    names = {child.tag for child in children}
    if len(children) > 1 and len(names) == 1:
        return [_xml_value(child) for child in children]
    if len(names) == 1 and element.tag.endswith("s") and element.tag[:-1] == children[0].tag:
        return [_xml_value(child) for child in children]
    return {child.tag: _xml_value(child) for child in children}


def decode_xml(raw: bytes) -> JsonValue:
    """An XML body as nested values (wrapped lists become arrays)."""
    if not raw.strip():
        return ABSENT  # type: ignore[return-value]
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise Unreadable
    try:
        root = fromstring(raw)  # noqa: S314 - DOCTYPE/ENTITY refused above
    except ParseError as exc:
        raise Unreadable from exc
    value = _xml_value(root)
    if isinstance(value, dict) and list(value) == ["item"]:
        return [value["item"]]  # a one-element array: <User><item>…</item></User>
    return value if isinstance(value, dict | list) else {}


def decode_form(raw: bytes) -> dict[str, str]:
    """An ``application/x-www-form-urlencoded`` body (first value of each field)."""
    form: dict[str, str] = {}
    for name, value in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True):
        form.setdefault(name, value)
    return form


def media_type(content_type: str | None) -> str:
    """The bare media type of a ``Content-Type`` header."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def read_model(
    raw: bytes,
    content_type: str | None,
    convert: Callable[[JsonValue], dict[str, JsonValue]],
) -> dict[str, JsonValue] | object:
    """Decode a JSON or XML body and convert it; :data:`ABSENT` for a null body."""
    decoded = decode_xml(raw) if media_type(content_type) == "application/xml" else decode_json(raw)
    if decoded is ABSENT:
        return ABSENT
    return convert(decoded)


def read_users(raw: bytes, media: str) -> list[dict[str, JsonValue]]:
    """A ``User[]`` body (empty for a null body)."""
    decoded = decode_xml(raw) if media == "application/xml" else decode_json(raw)
    if decoded is ABSENT:
        return []
    if not isinstance(decoded, list):
        raise Unreadable
    return [user_from(u) for u in decoded]
