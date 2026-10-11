from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall.jsonfmt import JsonFloat
from fauxcall.model.endpoints import EndpointModel, PaginationModel, SortModel
from fauxcall.model.world import WorldRecord
from fauxcall.runtime.listing import (
    Page,
    PageError,
    apply_filters,
    apply_sort,
    link_header,
    make_cursor,
    matches,
    page_value,
    paginate,
    read_cursor,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

BASE = "https://fake.test/v1/items"
RECORDS = [WorldRecord(key=f"k{n}", record={"id": f"k{n}", "n": n}) for n in range(1, 8)]


def keys(records: list[WorldRecord]) -> list[JsonValue]:
    return [record.key for record in records]


def pages(**fields: Any) -> PaginationModel:
    return PaginationModel.model_validate(fields)


def run(
    pagination: PaginationModel | None,
    params: dict[str, JsonValue] | None = None,
    query: list[tuple[str, str]] | None = None,
    records: list[WorldRecord] = RECORDS,
) -> Page:
    return paginate(records, pagination, params or {}, base_url=BASE, query=query or [])


# -- filtering ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "wanted", "expected"),
    [
        ({"v": 5}, "5", True),
        ({"v": 5}, "5.0", True),  # numeric fields compare by value
        ({"v": 5}, 5, True),
        ({"v": 5}, "6", False),
        ({"v": JsonFloat("105.50")}, "105.5", True),
        ({"v": "5"}, "5.0", False),  # string fields compare by text
        ({"v": "5"}, 5, True),
        ({"v": "XNAS"}, "XNAS", True),
        ({"v": "XNAS"}, "xnas", False),
        ({"v": True}, "true", True),
        ({"v": True}, "TRUE", True),
        ({"v": True}, True, True),
        ({"v": False}, "true", False),
        ({"v": None}, "null", False),
        ({"other": 1}, "1", False),
        ({"v": ["a", "b"]}, "b", True),  # list fields match any element
        ({"v": 5}, "nan", False),
        ({"v": "nan"}, "NaN", False),
        ({"v": 10**400}, "1", False),
    ],
)
def test_eq(record: JsonValue, wanted: JsonValue, expected: bool) -> None:
    assert matches(record, "v", wanted, "eq", None) is expected


@pytest.mark.parametrize(
    ("wanted", "operator", "separator", "expected"),
    [
        ("XNYS,XNAS", "in", ",", True),
        ("XNYS|LSE", "in", "|", False),
        (["XNYS", "XNAS"], "in", None, True),
        ("XNYS,XNAS", "in", None, False),  # without a separator the value is taken whole
        ("NA", "contains", None, True),
        ("na", "contains", None, False),
        ("na", "icontains", None, True),
        ("XN", "prefix", None, True),
        ("NAS", "prefix", None, False),
    ],
)
def test_text_operators(
    wanted: JsonValue, operator: str, separator: str | None, expected: bool
) -> None:
    assert matches({"v": "XNAS"}, "v", wanted, operator, separator) is expected


@pytest.mark.parametrize(
    ("value", "wanted", "operator", "expected"),
    [
        (10, "10", "gte", True),
        (10, "10", "gt", False),
        (10, 10.5, "lte", True),
        (10, "9", "lt", False),
        (9, "10", "lt", True),  # numerically, not "9" > "10"
        ("2026-10-10T10:00:00Z", "2026-10-10", "gte", True),
        ("2026-10-10T10:00:00Z", "2026-10-09T23:59:59Z", "lt", False),
        ("2026-10-10T10:00:00Z", "2026-10-11", "lt", True),
        (None, "2000-01-01", "gte", False),  # null never matches a range
        ("abc", 5, "gt", True),  # mixed: compared as text
    ],
)
def test_ranges(value: JsonValue, wanted: JsonValue, operator: str, expected: bool) -> None:
    assert matches({"v": value}, "v", wanted, operator, None) is expected


TICKER: JsonValue = {
    "name": "Apple Inc.",
    "tags": [{"label": "Tech"}, {"label": "Hardware"}],
    "meta": {"exchange": "XNAS", "delisted": None},
}


@pytest.mark.parametrize(
    ("path", "wanted", "expected"),
    [
        ("$", "apple xnas", True),
        ("$", "APPLE   tech", True),
        ("$", "apple nyse", False),
        ("$", "name", False),  # object keys are not searched
        ("$", "null", False),
        ("$", "", True),
        ("tags[*].label", "hard", True),
        ("tags[*].label", ["TECH"], True),
        ("tags", "tech", True),
    ],
)
def test_search(path: str, wanted: JsonValue, expected: bool) -> None:
    assert matches(TICKER, path, wanted, "search", None) is expected


def test_apply_filters_combines_mapped_parameters() -> None:
    endpoint = EndpointModel.model_validate(
        {
            "id": "GET /v1/tickers",
            "method": "GET",
            "path": "/v1/tickers",
            "role": "list",
            "collection": "tickers",
            "params": [
                {"name": "exchange", "location": "query", "maps_to": "exchange"},
                {"name": "q", "location": "query", "maps_to": "$", "operator": "search"},
                {"name": "sector", "location": "query", "maps_to": "sector", "operator": "in"},
                {"name": "limit", "location": "query"},
            ],
        }
    )
    records = [
        WorldRecord(key=1, record={"exchange": "XNAS", "sector": "Tech", "name": "Apple"}),
        WorldRecord(key=2, record={"exchange": "XNYS", "sector": "Tech", "name": "IBM"}),
        WorldRecord(key=3, record={"exchange": "XNAS", "sector": "Auto", "name": "Tesla"}),
    ]
    assert keys(apply_filters(records, endpoint, {"exchange": "XNAS", "limit": 1})) == [1, 3]
    assert keys(apply_filters(records, endpoint, {"exchange": "XNAS", "q": "tes"})) == [3]
    assert keys(apply_filters(records, endpoint, {"sector": ["Auto", "Tech"]})) == [1, 2, 3]
    empties: tuple[JsonValue, ...] = (None, "", [])
    for empty in empties:
        assert keys(apply_filters(records, endpoint, {"exchange": empty})) == [1, 2, 3]
    assert keys(apply_filters(records, endpoint, {})) == [1, 2, 3]


# -- sorting -----------------------------------------------------------------------------------

PRICES = [
    WorldRecord(key=1, record={"price": 10, "name": "b"}),
    WorldRecord(key=2, record={"price": None, "name": "a"}),
    WorldRecord(key=3, record={"price": 5, "name": "c"}),
    WorldRecord(key=4, record={"price": 5, "name": "a"}),
    WorldRecord(key=5, record={"name": "d"}),
]


@pytest.mark.parametrize(
    ("sort", "params", "expected"),
    [
        (None, {"sort": "price"}, [1, 2, 3, 4, 5]),
        (SortModel(default="price"), {}, [3, 4, 1, 2, 5]),
        (SortModel(param="sort", default="name"), {"sort": "price"}, [3, 4, 1, 2, 5]),
        (SortModel(param="sort"), {"sort": "-price"}, [1, 3, 4, 2, 5]),
        (SortModel(param="sort"), {"sort": ["-price", "name"]}, [1, 3, 4, 2, 5]),
        (SortModel(param="sort", default="name"), {"sort": []}, [2, 4, 1, 3, 5]),
        (
            SortModel(param="sort", order_param="order"),
            {"sort": "price", "order": "DESC"},
            [1, 3, 4, 2, 5],
        ),
        (
            SortModel(param="sort", order_param="order"),
            {"sort": "name", "order": "descending"},
            [5, 3, 1, 2, 4],
        ),
        (
            SortModel(param="sort", order_param="order"),
            {"sort": "name", "order": "asc"},
            [2, 4, 1, 3, 5],
        ),
        (SortModel(param="sort"), {}, [1, 2, 3, 4, 5]),
        (SortModel(param="sort"), {"sort": "name,-price"}, [1, 2, 3, 4, 5]),
        (SortModel(param="sort"), {"sort": "-"}, [1, 2, 3, 4, 5]),
        (SortModel(param="sort", descending_prefix=None), {"sort": "-price"}, [1, 2, 3, 4, 5]),
    ],
)
def test_sorting(sort: SortModel | None, params: dict[str, JsonValue], expected: list[int]) -> None:
    assert keys(apply_sort(PRICES, sort, params)) == expected


def test_sorting_mixed_types_puts_numbers_first() -> None:
    mixed = [
        WorldRecord(key=1, record={"v": 3}),
        WorldRecord(key=2, record={"v": "x"}),
        WorldRecord(key=3, record={"v": 1}),
        WorldRecord(key=4, record={"v": True}),
        WorldRecord(key=5, record={"v": "10"}),
        WorldRecord(key=6, record={"v": "9"}),
    ]
    assert keys(apply_sort(mixed, SortModel(default="v"), {})) == [3, 1, 5, 6, 4, 2]


# -- pagination --------------------------------------------------------------------------------


def test_no_pagination_returns_everything() -> None:
    for pagination in (None, pages(style="none", default_limit=2)):
        page = run(pagination)
        assert (keys(page.items), page.total, page.offset, page.limit) == (
            keys(RECORDS),
            7,
            0,
            None,
        )
        assert (page.next_cursor, page.prev_cursor, page.has_more) == (None, None, False)


OFFSET = pages(
    style="offset", offset_param="offset", limit_param="limit", default_limit=3, max_limit=5
)


def test_offset_pagination() -> None:
    first = run(OFFSET, query=[("q", "x"), ("offset", "0")])
    assert keys(first.items) == ["k1", "k2", "k3"]
    assert (first.offset, first.limit, first.has_more) == (0, 3, True)
    assert first.next_cursor == "3"
    assert first.next_url == f"{BASE}?q=x&offset=3"
    assert (first.prev_cursor, first.prev_url) == (None, None)
    second = run(OFFSET, {"offset": 3})
    assert keys(second.items) == ["k4", "k5", "k6"]
    assert (second.prev_cursor, second.prev_url) == ("0", f"{BASE}?offset=0")
    last = run(OFFSET, {"offset": "6"})
    assert keys(last.items) == ["k7"]
    assert (last.has_more, last.next_cursor, last.next_url) == (False, None, None)
    assert last.prev_cursor == "3"
    beyond = run(OFFSET, {"offset": 100})
    assert (beyond.items, beyond.has_more, beyond.prev_cursor) == ([], False, "4")


def test_limits() -> None:
    assert run(OFFSET, {"limit": 10}).limit == 5  # clamped to the maximum
    assert run(OFFSET, {"limit": None}).limit == 3
    assert run(OFFSET, {"limit": "2"}).limit == 2
    strict = pages(style="offset", limit_param="limit", max_limit=5, clamp_limit=False)
    with pytest.raises(PageError) as too_big:
        run(strict, {"limit": 10})
    assert (too_big.value.param, too_big.value.value) == ("limit", 10)
    for bad in (0, -1, "abc", True, "2.5"):
        with pytest.raises(PageError):
            run(OFFSET, {"limit": bad})
    unlimited = run(pages(style="offset", offset_param="offset"), {"offset": 5})
    assert (keys(unlimited.items), unlimited.limit) == (["k6", "k7"], None)
    assert unlimited.prev_cursor is None


def test_invalid_offsets() -> None:
    with pytest.raises(PageError, match="offset"):
        run(OFFSET, {"offset": -1})
    with pytest.raises(PageError, match="offset"):
        run(OFFSET, {"offset": "abc"})


PAGED = pages(style="page", page_param="page", limit_param="per_page", default_limit=3)


def test_page_numbers() -> None:
    first = run(PAGED)
    assert (keys(first.items), first.page, first.next_cursor) == (["k1", "k2", "k3"], 1, "2")
    assert first.next_url == f"{BASE}?page=2"
    second = run(PAGED, {"page": 2}, query=[("page", "2"), ("per_page", "3")])
    assert keys(second.items) == ["k4", "k5", "k6"]
    assert (second.prev_cursor, second.next_cursor) == ("1", "3")
    assert second.prev_url == f"{BASE}?per_page=3&page=1"
    third = run(PAGED, {"page": 3})
    assert (keys(third.items), third.has_more, third.prev_cursor) == (["k7"], False, "2")
    beyond = run(PAGED, {"page": 9})
    assert (beyond.items, beyond.prev_cursor) == ([], "3")  # back to the last real page
    with pytest.raises(PageError) as caught:
        run(PAGED, {"page": 0})
    assert caught.value.param == "page"


def test_zero_based_pages_and_pages_without_a_limit() -> None:
    zero = pages(style="page", page_param="p", first_page=0, default_limit=5)
    assert (keys(run(zero, {"p": 0}).items), run(zero, {"p": 0}).next_cursor) == (
        ["k1", "k2", "k3", "k4", "k5"],
        "1",
    )
    assert run(zero, {"p": 1}).prev_cursor == "0"
    unlimited = pages(style="page", page_param="page")
    assert len(run(unlimited).items) == 7
    assert run(unlimited, {"page": 2}).items == []
    empty = run(PAGED, {"page": 2}, records=[])
    assert (empty.items, empty.total, empty.prev_cursor) == ([], 0, "1")


def test_link_header_pagination() -> None:
    linked = pages(style="link_header", page_param="page", limit_param="per_page", default_limit=2)
    query = [("per_page", "2"), ("page", "1"), ("state", "open")]
    five = RECORDS[:5]

    def url(number: int) -> str:
        return f"{BASE}?per_page=2&state=open&page={number}"

    first = run(linked, {"page": 1}, query, five)
    assert first.links == {"next": url(2), "last": url(3)}
    assert link_header(first.links) == f'<{url(2)}>; rel="next", <{url(3)}>; rel="last"'
    middle = run(linked, {"page": 2}, query, five)
    assert middle.links == {"next": url(3), "last": url(3), "first": url(1), "prev": url(1)}
    last = run(linked, {"page": 3}, query, five)
    assert last.links == {"first": url(1), "prev": url(2)}
    assert run(linked, {"page": 1}, query, []).links == {}
    assert link_header({}) == ""


@pytest.mark.parametrize(
    ("encoding", "spelled"),
    [("plain", "2"), ("base64", "Mg=="), ("base64url", "Mg=="), ("base64url_nopad", "Mg")],
)
def test_offset_cursors_round_trip_in_every_encoding(encoding: str, spelled: str) -> None:
    cursor = pages(style="cursor", cursor_param="cursor", default_limit=2, cursor_encoding=encoding)
    first = run(cursor)
    assert first.next_cursor == spelled
    assert first.next_url == f"{BASE}?cursor={spelled.replace('=', '%3D')}"
    second = run(cursor, {"cursor": spelled})
    assert keys(second.items) == ["k3", "k4"]
    assert second.prev_cursor is not None
    assert keys(run(cursor, {"cursor": second.prev_cursor}).items) == ["k1", "k2"]


KEYED = pages(
    style="cursor",
    cursor_param="cursor",
    default_limit=2,
    cursor_format="key",
    cursor_template={"after": "{{key}}"},
    cursor_encoding="base64url_nopad",
)


def decode(token: str) -> JsonValue:
    loaded: JsonValue = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    return loaded


def test_key_cursors_spelled_like_the_real_api() -> None:
    first = run(KEYED)
    assert first.next_cursor is not None
    assert decode(first.next_cursor) == {"after": "k2"}
    assert "=" not in first.next_cursor
    second = run(KEYED, {"cursor": first.next_cursor})
    assert keys(second.items) == ["k3", "k4"]
    assert second.prev_cursor is not None
    assert decode(second.prev_cursor) == {"after": None}  # "from the start"
    assert keys(run(KEYED, {"cursor": second.prev_cursor}).items) == ["k1", "k2"]
    third = run(KEYED, {"cursor": second.next_cursor})
    assert third.prev_cursor is not None
    assert decode(third.prev_cursor) == {"after": "k2"}
    padded = base64.urlsafe_b64encode(b'{ "after" : "k2" }').decode()
    assert keys(run(KEYED, {"cursor": padded}).items) == ["k3", "k4"]


def test_key_cursors_with_integer_keys() -> None:
    numbered = [WorldRecord(key=n, record={"id": n}) for n in range(1, 6)]
    first = run(KEYED, records=numbered)
    assert first.next_cursor is not None
    assert decode(first.next_cursor) == {"after": 2}
    assert keys(run(KEYED, {"cursor": first.next_cursor}, records=numbered).items) == [3, 4]


@pytest.mark.parametrize(
    ("template", "fmt", "spelled"),
    [
        ("page-{{offset}}-x", "offset", "page-2-x"),
        ("{{ key }}", "key", "k2"),
        ("after:{{key}}", "key", "after:k2"),
        (None, "key", "k2"),
        (None, "opaque", "2"),
    ],
)
def test_string_templates_round_trip(template: str | None, fmt: str, spelled: str) -> None:
    cursor = pages(
        style="cursor",
        cursor_param="c",
        default_limit=2,
        cursor_format=fmt,
        cursor_template=template,
    )
    first = run(cursor)
    assert first.next_cursor == spelled
    assert keys(run(cursor, {"c": spelled}).items) == ["k3", "k4"]


def test_object_templates_round_trip() -> None:
    embedded = pages(
        style="cursor", cursor_param="c", default_limit=2, cursor_template={"c": "o:{{offset}}"}
    )
    first = run(embedded)
    assert first.next_cursor == '{"c":"o:2"}'
    assert keys(run(embedded, {"c": '{"c":"o:2"}'}).items) == ["k3", "k4"]
    typed = pages(
        style="cursor",
        cursor_param="c",
        default_limit=2,
        cursor_template=["v1", {"offset": "{{offset}}"}],
    )
    assert run(typed).next_cursor == '["v1",{"offset":2}]'
    assert keys(run(typed, {"c": '["v1",{"offset":4}]'}).items) == ["k5", "k6"]
    assert keys(run(typed, {"c": '["v1",{"offset":"4"}]'}).items) == ["k5", "k6"]


def test_a_null_key_means_from_the_start_unless_it_is_a_real_key() -> None:
    plain = pages(style="cursor", cursor_param="c", default_limit=2, cursor_format="key")
    assert keys(run(plain, {"c": "null"}).items) == ["k1", "k2"]
    nully = [WorldRecord(key="null", record={}), *RECORDS[:3]]
    assert keys(run(plain, {"c": "null"}, records=nully).items) == ["k1", "k2"]


def test_base64_cursors_survive_form_decoded_plus_signs() -> None:
    cursor = pages(
        style="cursor",
        cursor_param="c",
        default_limit=1,
        cursor_template="~~~{{offset}}",
        cursor_encoding="base64",
    )
    token = make_cursor(cursor, offset=1, key=None)
    assert token == "fn5+MQ=="
    assert keys(run(cursor, {"c": token.replace("+", " ")}).items) == ["k2"]


@pytest.mark.parametrize(
    ("fields", "token"),
    [
        ({"cursor_encoding": "base64"}, "!!!"),
        ({"cursor_encoding": "base64url"}, base64.urlsafe_b64encode(b"\xff\xfe").decode()),
        ({"cursor_format": "key", "cursor_template": {"after": "{{key}}"}}, "not json"),
        ({"cursor_format": "key", "cursor_template": {"after": "{{key}}"}}, '{"before": "k1"}'),
        ({"cursor_format": "key", "cursor_template": {"after": "{{key}}"}}, '{"after": "zzz"}'),
        ({"cursor_template": {"after": "fixed"}}, '{"after": "fixed"}'),
        ({"cursor_template": {"c": "o:{{offset}}"}}, '{"c": "x:2"}'),
        ({"cursor_template": {"c": "o:{{offset}}"}}, '{"c": 2}'),
        ({"cursor_template": {"offset": "{{offset}}"}}, '{"offset": true}'),
        ({"cursor_template": {"offset": "{{offset}}"}}, '{"offset": null}'),
        ({"cursor_template": "page-{{offset}}-x"}, "page-2"),
        ({"cursor_template": "page-{{offset}}-x"}, "page--x"),
        ({"cursor_template": "ab{{offset}}ba"}, "aba"),
        ({}, "-5"),
        ({}, "2.0"),
        ({"cursor_format": "key"}, "k99"),
    ],
)
def test_invalid_cursors(fields: dict[str, Any], token: str) -> None:
    cursor = pages(style="cursor", cursor_param="c", default_limit=2, **fields)
    with pytest.raises(PageError) as caught:
        run(cursor, {"c": token})
    assert (caught.value.param, caught.value.value) == ("c", token)
    assert str(caught.value) == f"invalid c: {token!r}"


def test_cursor_absent_or_empty_starts_at_the_beginning() -> None:
    cursor = pages(style="cursor", cursor_param="c", default_limit=2)
    assert keys(run(cursor, {"c": ""}).items) == ["k1", "k2"]
    no_param = pages(style="cursor", default_limit=2)
    page = run(no_param)
    assert keys(page.items) == ["k1", "k2"]
    assert page.next_cursor == "2"
    assert page.next_url is None


def test_read_cursor_reports_kind_and_position() -> None:
    assert read_cursor(pages(style="cursor"), "17") == ("offset", "17")
    assert read_cursor(pages(style="cursor", cursor_format="key"), "AAPL") == ("key", "AAPL")
    assert read_cursor(KEYED, "eyJhZnRlciI6IkFBUEwifQ") == ("key", "AAPL")


def test_make_cursor_renders_offsets_keys_and_pages() -> None:
    key_cursor = pages(style="cursor", cursor_format="key")
    assert make_cursor(key_cursor, offset=4, key="AAPL") == "AAPL"
    assert make_cursor(key_cursor, offset=4, key=7) == "7"
    assert make_cursor(key_cursor, offset=0, key=None) == "null"
    assert make_cursor(pages(style="cursor"), offset=4, key="AAPL") == "4"
    assert make_cursor(pages(style="page"), offset=4, key="AAPL", page=3) == "3"
    assert make_cursor(KEYED, offset=4, key="AAPL") == "eyJhZnRlciI6IkFBUEwifQ"


def test_page_values() -> None:
    page = run(OFFSET, {"offset": 3})
    assert page_value(page, OFFSET, "total", None) == 7
    assert page_value(page, OFFSET, "count", None) == 3
    assert page_value(page, OFFSET, "page", None) is None
    assert page_value(page, OFFSET, "per_page", None) == 3
    assert page_value(page, OFFSET, "limit", None) == 3
    assert page_value(page, OFFSET, "offset", None) == 3
    assert page_value(page, OFFSET, "total_pages", None) == 3
    assert page_value(page, OFFSET, "has_more", None) is True
    assert page_value(page, OFFSET, "next", "ignored") == "6"
    assert page_value(page, OFFSET, "prev", None) == "0"
    as_urls = OFFSET.model_copy(update={"next_as_url": True})
    assert page_value(page, as_urls, "next", None) == f"{BASE}?offset=6"
    assert page_value(page, as_urls, "next", "https://given/") == "https://given/"
    assert page_value(page, as_urls, "prev", None) == f"{BASE}?offset=0"
    unlimited = run(pages(style="offset"))
    assert page_value(unlimited, OFFSET, "total_pages", None) == 1
    assert page_value(run(PAGED, {"page": 2}), PAGED, "page", None) == 2
