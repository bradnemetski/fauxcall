"""JavaScript number formatting and parsing, against values the real server produced."""

from __future__ import annotations

import math

import pytest

from fauxcall_everything.jsnum import as_double, is_positive_integer, js_number, js_string


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (-0.0, "0"),
        (3, "3"),
        (2.5, "2.5"),
        (0.1 + 0.2, "0.30000000000000004"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.5e-7, "1.5e-7"),
        (0.000001, "0.000001"),
        (1.25e-10, "1.25e-10"),
        (-123.456, "-123.456"),
        (9007199254740993, "9007199254740992"),
        (math.inf, "Infinity"),
        (-math.inf, "-Infinity"),
        (math.nan, "NaN"),
    ],
)
def test_js_string(value: float, text: str) -> None:
    assert js_string(value) == text


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("", 0.0),
        ("  ", 0.0),
        (" 5 ", 5.0),
        ("01", 1.0),
        ("1e2", 100.0),
        ("0x10", 16.0),
        ("0b101", 5.0),
        ("0o17", 15.0),
        ("Infinity", math.inf),
        ("-Infinity", -math.inf),
        (".5", 0.5),
    ],
)
def test_js_number(text: str, value: float) -> None:
    assert js_number(text) == value


@pytest.mark.parametrize("text", ["abc", "0xZZ", "1e", "1,5", "--1"])
def test_js_number_nan(text: str) -> None:
    assert math.isnan(js_number(text))


def test_huge_integers_become_infinity() -> None:
    assert as_double(10**400) == math.inf
    assert as_double(-(10**400)) == -math.inf


def test_positive_integers() -> None:
    assert is_positive_integer(1)
    assert is_positive_integer(1e21)
    assert not is_positive_integer(0)
    assert not is_positive_integer(1.5)
    assert not is_positive_integer(math.inf)
