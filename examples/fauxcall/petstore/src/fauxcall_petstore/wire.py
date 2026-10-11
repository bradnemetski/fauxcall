"""How the real Petstore (Swagger Inflector on Jetty, Jackson) spells its responses.

Everything here reproduces recorded behavior:

- Content negotiation (``Util.getMediaType``): XML only when the first ``Accept`` header is exactly
  ``application/xml``; JSON otherwise, including ``*/*``, ``text/plain`` and mixed lists
  (recordings 000051, 000313-000315).
- Jackson XML: every property is written, ``null`` as an empty element (000305) except null
  lists, which are left out (000355); lists are wrapped (``<photoUrls><photoUrl>…``); a list body
  is ``<ArrayList><item>…`` (000302, 000303); ``Order`` properties are alphabetical with
  ``shipDate`` as epoch milliseconds (000309, 000358); maps are ``<HashMap>`` (000065); framework
  errors are ``<ApiError>`` (000308, 000311).
- Jackson YAML when the client accepts ``application/yaml`` but not JSON (``Util.getMediaType``,
  000431): every property is written, nulls included; strings are double-quoted.
- Plain strings (``"Pet not found"``) are written as raw text. Controller errors negotiate with
  Jersey: JSON when the Accept list names ``application/json`` (000420-000424), XML when it is
  exactly ``application/xml``, the operation's default for ``*/*`` (000024, 000184).
- Unrecorded and left as JSON: YAML spellings of errors and plain strings.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import Response
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonValue

JSON = "application/json"
XML = "application/xml"
YAML = "application/yaml"

PET_FIELDS = ("id", "category", "name", "photoUrls", "tags", "status")
ORDER_FIELDS = ("id", "petId", "quantity", "shipDate", "status", "complete")
ORDER_XML_FIELDS = ("complete", "id", "petId", "quantity", "shipDate", "status")
USER_FIELDS = (
    "id",
    "username",
    "firstName",
    "lastName",
    "email",
    "password",
    "phone",
    "userStatus",
)
ID_NAME_FIELDS = ("id", "name")

SERVER_ERROR = "There was an error processing your request. It has been logged (ID: {})"

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def wants_xml(ctx: RequestContext) -> bool:
    """``Util.getMediaType``: XML iff the first ``Accept`` header is exactly application/xml."""
    accept = ctx.request.headers.getlist("accept")
    return bool(accept) and accept[0] == XML


def _accepted(ctx: RequestContext) -> set[str]:
    types: set[str] = set()
    for header in ctx.request.headers.getlist("accept"):
        types |= {part.split(";", 1)[0].strip().lower() for part in header.split(",")}
    return types


def wants_yaml(ctx: RequestContext) -> bool:
    """``Util.getMediaType``: YAML when application/yaml is acceptable, application/json not."""
    if wants_xml(ctx):
        return False
    accepted = _accepted(ctx)
    return YAML in accepted and JSON not in accepted


# -- YAML ------------------------------------------------------------------------------------------


def _yaml_scalar(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _yaml_lines(value: JsonValue, indent: str) -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{indent}{key}:")
                lines += _yaml_lines(item, indent + "  ")
            elif isinstance(item, list) and item:
                lines.append(f"{indent}{key}:")
                lines += _yaml_lines(item, indent)
            else:
                lines.append(f"{indent}{key}: {_yaml_inline(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                inner = _yaml_lines(item, indent + "  ")
                lines.append(f"{indent}- {inner[0].lstrip()}")
                lines += inner[1:]
            else:
                lines.append(f"{indent}- {_yaml_inline(item)}")
    return lines


def _yaml_inline(value: JsonValue) -> str:
    if isinstance(value, list):
        return "[]"
    if isinstance(value, dict):
        return "{}"
    return _yaml_scalar(value)


def to_yaml(value: JsonValue) -> str:
    """Jackson YAML (``---`` document start, quoted strings, nulls written)."""
    if (isinstance(value, dict | list) and not value) or not isinstance(value, dict | list):
        return f"--- {_yaml_inline(value)}\n"
    return "---\n" + "\n".join(_yaml_lines(value, "")) + "\n"


def full(record: dict[str, JsonValue], fields: Sequence[str]) -> dict[str, JsonValue]:
    """Every property in declaration order, nulls included (the YAML mapper writes them)."""
    return {name: record.get(name) for name in fields}


def pet_full(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """A pet with every property (nested category/tags too)."""
    body = full(record, PET_FIELDS)
    if isinstance(body["category"], dict):
        body["category"] = full(body["category"], ID_NAME_FIELDS)
    if isinstance(body["tags"], list):
        body["tags"] = [full(t, ID_NAME_FIELDS) if isinstance(t, dict) else t for t in body["tags"]]
    return body


# -- JSON shapes -----------------------------------------------------------------------------------


def ordered(record: dict[str, JsonValue], fields: Sequence[str]) -> dict[str, JsonValue]:
    """The record's non-null properties in Jackson's declaration order (nulls are omitted)."""
    return {name: record[name] for name in fields if record.get(name) is not None}


def pet_json(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """A pet as Jackson writes it."""
    body = ordered(record, PET_FIELDS)
    category = body.get("category")
    if isinstance(category, dict):
        body["category"] = ordered(category, ID_NAME_FIELDS)
    tags = body.get("tags")
    if isinstance(tags, list):
        body["tags"] = [ordered(t, ID_NAME_FIELDS) if isinstance(t, dict) else t for t in tags]
    return body


# -- XML -------------------------------------------------------------------------------------------


def _scalar(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value))


def _element(name: str, value: JsonValue) -> str:
    if value is None or value == "":
        return f"<{name}/>"
    return f"<{name}>{_scalar(value)}</{name}>"


def _object_xml(name: str, value: JsonValue, fields: Sequence[str]) -> str:
    if not isinstance(value, dict):
        return f"<{name}/>"
    inner = "".join(_element(field, value.get(field)) for field in fields)
    return f"<{name}>{inner}</{name}>"


def _wrapped(name: str, item: str, values: JsonValue, render: object = None) -> str:
    """A wrapped list: omitted when null (000355), ``<name/>`` when empty (000023/000051)."""
    if not isinstance(values, list):
        return ""
    if not values:
        return f"<{name}/>"
    if render is None:
        inner = "".join(_element(item, v) for v in values)
    else:
        inner = "".join(_object_xml(item, v, ID_NAME_FIELDS) for v in values)
    return f"<{name}>{inner}</{name}>"


def pet_xml_fields(record: dict[str, JsonValue]) -> str:
    """The inside of a ``<Pet>`` (or list ``<item>``) element."""
    return (
        _element("id", record.get("id"))
        + _object_xml("category", record.get("category"), ID_NAME_FIELDS)
        + _element("name", record.get("name"))
        + _wrapped("photoUrls", "photoUrl", record.get("photoUrls"))
        + _wrapped("tags", "tag", record.get("tags"), render=True)
        + _element("status", record.get("status"))
    )


def pet_xml(record: dict[str, JsonValue]) -> str:
    """One pet as Jackson XML."""
    return f"<Pet>{pet_xml_fields(record)}</Pet>"


def pets_xml(records: Sequence[dict[str, JsonValue]]) -> str:
    """A list of pets as Jackson XML."""
    if not records:
        return "<ArrayList/>"
    items = "".join(f"<item>{pet_xml_fields(r)}</item>" for r in records)
    return f"<ArrayList>{items}</ArrayList>"


def order_xml(record: dict[str, JsonValue]) -> str:
    """One order as Jackson XML: alphabetical properties, ``shipDate`` as epoch milliseconds."""
    values = dict(record)
    ship = values.get("shipDate")
    if isinstance(ship, str):
        values["shipDate"] = epoch_ms(ship)
    inner = "".join(_element(field, values.get(field)) for field in ORDER_XML_FIELDS)
    return f"<Order>{inner}</Order>"


def user_xml_fields(record: dict[str, JsonValue]) -> str:
    """The inside of a ``<User>`` element."""
    return "".join(_element(field, record.get(field)) for field in USER_FIELDS)


def user_xml(record: dict[str, JsonValue]) -> str:
    """One user as Jackson XML."""
    return f"<User>{user_xml_fields(record)}</User>"


def users_xml(records: Sequence[dict[str, JsonValue]]) -> str:
    """An array of users as Jackson XML (``User[]``)."""
    if not records:
        return "<User/>"
    items = "".join(f"<item>{user_xml_fields(r)}</item>" for r in records)
    return f"<User>{items}</User>"


def map_xml(counts: dict[str, int]) -> str:
    """A ``HashMap<String, Integer>`` as Jackson XML."""
    if not counts:
        return "<HashMap/>"
    inner = "".join(_element(k, v) for k, v in counts.items())
    return f"<HashMap>{inner}</HashMap>"


# -- Java formatting -------------------------------------------------------------------------------


def java_string_hash(text: str) -> int:
    """``String.hashCode()`` (UTF-16 code units, 32-bit overflow)."""
    value = 0
    for unit in text.encode("utf-16-be").hex(" ", 2).split():
        value = (31 * value + int(unit, 16)) & 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def hashmap_order(keys: Sequence[str]) -> list[str]:
    """Iteration order of a ``java.util.HashMap`` with these keys inserted in this order."""
    capacity = 16
    while len(keys) > capacity * 3 // 4:
        capacity *= 2

    def bucket(key: str) -> int:
        h = java_string_hash(key) & 0xFFFFFFFF
        return (h ^ (h >> 16)) & (capacity - 1)

    position = {key: index for index, key in enumerate(keys)}
    return sorted(keys, key=lambda k: (bucket(k), position[k]))


def java_date_string(epoch_ns: int) -> str:
    """``java.util.Date.toString()`` in UTC, e.g. ``Sun Oct 11 12:38:00 UTC 2026``."""
    moment = datetime.fromtimestamp(epoch_ns / 1e9, tz=UTC)
    return (
        f"{_DAYS[moment.weekday()]} {_MONTHS[moment.month - 1]} {moment.day:02d} "
        f"{moment:%H:%M:%S} UTC {moment.year}"
    )


_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}(:?\d{2})?)?)?$"
)


def parse_date(value: str) -> datetime | None:
    """Jackson's ``StdDateFormat`` for ``java.util.Date``.

    ISO-8601 with a ``T`` or a plain date; a space separator is refused (000435) and an all-digit
    string is epoch milliseconds (000434).
    """
    text = value.strip()
    if re.fullmatch(r"-?\d+", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
    if not _ISO_DATE.match(text):
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_ship_date(moment: datetime) -> str:
    """How the server writes a ``Date``: ``2026-10-12T10:30:45.000+00:00`` (000214)."""
    millis = moment.microsecond // 1000
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{millis:03d}+00:00"


def epoch_ms(ship_date: str) -> int | None:
    """Epoch milliseconds of a stored ``shipDate`` (XML spelling, 000309)."""
    moment = parse_date(ship_date)
    if moment is None:
        return None
    return int(moment.timestamp() * 1000)


# -- responses -------------------------------------------------------------------------------------


def entity(
    ctx: RequestContext,
    body: JsonValue,
    xml: str,
    *,
    status: int = 200,
    yaml: JsonValue = None,
) -> Response:
    """A negotiated success entity: JSON, or its XML / YAML spelling (``yaml`` keeps nulls)."""
    if wants_xml(ctx):
        return ctx.respond_bytes(xml.encode(), status=status, content_type=XML)
    if wants_yaml(ctx):
        document = to_yaml(body if yaml is None else yaml)
        return ctx.respond_bytes(document.encode(), status=status, content_type=YAML)
    return ctx.respond(body, status=status, content_type=JSON)


def text(ctx: RequestContext, message: str, *, status: int = 200) -> Response:
    """A plain string entity, labeled with the negotiated type (``"Pet deleted"``)."""
    media = XML if wants_xml(ctx) else JSON
    return ctx.respond_bytes(message.encode(), status=status, content_type=media)


def plain_error(
    ctx: RequestContext, status: int, message: str, content_type: str = XML
) -> Response:
    """A controller error: raw text; Jersey picks JSON when asked for it, else ``content_type``.

    ``content_type`` is what the operation answers ``*/*`` with (XML for most, JSON for
    uploadImage: 000024 vs 000184); exact ``application/xml`` gets XML (000307).
    """
    if wants_xml(ctx):
        media = XML
    elif JSON in _accepted(ctx):
        media = JSON
    else:
        media = content_type
    return ctx.respond_bytes(message.encode(), status=status, content_type=media)


def api_error(ctx: RequestContext, status: int, message: str) -> Response:
    """A framework ``ApiError`` (input errors, exceptions) as JSON or XML."""
    if wants_xml(ctx):
        xml = f"<ApiError><code>{status}</code><message>{escape(message)}</message></ApiError>"
        return ctx.respond_bytes(xml.encode(), status=status, content_type=XML)
    return ctx.respond({"code": status, "message": message}, status=status, content_type=JSON)


def server_error(ctx: RequestContext) -> Response:
    """An unhandled exception in the server (500 with a fresh 16-hex-digit log id)."""
    log_id = f"{ctx.rng.getrandbits(64):016x}"
    return api_error(ctx, 500, SERVER_ERROR.format(log_id))


def convert_error(ctx: RequestContext, value: str) -> Response:
    """A path value that is not a Java ``long``."""
    return api_error(
        ctx, 400, f"Input error: couldn't convert `{value}` to type `class java.lang.Long`"
    )


def body_error(ctx: RequestContext, model: str) -> Response:
    """A request body Jackson cannot read as ``model``."""
    return api_error(
        ctx, 400, f"Input error: unable to convert input to io.swagger.petstore.model.{model}"
    )


def unsupported_media(ctx: RequestContext) -> Response:
    """A body content type the operation does not consume."""
    return api_error(ctx, 415, "HTTP 415 Unsupported Media Type")


def empty_ok(ctx: RequestContext) -> Response:
    """A null entity: 200 with no body and no content type (000233)."""
    return ctx.empty(200)
