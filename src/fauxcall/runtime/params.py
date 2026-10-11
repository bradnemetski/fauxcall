"""Read and validate request parameters the way the real API does.

FastAPI's own validation is bypassed on purpose: real APIs differ in what they reject, how strictly,
and what their errors look like. Each parameter's observed reaction (``on_invalid``) decides whether
a malformed value is an error (rendered with the real error template), ignored, or passed through:

* ``error``: always :class:`ParamProblem` (``invalid_param``);
* ``ignore``: as if absent (the default applies);
* ``coerce``: the raw text is passed on unconverted;
* ``unknown`` (not probed): an error when the endpoint has an ``invalid_param`` error case for the
  parameter (or a generic one), else passed on like ``coerce``.
"""

from __future__ import annotations

import functools
import operator
import re
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt
from fauxcall.jsonfmt import JsonFloat
from fauxcall.logs import get_logger
from fauxcall.runtime.routing import path_value

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from starlette.requests import Request

    from fauxcall.model.endpoints import EndpointModel, ParamModel
    from fauxcall.model.errors import ErrorCondition
    from fauxcall.types import JsonValue

log = get_logger(__name__)

_INTEGER: Final = re.compile(r"[+-]?[0-9]+")
_NUMBER: Final = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_JSON_NUMBER: Final = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_DELIMITERS: Final = {"form": ",", "spaceDelimited": " ", "pipeDelimited": "|"}
_BOUNDS: Final[tuple[tuple[str, Callable[[float, float], bool]], ...]] = (
    ("minimum", operator.ge),
    ("maximum", operator.le),
    ("exclusiveMinimum", operator.gt),
    ("exclusiveMaximum", operator.lt),
)


class ParamProblem(Exception):  # noqa: N818 - signals a client error, rendered by the pipeline
    """A required parameter is missing or a value is malformed."""

    def __init__(self, condition: ErrorCondition, param: str, value: str | None) -> None:
        super().__init__(f"{condition}: {param}={value!r}")
        self.condition = condition
        self.param = param
        self.value = value


def _schema_type(schema: Mapping[str, JsonValue]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    return kind if isinstance(kind, str) else "string"


def _check_bounds(value: float, schema: Mapping[str, JsonValue]) -> None:
    for key, within in _BOUNDS:
        bound = schema.get(key)
        if (
            isinstance(bound, int | float)
            and not isinstance(bound, bool)
            and not within(value, bound)
        ):
            raise ValueError(f"{key} {bound}")


@functools.lru_cache(maxsize=256)
def _pattern(source: str) -> re.Pattern[str] | None:
    try:
        return re.compile(source)
    except re.error as exc:  # ECMA-262 constructs Python lacks (e.g. \p{L}): don't validate
        log.warning("param_pattern_invalid", pattern=source, error=str(exc))
        return None


def _number(text: str) -> int | JsonFloat:
    """An integer, or a float that keeps its spelling when that spelling is valid JSON."""
    if not any(c in text for c in ".eE"):
        return int(text)
    if _JSON_NUMBER.fullmatch(text):
        return JsonFloat(text)  # e.g. "105.50" stays "105.50"
    return JsonFloat(float(text))  # "+1.5", ".5", "5." are re-spelled as valid JSON


def _in_enum(value: JsonValue, text: str, enum: list[JsonValue]) -> JsonValue:
    """The value as an enum member, matching typed values or their JSON spelling."""
    if value in enum:
        return value
    for member in enum:
        if text == (member if isinstance(member, str) else jsonfmt.dumps(member)):
            return member
    raise ValueError("not in enum")


def coerce_scalar(text: str, schema: Mapping[str, JsonValue]) -> JsonValue:
    """Convert one raw value according to a (resolved) JSON schema; ``ValueError`` if invalid."""
    kind = _schema_type(schema)
    stripped = text.strip()
    value: JsonValue
    if kind == "integer":
        if not _INTEGER.fullmatch(stripped):
            raise ValueError("not an integer")
        value = int(stripped)
        _check_bounds(value, schema)
    elif kind == "number":
        if not _NUMBER.fullmatch(stripped):
            raise ValueError("not a number")
        number = _number(stripped)
        _check_bounds(float(number), schema)
        value = number
    elif kind == "boolean":
        lowered = stripped.lower()
        if lowered not in {"true", "false", "1", "0"}:
            raise ValueError("not a boolean")
        value = lowered in {"true", "1"}
    else:
        value = text
        _check_string(text, schema)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        value = _in_enum(value, text, enum)
    return value


def _check_string(text: str, schema: Mapping[str, JsonValue]) -> None:
    min_length, max_length = schema.get("minLength"), schema.get("maxLength")
    if isinstance(min_length, int) and len(text) < min_length:
        raise ValueError("too short")
    if isinstance(max_length, int) and len(text) > max_length:
        raise ValueError("too long")
    source = schema.get("pattern")
    pattern = _pattern(source) if isinstance(source, str) else None
    if pattern is not None and not pattern.search(text):
        raise ValueError("pattern mismatch")


def coerce(param: ParamModel, raw: Sequence[str]) -> JsonValue:
    """Convert a parameter's raw value(s) to JSON according to its schema and style.

    Arrays accept repeated parameters (``?id=1&id=2``) and, when given one value or a non-exploded
    style, delimited values (``form``/``simple``: ``,``, ``spaceDelimited``: `` ``,
    ``pipeDelimited``: ``|``).
    """
    schema = param.param_schema if isinstance(param.param_schema, dict) else {}
    if _schema_type(schema) != "array":
        return coerce_scalar(raw[0], schema)
    items = schema.get("items")
    item_schema = items if isinstance(items, dict) else {}
    explode = param.explode if param.explode is not None else (param.style in {None, "form"})
    values: list[str] = list(raw)
    if not explode or len(raw) == 1:
        delimiter = _DELIMITERS.get(param.style or "form", ",")
        values = [part for text in raw for part in text.split(delimiter) if part != ""]
    coerced: list[JsonValue] = [coerce_scalar(v, item_schema) for v in values]
    min_items, max_items = schema.get("minItems"), schema.get("maxItems")
    if isinstance(min_items, int) and len(coerced) < min_items:
        raise ValueError("too few items")
    if isinstance(max_items, int) and len(coerced) > max_items:
        raise ValueError("too many items")
    return coerced


def raw_values(param: ParamModel, request: Request) -> list[str] | None:
    """The parameter's raw value(s) from the request, or ``None`` when absent."""
    match param.location:
        case "path":
            value = path_value(request.path_params, param.name)
            return None if value is None else [str(value)]
        case "query":
            values = request.query_params.getlist(param.name)
            return values or None
        case "header":
            header = request.headers.get(param.name)
            return None if header is None else [header]
        case _:
            cookie = request.cookies.get(param.name)
            return None if cookie is None else [cookie]


def parse_params(endpoint: EndpointModel, request: Request) -> dict[str, JsonValue]:
    """Parse every declared parameter, raising :class:`ParamProblem` like the real API would.

    Parameters are checked in declaration order, so the first problem reported is the one the real
    API reports. Absent optional parameters get their default (if any).
    """
    values: dict[str, JsonValue] = {}
    for param in endpoint.params:
        raw = raw_values(param, request)
        if raw is not None and param.empty_as_absent and all(value == "" for value in raw):
            raw = None
        if raw is None or (param.location != "path" and raw == [""] and param.required):
            if param.required:
                raise ParamProblem("missing_param", param.name, None)
            if param.default is not None:
                values[param.name] = param.default
            continue
        try:
            values[param.name] = coerce(param, raw)
        except ValueError:
            behavior = param.on_invalid
            has_template = endpoint.error_case("invalid_param", param.name) is not None
            if behavior == "error" or (behavior == "unknown" and has_template):
                raise ParamProblem("invalid_param", param.name, raw[0]) from None
            if behavior == "ignore":
                if param.default is not None:
                    values[param.name] = param.default
                continue
            values[param.name] = raw[0] if len(raw) == 1 else list(raw)
    return values
