"""Filtering, sorting and pagination of collection listings.

Pagination reproduces the real API's mechanics — offsets, page numbers, cursors spelled exactly like
the real ones, ``Link`` headers — computed over the consistent world, so totals and page boundaries
stay coherent with every other endpoint.

Cursors are rendered from ``PaginationModel.cursor_template`` with ``{{offset}}`` (where the page
starts), ``{{key}}`` (the key of the record *before* the page; ``null`` for the first page) and
``{{page}}`` (the page number), then encoded with ``cursor_encoding``. Reading a cursor reverses
that; anything that doesn't decode back to a position is a :class:`PageError`.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from dataclasses import dataclass, field
from operator import ge, gt, le, lt
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

from fauxcall import jsonfmt, jsonpath
from fauxcall.runtime.templating import as_text, from_mapping, render

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fauxcall.model.endpoints import EndpointModel, PaginationModel, SortModel
    from fauxcall.model.world import WorldRecord
    from fauxcall.types import JsonValue

_NUMERIC: Final = re.compile(r"\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*")
_DIGITS: Final = re.compile(r"[0-9]+")
_RANGE: Final[dict[str, Callable[[float | str, float | str], bool]]] = {
    "gte": ge,
    "lte": le,
    "gt": gt,
    "lt": lt,
}


class PageError(Exception):
    """A pagination parameter is invalid (``param`` names it)."""

    def __init__(self, param: str, value: JsonValue) -> None:
        super().__init__(f"invalid {param}: {value!r}")
        self.param = param
        self.value = value


# -- filtering ---------------------------------------------------------------------------------


def _as_number(value: JsonValue) -> float | None:
    """A number, or a string spelling a plain decimal number; ``None`` for anything else."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str) and _NUMERIC.fullmatch(value):
            return float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return None


def _compare(field_value: JsonValue, wanted: JsonValue, operator: str) -> bool:
    """Whether one field value satisfies ``operator`` against one wanted value.

    ``null`` fields never match. Range operators compare numerically when both sides are numbers,
    else as text (which orders ISO-8601 instants correctly).
    """
    if field_value is None:
        return False
    if operator in _RANGE:
        a, b = _as_number(field_value), _as_number(wanted)
        if a is not None and b is not None:
            return _RANGE[operator](a, b)
        return _RANGE[operator](as_text(field_value), as_text(wanted))
    text, target = as_text(field_value), as_text(wanted)
    match operator:
        case "contains":
            return target in text
        case "icontains" | "search":
            return target.lower() in text.lower()
        case "prefix":
            return text.startswith(target)
        case _:
            return _equals(field_value, wanted)


def _equals(field_value: JsonValue, wanted: JsonValue) -> bool:
    """``eq`` semantics.

    Numbers compare by value (unless the field is a string), booleans case-insensitively
    (``?active=true``), everything else by exact JSON text.
    """
    a, b = _as_number(field_value), _as_number(wanted)
    if a is not None and b is not None and not isinstance(field_value, str):
        return a == b
    if isinstance(field_value, bool):
        return as_text(field_value) == as_text(wanted).lower()
    return as_text(field_value) == as_text(wanted)


def _wanted_values(value: JsonValue, operator: str, separator: str | None) -> list[JsonValue]:
    if isinstance(value, list):
        return list(value)
    if operator == "in" and separator and isinstance(value, str):
        return [part for part in value.split(separator) if part]
    return [value]


def _search_text(values: Sequence[JsonValue]) -> str:
    """Every scalar under ``values`` (values only, not object keys), lower-cased and joined."""
    leaves = (
        as_text(leaf)
        for value in values
        for _, leaf in jsonpath.leaf_paths(value)
        if leaf is not None
    )
    return " ".join(leaves).lower()


def matches(
    record: JsonValue, path: str, value: JsonValue, operator: str, separator: str | None
) -> bool:
    """Whether ``record`` satisfies one filter (any field value matching any wanted value).

    ``search`` with a text value requires every whitespace-separated token to appear somewhere in
    the field's scalar values (nested objects and lists included).
    """
    field_values = jsonpath.get_all(record, path)
    if operator == "search" and isinstance(value, str):
        haystack = _search_text(field_values)
        return all(token in haystack for token in value.lower().split())
    flattened: list[JsonValue] = []
    for item in field_values:
        flattened.extend(item if isinstance(item, list) else [item])
    wanted = _wanted_values(value, operator, separator)
    effective = "eq" if operator == "in" else operator
    return any(_compare(f, w, effective) for f in flattened for w in wanted)


def apply_filters(
    records: Sequence[WorldRecord], endpoint: EndpointModel, params: Mapping[str, JsonValue]
) -> list[WorldRecord]:
    """Keep records matching every parameter that maps to a record field (empty values: no-op)."""
    selected = list(records)
    for param in endpoint.params:
        if param.maps_to is None or param.name not in params:
            continue
        value = params[param.name]
        if value is None or value in ("", []):
            continue
        selected = [
            r
            for r in selected
            if matches(r.record, param.maps_to, value, param.operator, param.separator)
        ]
    return selected


def _sort_key(value: JsonValue) -> tuple[int, float | str]:
    """Numbers (by value) before everything else (by JSON text); never compares across types."""
    number = None if isinstance(value, str) else _as_number(value)
    return (0, number) if number is not None else (1, as_text(value))


def apply_sort(
    records: Sequence[WorldRecord], sort: SortModel | None, params: Mapping[str, JsonValue]
) -> list[WorldRecord]:
    """Sort as requested (or by the default), keeping recorded order for ties.

    Records without the field come last in either direction. A sort field that isn't a valid
    field path leaves the order unchanged.
    """
    if sort is None:
        return list(records)
    requested = params.get(sort.param) if sort.param else None
    if isinstance(requested, list):
        requested = requested[0] if requested else None
    field_name = as_text(requested) if requested else sort.default
    if not field_name:
        return list(records)
    descending = False
    prefix = sort.descending_prefix
    if prefix and field_name.startswith(prefix):
        descending, field_name = True, field_name[len(prefix) :]
    order = params.get(sort.order_param) if sort.order_param else None
    if isinstance(order, str) and order.lower() in {"desc", "descending"}:
        descending = True
    try:
        if not jsonpath.parse(field_name):
            return list(records)
    except ValueError:
        return list(records)
    keyed = [(jsonpath.get_one(record.record, field_name), record) for record in records]
    present = [(value, record) for value, record in keyed if value is not None]
    missing = [record for value, record in keyed if value is None]
    present.sort(key=lambda pair: _sort_key(pair[0]), reverse=descending)
    return [record for _, record in present] + missing


# -- pagination --------------------------------------------------------------------------------


@dataclass(slots=True)
class Page:
    """One page of a listing.

    ``next_url``/``prev_url`` are this listing's URL with the page/offset/cursor parameter moved
    (other query parameters kept); ``links`` holds the ``Link`` header relations (``link_header``
    style only).
    """

    items: list[WorldRecord]
    total: int
    offset: int
    limit: int | None
    next_cursor: str | None = None
    prev_cursor: str | None = None
    has_more: bool = False
    page: int | None = None
    links: dict[str, str] = field(default_factory=dict)
    next_url: str | None = None
    prev_url: str | None = None


def _encode(text: str, encoding: str) -> str:
    raw = text.encode()
    match encoding:
        case "base64":
            return base64.b64encode(raw).decode()
        case "base64url":
            return base64.urlsafe_b64encode(raw).decode()
        case "base64url_nopad":
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        case _:
            return text


def _decode(token: str, encoding: str) -> str:
    if encoding == "plain":
        return token
    cleaned = token.strip()
    if encoding == "base64":
        cleaned = cleaned.replace(" ", "+")  # an unescaped '+' turned into ' ' by URL decoding
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        if encoding == "base64":
            return base64.b64decode(padded, validate=True).decode()
        return base64.urlsafe_b64decode(padded).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("undecodable cursor") from exc


def _default_template(pagination: PaginationModel) -> str:
    if pagination.cursor_format == "key":
        return "{{key}}"
    return "{{page}}" if pagination.style in {"page", "link_header"} else "{{offset}}"


def make_cursor(
    pagination: PaginationModel, *, offset: int, key: JsonValue, page: int | None = None
) -> str:
    """Spell a cursor the way the real API does.

    ``offset`` is where the page starts, ``key`` the key of the record before it (``None`` for the
    first page) and ``page`` its page number (page-numbered styles).
    """
    template = pagination.cursor_template
    if template is None:
        template = _default_template(pagination)
    rendered = render(template, from_mapping({"offset": offset, "key": key, "page": page}))
    text = rendered if isinstance(rendered, str) else jsonfmt.dumps(rendered)
    return _encode(text, pagination.cursor_encoding)


def _slot(template: str, name: str) -> tuple[str, str] | None:
    """``(prefix, suffix)`` around the ``{{name}}`` placeholder in ``template`` (if present)."""
    found = re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", template)
    return None if found is None else (template[: found.start()], template[found.end() :])


def _between(text: str, prefix: str, suffix: str) -> str:
    if len(text) < len(prefix) + len(suffix) or not (
        text.startswith(prefix) and text.endswith(suffix)
    ):
        raise ValueError("cursor does not match the template")
    return text[len(prefix) : len(text) - len(suffix)]


def read_cursor(pagination: PaginationModel, token: str) -> tuple[str, JsonValue]:
    """Decode a cursor into ``("offset", n)`` or ``("key", value)``; ``ValueError`` if invalid.

    A key of ``None`` means "from the start" (the previous-page cursor of the second page).
    """
    text = _decode(token, pagination.cursor_encoding)
    template = pagination.cursor_template
    kind = "key" if pagination.cursor_format == "key" else "offset"
    if isinstance(template, dict | list):
        document = jsonfmt.loads(text)
        for path, leaf in jsonpath.leaf_paths(template, generalize_lists=False):
            slot = _slot(leaf, kind) if isinstance(leaf, str) else None
            if slot is None:
                continue
            found = jsonpath.get_all(document, path)
            if not found:
                raise ValueError("cursor is missing its position")
            if slot == ("", ""):
                return kind, found[0]
            return kind, _between(as_text(found[0]), *slot)
        raise ValueError("cursor template has no position placeholder")
    slot = _slot(template, kind) if isinstance(template, str) else None
    if slot is not None:
        text = _between(text, *slot)
    return kind, text


def _int_param(
    params: Mapping[str, JsonValue], name: str | None, default: int | None
) -> int | None:
    if name is None or params.get(name) is None:
        return default
    value = params[name]
    if isinstance(value, bool):
        raise PageError(name, value)
    try:
        return int(str(value))
    except ValueError as exc:
        raise PageError(name, value) from exc


def _limit(pagination: PaginationModel, params: Mapping[str, JsonValue]) -> int | None:
    limit = _int_param(params, pagination.limit_param, pagination.default_limit)
    if limit is None:
        return None
    if limit < 1:
        raise PageError(pagination.limit_param or "limit", limit)
    if pagination.max_limit is not None and limit > pagination.max_limit:
        if not pagination.clamp_limit:
            raise PageError(pagination.limit_param or "limit", limit)
        limit = pagination.max_limit
    return limit


def _start_from_cursor(
    pagination: PaginationModel,
    records: Sequence[WorldRecord],
    token: str,
    ordering: Sequence[WorldRecord] | None = None,
) -> int:
    name = pagination.cursor_param or "cursor"
    try:
        kind, position = read_cursor(pagination, token)
    except ValueError as exc:
        raise PageError(name, token) from exc
    if kind == "offset":
        text = position if isinstance(position, str) else as_text(position)
        if isinstance(position, bool) or not _DIGITS.fullmatch(text):
            raise PageError(name, token)
        return int(text)
    keys = [as_text(record.key) for record in records]
    wanted = as_text(position)
    if wanted in keys:
        return keys.index(wanted) + 1
    if position is None or wanted == "null":  # "from the start"
        return 0
    full = [as_text(record.key) for record in ordering or ()]
    if wanted in full:  # an existing record the current filters exclude: continue after its place
        place = {key: index for index, key in enumerate(full)}
        cutoff = place[wanted]
        return sum(1 for key in keys if place.get(key, -1) <= cutoff)
    raise PageError(name, token)


def _url(base_url: str, query: Sequence[tuple[str, str]], name: str, value: str) -> str:
    kept = [(k, v) for k, v in query if k != name]
    return f"{base_url}?{urlencode([*kept, (name, value)])}"


def paginate(
    records: Sequence[WorldRecord],
    pagination: PaginationModel | None,
    params: Mapping[str, JsonValue],
    *,
    base_url: str,
    query: Sequence[tuple[str, str]],
    ordering: Sequence[WorldRecord] | None = None,
) -> Page:
    """Slice ``records`` per the endpoint's pagination style (``PageError`` for bad parameters).

    ``ordering`` is the whole collection in the same order before filtering: key cursors naming a
    record the filters exclude then continue from that record's place, as most real APIs do.
    """
    total = len(records)
    if pagination is None or pagination.style == "none":
        return Page(items=list(records), total=total, offset=0, limit=None)
    limit = _limit(pagination, params)
    page_number: int | None = None
    if pagination.style in {"page", "link_header"}:
        page_number = _int_param(params, pagination.page_param, pagination.first_page)
        if page_number is None or page_number < pagination.first_page:
            raise PageError(pagination.page_param or "page", page_number)
        offset = (page_number - pagination.first_page) * (limit or total or 1)
    elif pagination.style == "cursor":
        token = params.get(pagination.cursor_param) if pagination.cursor_param else None
        offset = _start_from_cursor(pagination, records, as_text(token), ordering) if token else 0
    else:
        offset = _int_param(params, pagination.offset_param, 0) or 0
        if offset < 0:
            raise PageError(pagination.offset_param or "offset", offset)
    end = total if limit is None else offset + limit
    items = list(records[offset:end])
    has_more = end < total
    page = Page(
        items=items, total=total, offset=offset, limit=limit, has_more=has_more, page=page_number
    )
    next_page = None if page_number is None else page_number + 1
    if has_more and items:
        page.next_cursor = make_cursor(pagination, offset=end, key=items[-1].key, page=next_page)
        page.next_url = _position_url(pagination, page.next_cursor, end, next_page, base_url, query)
    if offset > 0 and limit:
        # Past the end, "previous" is the last page that has items.
        prev_page: int | None = None
        if page_number is None:
            previous = max(0, min(offset, total) - limit)
        else:
            last = pagination.first_page + max(0, math.ceil(total / limit) - 1)
            prev_page = min(page_number - 1, last)
            previous = (prev_page - pagination.first_page) * limit
        anchor = records[previous - 1].key if previous > 0 else None
        page.prev_cursor = make_cursor(pagination, offset=previous, key=anchor, page=prev_page)
        page.prev_url = _position_url(
            pagination, page.prev_cursor, previous, prev_page, base_url, query
        )
    if pagination.style == "link_header" and page_number is not None and limit:
        page.links = _links(pagination, page_number, limit, total, base_url, query)
    return page


def _position_url(
    pagination: PaginationModel,
    cursor: str,
    offset: int,
    page_number: int | None,
    base_url: str,
    query: Sequence[tuple[str, str]],
) -> str | None:
    """The listing URL positioned at another page (``None`` if the style has no parameter)."""
    if page_number is not None:
        return _url(base_url, query, pagination.page_param or "page", str(page_number))
    if pagination.style == "cursor":
        name = pagination.cursor_param
        return None if name is None else _url(base_url, query, name, cursor)
    return _url(base_url, query, pagination.offset_param or "offset", str(offset))


def _links(
    pagination: PaginationModel,
    page_number: int,
    limit: int,
    total: int,
    base_url: str,
    query: Sequence[tuple[str, str]],
) -> dict[str, str]:
    name = pagination.page_param or "page"
    last = pagination.first_page + max(0, math.ceil(total / limit) - 1)
    links: dict[str, str] = {}
    if page_number < last:
        links["next"] = _url(base_url, query, name, str(page_number + 1))
        links["last"] = _url(base_url, query, name, str(last))
    if page_number > pagination.first_page:
        links["first"] = _url(base_url, query, name, str(pagination.first_page))
        links["prev"] = _url(base_url, query, name, str(page_number - 1))
    return links


def link_header(links: Mapping[str, str]) -> str:
    """RFC 8288 ``Link`` header value."""
    return ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items())


def page_value(
    page: Page, pagination: PaginationModel, field_name: str, next_url: str | None
) -> JsonValue:
    """Value of one computed page field.

    With ``next_as_url``, ``next``/``prev`` are full URLs (``next_url`` when the caller passes one,
    else the page's own); otherwise they are the bare cursors.
    """
    match field_name:
        case "total":
            return page.total
        case "count":
            return len(page.items)
        case "page":
            return page.page
        case "per_page" | "limit":
            return page.limit
        case "offset":
            return page.offset
        case "total_pages":
            return math.ceil(page.total / page.limit) if page.limit else 1
        case "has_more":
            return page.has_more
        case "next":
            if pagination.next_as_url:
                return next_url if next_url is not None else page.next_url
            return page.next_cursor
        case _:
            return page.prev_url if pagination.next_as_url else page.prev_cursor
