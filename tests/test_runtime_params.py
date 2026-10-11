from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request
from structlog.testing import capture_logs

from fauxcall.jsonfmt import JsonFloat, dumps
from fauxcall.model.endpoints import EndpointModel, ParamModel
from fauxcall.runtime.params import ParamProblem, coerce, coerce_scalar, parse_params, raw_values

if TYPE_CHECKING:
    from fauxcall.types import JsonValue


def param(name: str = "p", location: str = "query", **fields: Any) -> ParamModel:
    return ParamModel.model_validate({"name": name, "location": location, **fields})


def endpoint(params: list[ParamModel], errors: list[dict[str, Any]] | None = None) -> EndpointModel:
    return EndpointModel.model_validate(
        {
            "id": "GET /pets/{pet-id}",
            "method": "GET",
            "path": "/pets/{pet-id}",
            "params": [p.model_dump(by_alias=True) for p in params],
            "errors": errors or [],
        }
    )


def request(
    *,
    query: str = "",
    path_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    cookie: str | None = None,
) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
    if cookie is not None:
        raw.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/pets/1",
            "query_string": query.encode(),
            "headers": raw,
            "path_params": path_params or {},
        }
    )


# -- scalars -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [("42", 42), (" 7 ", 7), ("+3", 3), ("-12", -12)])
def test_integers(text: str, expected: int) -> None:
    value = coerce_scalar(text, {"type": "integer"})
    assert value == expected
    assert type(value) is int


@pytest.mark.parametrize("text", ["4.0", "abc", "", "1e3", "٣", "0x1f"])
def test_invalid_integers(text: str) -> None:
    with pytest.raises(ValueError, match="not an integer"):
        coerce_scalar(text, {"type": "integer"})


@pytest.mark.parametrize(
    ("text", "spelled"),
    [
        ("1.50", "1.50"),
        ("-0.25", "-0.25"),
        ("1e3", "1e3"),
        ("2E-2", "2E-2"),
        ("+1.5", "1.5"),
        (".5", "0.5"),
        ("5.", "5.0"),
        ("01.5", "1.5"),
    ],
)
def test_numbers_keep_valid_json_spellings(text: str, spelled: str) -> None:
    value = coerce_scalar(text, {"type": "number"})
    assert isinstance(value, JsonFloat)
    assert dumps(value) == spelled
    assert value == float(text)


def test_integral_numbers_stay_integers() -> None:
    assert coerce_scalar("5", {"type": "number"}) == 5
    assert type(coerce_scalar("+5", {"type": "number"})) is int


@pytest.mark.parametrize("text", ["nan", "inf", "1,5", "", "1.2.3"])
def test_invalid_numbers(text: str) -> None:
    with pytest.raises(ValueError, match="not a number"):
        coerce_scalar(text, {"type": "number"})


@pytest.mark.parametrize(
    ("schema", "text", "ok"),
    [
        ({"type": "integer", "minimum": 1, "maximum": 100}, "1", True),
        ({"type": "integer", "minimum": 1, "maximum": 100}, "0", False),
        ({"type": "integer", "minimum": 1, "maximum": 100}, "101", False),
        ({"type": "integer", "exclusiveMinimum": 0}, "0", False),
        ({"type": "integer", "exclusiveMaximum": 10}, "9", True),
        ({"type": "number", "exclusiveMaximum": 1.5}, "1.5", False),
        ({"type": "number", "maximum": 1.5}, "1.50", True),
        ({"type": "integer", "minimum": True}, "0", True),  # booleans are not bounds
    ],
)
def test_bounds(schema: dict[str, JsonValue], text: str, ok: bool) -> None:
    if ok:
        coerce_scalar(text, schema)
    else:
        with pytest.raises(ValueError, match="imum"):
            coerce_scalar(text, schema)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("true", True), ("FALSE", False), ("1", True), ("0", False), (" True ", True)],
)
def test_booleans(text: str, expected: bool) -> None:
    assert coerce_scalar(text, {"type": "boolean"}) is expected


def test_invalid_booleans() -> None:
    with pytest.raises(ValueError, match="not a boolean"):
        coerce_scalar("yes", {"type": "boolean"})


def test_strings_lengths_and_patterns() -> None:
    schema: dict[str, JsonValue] = {
        "type": "string",
        "minLength": 2,
        "maxLength": 4,
        "pattern": "[a-z]",
    }
    assert coerce_scalar("ab", schema) == "ab"
    assert coerce_scalar(" a1 ", schema) == " a1 "  # strings are taken as sent
    with pytest.raises(ValueError, match="too short"):
        coerce_scalar("a", schema)
    with pytest.raises(ValueError, match="too long"):
        coerce_scalar("abcde", schema)
    with pytest.raises(ValueError, match="pattern"):
        coerce_scalar("123", schema)


def test_patterns_python_cannot_compile_are_not_enforced() -> None:
    with capture_logs() as logs:
        assert coerce_scalar("Moët", {"type": "string", "pattern": r"^\p{L}+$"}) == "Moët"
    assert [entry["event"] for entry in logs] == ["param_pattern_invalid"]


@pytest.mark.parametrize(
    ("schema", "kind"),
    [
        ({}, str),
        ({"type": ["null", "integer"]}, int),
        ({"type": ["null"]}, str),
        ({"type": 5}, str),
    ],
)
def test_schema_types(schema: dict[str, JsonValue], kind: type) -> None:
    assert type(coerce_scalar("7", schema)) is kind


def test_enums() -> None:
    assert coerce_scalar("sold", {"type": "string", "enum": ["available", "sold"]}) == "sold"
    with pytest.raises(ValueError, match="not in enum"):
        coerce_scalar("lost", {"type": "string", "enum": ["available", "sold"]})
    assert coerce_scalar("2", {"type": "integer", "enum": [1, 2]}) == 2
    untyped = coerce_scalar("2", {"enum": [1, 2]})  # matched by JSON spelling, typed member
    assert untyped == 2
    assert type(untyped) is int
    assert coerce_scalar("true", {"enum": [True]}) is True
    assert coerce_scalar("x", {"type": "string", "enum": []}) == "x"


# -- arrays ------------------------------------------------------------------------------------


def test_exploded_form_arrays_accept_repeats_or_one_delimited_value() -> None:
    ids = param("ids", schema={"type": "array", "items": {"type": "integer"}})
    assert coerce(ids, ["1", "2"]) == [1, 2]
    assert coerce(ids, ["1,2,,3"]) == [1, 2, 3]
    with pytest.raises(ValueError, match="not an integer"):
        coerce(ids, ["1", "x"])


def test_repeated_exploded_values_are_not_split() -> None:
    names = param("names", schema={"type": "array", "items": {"type": "string"}})
    assert coerce(names, ["a,b", "c"]) == ["a,b", "c"]


@pytest.mark.parametrize(
    ("style", "explode", "raw", "expected"),
    [
        ("form", False, ["a,b", "c"], ["a", "b", "c"]),
        ("spaceDelimited", None, ["a b"], ["a", "b"]),
        ("pipeDelimited", None, ["a|b|"], ["a", "b"]),
        ("simple", None, ["x,y"], ["x", "y"]),
        ("deepObject", None, ["x,y"], ["x", "y"]),
    ],
)
def test_array_styles(
    style: str, explode: bool | None, raw: list[str], expected: list[str]
) -> None:
    tags = param("tags", style=style, explode=explode, schema={"type": "array"})
    assert coerce(tags, raw) == expected


def test_array_item_counts() -> None:
    tags = param("tags", schema={"type": "array", "minItems": 1, "maxItems": 2, "items": []})
    assert coerce(tags, ["a,b"]) == ["a", "b"]
    with pytest.raises(ValueError, match="too few"):
        coerce(tags, [""])
    with pytest.raises(ValueError, match="too many"):
        coerce(tags, ["a,b,c"])


def test_no_schema_means_string() -> None:
    assert coerce(param("q"), ["apple pie"]) == "apple pie"


# -- reading the request -----------------------------------------------------------------------


def test_raw_values_from_every_location() -> None:
    req = request(
        query="tag=a&tag=b&empty=",
        path_params={"pet_id": "42"},
        headers={"X-Trace": "t-1"},
        cookie="session=abc",
    )
    assert raw_values(param("tag"), req) == ["a", "b"]
    assert raw_values(param("empty"), req) == [""]
    assert raw_values(param("absent"), req) is None
    assert raw_values(param("pet-id", "path"), req) == ["42"]
    assert raw_values(param("owner", "path"), req) is None
    assert raw_values(param("x-trace", "header"), req) == ["t-1"]
    assert raw_values(param("x-other", "header"), req) is None
    assert raw_values(param("session", "cookie"), req) == ["abc"]
    assert raw_values(param("theme", "cookie"), req) is None


def test_parse_params_converts_and_applies_defaults() -> None:
    model = endpoint(
        [
            param("pet-id", "path", required=True, schema={"type": "integer"}),
            param("limit", schema={"type": "integer"}, default=20),
            param("sort"),
            param("verbose", "header", schema={"type": "boolean"}),
        ]
    )
    values = parse_params(model, request(path_params={"pet_id": "42"}, headers={"verbose": "1"}))
    assert values == {"pet-id": 42, "limit": 20, "verbose": True}


@pytest.mark.parametrize("query", ["", "q=", "other=1"])
def test_missing_required_parameters(query: str) -> None:
    model = endpoint([param("q", required=True)])
    with pytest.raises(ParamProblem) as caught:
        parse_params(model, request(query=query))
    assert (caught.value.condition, caught.value.param, caught.value.value) == (
        "missing_param",
        "q",
        None,
    )
    assert str(caught.value) == "missing_param: q=None"


def test_problems_follow_declaration_order() -> None:
    model = endpoint(
        [
            param("limit", schema={"type": "integer"}, on_invalid="error"),
            param("q", required=True),
        ]
    )
    with pytest.raises(ParamProblem) as caught:
        parse_params(model, request(query="limit=abc"))
    assert (caught.value.condition, caught.value.param, caught.value.value) == (
        "invalid_param",
        "limit",
        "abc",
    )


def test_on_invalid_ignore_and_coerce() -> None:
    model = endpoint(
        [
            param("limit", schema={"type": "integer"}, on_invalid="ignore", default=10),
            param("page", schema={"type": "integer"}, on_invalid="ignore"),
            param("size", schema={"type": "integer"}, on_invalid="coerce"),
            param(
                "ids", schema={"type": "array", "items": {"type": "integer"}}, on_invalid="coerce"
            ),
        ]
    )
    values = parse_params(model, request(query="limit=x&page=y&size=big&ids=1&ids=two"))
    assert values == {"limit": 10, "size": "big", "ids": ["1", "two"]}


@pytest.mark.parametrize(
    ("errors", "raises"),
    [
        ([], False),
        ([{"when": "invalid_param", "template": "bad", "param": "limit"}], True),
        ([{"when": "invalid_param", "template": "bad"}], True),
        ([{"when": "invalid_param", "template": "bad", "param": "other"}], False),
    ],
)
def test_on_invalid_unknown_depends_on_a_recorded_error_case(
    errors: list[dict[str, Any]], raises: bool
) -> None:
    model = endpoint([param("limit", schema={"type": "integer"})], errors)
    req = request(query="limit=abc")
    if raises:
        with pytest.raises(ParamProblem, match="invalid_param"):
            parse_params(model, req)
    else:
        assert parse_params(model, req) == {"limit": "abc"}


def test_path_parameters_are_found_by_their_routed_identifier() -> None:
    model = endpoint([param("pet-id", "path", required=True, schema={"type": "integer"})])
    assert parse_params(model, request(path_params={"pet_id": "7"})) == {"pet-id": 7}
    assert parse_params(model, request(path_params={"pet-id": "8"})) == {"pet-id": 8}
    with pytest.raises(ParamProblem, match="missing_param"):
        parse_params(model, request())


def test_empty_optional_values_are_validated_like_any_other() -> None:
    model = endpoint(
        [param("q", schema={"type": "string"}), param("n", schema={"type": "integer"})]
    )
    assert parse_params(model, request(query="q=&n=1")) == {"q": "", "n": 1}
