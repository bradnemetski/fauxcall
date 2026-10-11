from __future__ import annotations

import copy
import json
import pickle

import pytest

from fauxcall import jsonfmt
from fauxcall.jsonfmt import (
    COMPACT,
    JsonFloat,
    JsonStyle,
    canonical_dumps,
    dumps,
    infer_style,
    loads,
    plain,
)


def test_loads_preserves_float_spelling_and_key_order() -> None:
    value = loads('{"z":1,"price":105.50,"tiny":1E-5,"n":[1.0,2]}')
    assert isinstance(value, dict)
    assert list(value) == ["z", "price", "tiny", "n"]
    price = value["price"]
    assert isinstance(price, JsonFloat)
    assert price == 105.5
    assert price.lexeme == "105.50"
    assert dumps(value) == '{"z":1,"price":105.50,"tiny":1E-5,"n":[1.0,2]}'


def test_loads_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError, match="NaN"):
        loads("[NaN]")
    with pytest.raises(ValueError, match="Infinity"):
        loads("[Infinity]")


def test_json_float_from_float_and_copies() -> None:
    value = JsonFloat(0.1)
    assert value.lexeme == "0.1"
    assert copy.deepcopy(JsonFloat("2.50")).lexeme == "2.50"
    restored = pickle.loads(pickle.dumps(JsonFloat("3.000")))  # noqa: S301
    assert isinstance(restored, JsonFloat)
    assert restored.lexeme == "3.000"
    # Arithmetic produces plain floats.
    assert type(JsonFloat("1.5") + 1) is float


def test_plain_strips_lexemes_recursively() -> None:
    value = loads('{"a":[1.50,{"b":2.0}],"c":"x","d":null}')
    stripped = plain(value)
    assert stripped == {"a": [1.5, {"b": 2.0}], "c": "x", "d": None}
    assert isinstance(stripped, dict)
    inner = stripped["a"]
    assert isinstance(inner, list)
    assert type(inner[0]) is float


def test_dumps_scalars_and_containers() -> None:
    assert dumps(None) == "null"
    assert dumps(True) == "true"  # noqa: FBT003
    assert dumps(False) == "false"  # noqa: FBT003
    assert dumps(3) == "3"
    assert dumps(2.5) == "2.5"
    assert dumps(1e-5) == "1e-05"
    assert dumps([]) == "[]"
    assert dumps({}) == "{}"
    assert dumps({"a": [1, {"b": None}]}) == '{"a":[1,{"b":null}]}'


def test_dumps_rejects_non_json() -> None:
    with pytest.raises(ValueError, match="cannot be represented"):
        dumps(float("nan"))
    with pytest.raises(ValueError, match="cannot be represented"):
        dumps([float("inf")])
    with pytest.raises(TypeError, match="not JSON serializable"):
        dumps({"a": object()})  # type: ignore[dict-item]


def test_string_escaping_matches_stdlib_by_default() -> None:
    text = 'quote" back\\ nl\n tab\t ctrl\x01 bell\b ff\f cr\r é 漢 😀 /</>'
    assert dumps(text) == json.dumps(text, ensure_ascii=False)


def test_ensure_ascii_escapes_including_surrogates() -> None:
    style = JsonStyle(ensure_ascii=True)
    assert dumps("é😀", style) == json.dumps("é😀", ensure_ascii=True)
    assert dumps("é", JsonStyle(ensure_ascii=True, upper_hex=True)) == '"\\u00E9"'


def test_html_and_slash_escaping() -> None:
    style = JsonStyle(escape_html=True, escape_slash=True, upper_hex=True)
    assert dumps("<a href='/x'>&</a>", style) == (
        '"\\u003Ca href=\\u0027\\/x\\u0027\\u003E\\u0026\\u003C\\/a\\u003E"'
    )


def test_spaced_and_indented_styles() -> None:
    spaced = JsonStyle(item_separator=", ", key_separator=": ")
    assert dumps({"a": [1, 2]}, spaced) == '{"a": [1, 2]}'
    pretty = JsonStyle(item_separator=",", key_separator=": ", indent=2, trailing_newline=True)
    assert (
        dumps({"a": [1, 2], "b": {}}, pretty) == '{\n  "a": [\n    1,\n    2\n  ],\n  "b": {}\n}\n'
    )


def test_canonical_dumps_is_sorted_and_plain() -> None:
    assert canonical_dumps(loads('{"b":1.50,"a":"é"}')) == '{"a":"\\u00e9","b":1.5}'


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        (['{"a":1,"b":[1,2]}'], COMPACT),
        (['{"a": 1, "b": [1, 2]}'], JsonStyle(item_separator=", ", key_separator=": ")),
        (
            ['{\n    "a": 1,\n    "b": 2\n}\n'],
            JsonStyle(item_separator=",", key_separator=": ", indent=4, trailing_newline=True),
        ),
        (['{"u":"\\u00E9"}'], JsonStyle(ensure_ascii=True, upper_hex=True)),
        (['{"u":"\\u00e9 é"}'], JsonStyle(ensure_ascii=False)),
        (
            ['{"h":"\\u003Cb\\u003E","p":"a\\/b"}'],
            JsonStyle(escape_html=True, escape_slash=True, upper_hex=True),
        ),
        (['{"s":"a, b: c"}', '{"x":1}'], COMPACT),
        ([], COMPACT),
    ],
)
def test_infer_style(samples: list[str], expected: JsonStyle) -> None:
    assert infer_style(samples) == expected


def test_infer_style_round_trips_real_bodies() -> None:
    body = (
        '{"date_time":"2026-10-10T21:17:26.379953-04:00",'
        '"utc_offset_seconds":-14400,"dst_active":true}'
    )
    assert dumps(loads(body), infer_style([body])) == body
    pretty = json.dumps({"a": [1, {"b": "é"}]}, indent=2)
    assert dumps(loads(pretty), infer_style([pretty])) == pretty


def test_style_is_frozen_model() -> None:
    style = JsonStyle()
    with pytest.raises(ValueError, match="frozen"):
        style.indent = 2  # type: ignore[misc]
    assert JsonStyle.model_validate(style.model_dump()) == style
    assert jsonfmt.COMPACT.item_separator == ","
