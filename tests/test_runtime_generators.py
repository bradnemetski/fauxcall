from __future__ import annotations

import random
import re
import string
import uuid
from typing import TYPE_CHECKING

import pytest

from fauxcall.runtime import generators

if TYPE_CHECKING:
    from collections.abc import Callable

    from fauxcall.types import JsonValue

NOW_NS = 1_791_681_446_844_021_600  # 2026-10-11T01:17:26.8440216Z


def rng(seed: int = 7) -> random.Random:
    return random.Random(seed)


def test_uuid4_has_version_and_variant_bits_and_is_seeded() -> None:
    values = [generators.uuid4(rng(seed)) for seed in range(50)]
    for value in values:
        parsed = uuid.UUID(value)
        assert str(parsed) == value
        assert parsed.version == 4
        assert parsed.variant == uuid.RFC_4122
    assert len(set(values)) == 50
    assert generators.uuid4(rng(3)) == generators.uuid4(rng(3))


def test_uuid7_encodes_the_millisecond_and_orders_by_time() -> None:
    generator = rng()
    value = generators.uuid7(generator, NOW_NS)
    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    assert parsed.int >> 80 == NOW_NS // 1_000_000
    ordered = [generators.uuid7(generator, NOW_NS + step * 1_000_000) for step in range(200)]
    assert ordered == sorted(ordered)
    assert generators.uuid7(rng(1), NOW_NS) == generators.uuid7(rng(1), NOW_NS)
    assert generators.uuid7(rng(1), NOW_NS) != generators.uuid7(rng(2), NOW_NS)


def test_uuid7_keeps_only_48_bits_of_milliseconds() -> None:
    far_future = ((1 << 48) + 5) * 1_000_000
    assert uuid.UUID(generators.uuid7(rng(), far_future)).int >> 80 == 5


def test_traceparent_format() -> None:
    value = generators.traceparent(rng())
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-00", value)


@pytest.mark.parametrize("length", [1, 2, 16, 32, 64])
def test_hex_string_length_and_alphabet(length: int) -> None:
    for seed in range(20):
        value = generators.hex_string(rng(seed), length)
        assert len(value) == length
        assert set(value) <= set("0123456789abcdef")


@pytest.mark.parametrize("length", [1, 6, 8, 20])
def test_digits_never_start_with_zero(length: int) -> None:
    for seed in range(200):
        value = generators.digits(rng(seed), length)
        assert len(value) == length
        assert value.isdigit()
        assert value[0] != "0"
        assert len(str(int(value))) == length


@pytest.mark.parametrize("length", [1, 16, 40])
def test_alnum_length_and_alphabet(length: int) -> None:
    value = generators.alnum(rng(), length)
    assert len(value) == length
    assert set(value) <= set(string.ascii_letters + string.digits)


@pytest.mark.parametrize("length", [1, 2, 3, 4, 22, 43])
def test_base64url_length_and_alphabet(length: int) -> None:
    for seed in range(20):
        value = generators.base64url(rng(seed), length)
        assert len(value) == length
        assert re.fullmatch(r"[A-Za-z0-9_-]+", value)


@pytest.mark.parametrize(
    "make",
    [generators.hex_string, generators.digits, generators.alnum, generators.base64url],
)
@pytest.mark.parametrize("length", [0, -1, -10])
def test_non_positive_lengths_give_empty_strings(
    make: Callable[[random.Random, int], str], length: int
) -> None:
    assert make(rng(), length) == ""


def test_choice_picks_one_of_the_options_deterministically() -> None:
    options: list[JsonValue] = ["queued", 3, None, {"a": 1}]
    picks = [generators.choice(rng(seed), options) for seed in range(40)]
    assert all(pick in options for pick in picks)
    assert len({repr(pick) for pick in picks}) > 1
    assert generators.choice(rng(9), options) == generators.choice(rng(9), options)
    assert generators.choice(rng(), ("only",)) == "only"


def test_choice_needs_options() -> None:
    with pytest.raises(ValueError, match="at least one option"):
        generators.choice(rng(), [])


def test_same_seed_same_sequence() -> None:
    def run(seed: int) -> list[str]:
        generator = rng(seed)
        return [
            generators.uuid4(generator),
            generators.uuid7(generator, NOW_NS),
            generators.traceparent(generator),
            generators.hex_string(generator, 8),
            generators.digits(generator, 6),
            generators.alnum(generator, 10),
            generators.base64url(generator, 22),
        ]

    assert run(11) == run(11)
    assert run(11) != run(12)
