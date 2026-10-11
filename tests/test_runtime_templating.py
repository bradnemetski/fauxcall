from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall.jsonfmt import JsonFloat, dumps
from fauxcall.runtime.templating import (
    UNRESOLVED,
    _Unresolved,
    as_text,
    chain,
    from_mapping,
    placeholders,
    render,
    render_string,
    render_text,
)

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

VALUES: dict[str, JsonValue] = {
    "id": 42,
    "price": JsonFloat("105.50"),
    "flag": True,
    "nothing": None,
    "name": "rex",
    "tags": ["a", "b"],
    "owner": {"id": 7, "vip": False},
}


def resolve(name: str, arg: str | None) -> JsonValue | _Unresolved:
    return from_mapping(VALUES)(name, arg)


@pytest.mark.parametrize("name", list(VALUES))
def test_lone_placeholder_keeps_the_value_type(name: str) -> None:
    rendered = render_string("{{" + name + "}}", resolve)
    assert rendered == VALUES[name]
    assert type(rendered) is type(VALUES[name])


def test_lone_float_keeps_its_spelling_and_containers_are_fresh_copies() -> None:
    price = render_string("{{price}}", resolve)
    assert isinstance(price, JsonFloat)
    assert dumps(price) == "105.50"
    tags = render_string("{{tags}}", resolve)
    assert isinstance(tags, list)
    tags.append("mutated")
    assert VALUES["tags"] == ["a", "b"]


def test_embedded_placeholders_are_spelled_as_json() -> None:
    text = "id={{id}} price={{price}} flag={{flag}} none={{nothing}} name={{name}}"
    assert render_string(text, resolve) == "id=42 price=105.50 flag=true none=null name=rex"
    assert render_string("tags={{tags}} owner={{owner}}", resolve) == (
        'tags=["a","b"] owner={"id":7,"vip":false}'
    )


def test_adjacent_placeholders_render_as_text() -> None:
    assert render_string("{{id}}{{name}}", resolve) == "42rex"


def test_unknown_placeholders_are_kept_verbatim() -> None:
    assert render_string("{{missing}}", resolve) == "{{missing}}"
    assert render_string("{{ missing:arg }}", resolve) == "{{ missing:arg }}"
    assert render_string("{{id}}-{{missing}}", resolve) == "42-{{missing}}"
    assert render_string("no placeholders", resolve) == "no placeholders"


def test_text_around_a_lone_placeholder_is_preserved() -> None:
    assert render_string("{{id}}\n", resolve) == "42\n"
    assert render_string(" {{id}}", resolve) == " 42"


def test_whitespace_and_arguments() -> None:
    seen: list[tuple[str, str | None]] = []

    def record(name: str, arg: str | None) -> JsonValue | _Unresolved:
        seen.append((name, arg))
        return f"{name}|{arg}"

    assert render_string("{{ now:iso_ms }}", record) == "now|iso_ms"
    assert render_string("{{now:%H:%M}}", record) == "now|%H:%M"
    assert render_string("{{hex:16}}", record) == "hex|16"
    assert render_string("{{header.x-request-id}}", record) == "header.x-request-id|None"
    assert render_string("{{ uuid }}", record) == "uuid|None"
    assert seen == [
        ("now", "iso_ms"),
        ("now", "%H:%M"),
        ("hex", "16"),
        ("header.x-request-id", None),
        ("uuid", None),
    ]


def test_render_walks_templates_and_renders_keys_without_touching_the_template() -> None:
    template: JsonValue = {
        "error": {"code": "{{name}}", "status": "{{id}}", "{{name}}_key": ["{{flag}}", 1, None]},
        "{{id}}": "x {{price}}",
        "kept": JsonFloat("2.50"),
        "{{owner}}": "{{missing}}",
    }
    before = copy.deepcopy(template)
    rendered = render(template, resolve)
    assert rendered == {
        "error": {"code": "rex", "status": 42, "rex_key": [True, 1, None]},
        "42": "x 105.50",
        "kept": 2.5,
        '{"id":7,"vip":false}': "{{missing}}",
    }
    assert template == before
    assert isinstance(rendered, dict)
    assert dumps(rendered["kept"]) == "2.50"


def test_render_text_spells_lone_values_as_json() -> None:
    assert render_text("{{flag}}", resolve) == "true"
    assert render_text("{{nothing}}", resolve) == "null"
    assert render_text("{{id}}", resolve) == "42"
    assert render_text("{{name}}", resolve) == "rex"
    assert render_text("{{missing}}", resolve) == "{{missing}}"


def test_as_text_falls_back_to_str_for_values_json_cannot_spell() -> None:
    assert as_text(float("nan")) == "nan"
    moment = datetime(2026, 10, 10, tzinfo=UTC)
    assert as_text(moment) == str(moment)  # type: ignore[arg-type]


def test_placeholders_lists_names_in_first_seen_order() -> None:
    template: JsonValue = {
        "{{key_name}}": ["{{a}} and {{b}}", {"nested": "{{a}}"}],
        "plain": "{{ c:arg }}",
        "number": 5,
    }
    assert placeholders(template) == ["key_name", "a", "b", "c"]
    assert placeholders("nothing") == []


def test_chain_uses_the_first_resolver_that_knows_a_name() -> None:
    first = from_mapping({"a": 1})
    second = from_mapping({"a": 2, "b": None})
    combined = chain(first, second)
    assert combined("a", None) == 1
    assert combined("b", None) is None
    assert combined("c", None) is UNRESOLVED
    assert render_string("{{b}}|{{c}}", combined) == "null|{{c}}"


def test_from_mapping_ignores_the_argument_and_sees_later_updates() -> None:
    values: dict[str, JsonValue] = {"x": "1"}
    resolver = from_mapping(values)
    assert resolver("x", "ignored") == "1"
    values["y"] = 2
    assert resolver("y", None) == 2
    assert resolver("z", None) is UNRESOLVED
    assert repr(UNRESOLVED) == "UNRESOLVED"
