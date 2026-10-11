"""Infer JSON Schemas from recorded samples, merge them, and compare them with documented ones.

Real APIs are often under-documented ("returns JSON"), so FauxCall learns response shapes from what
it records:

* :func:`infer_schema` builds a JSON Schema (a draft 2020-12 subset) that every sample validates
  against, with string formats, examples and the exact spelling of timestamps (``x-time-format``);
* :func:`merge_schemas` combines two inferred schemas as if their samples had been inferred
  together;
* :func:`compare_schemas` lists where observed data disagrees with an OpenAPI-flavored documented
  schema: undocumented fields, required fields never seen, type, nullability and format mismatches.

Inference is a single pass over the samples into an accumulator tree, rendered once at the end.
Inference, merging and rendering use explicit stacks, so deeply nested documents never hit Python's
recursion limit; the comparison recurses along the observed schema, which documented APIs keep
shallow.

``format`` is an annotation (the JSON Schema 2020-12 default): ``date-time`` is reported for every
ISO-8601 timestamp :func:`fauxcall.timefmt.infer_time_format` understands, including naive ones;
``x-time-format`` records the precise spelling the fake must reproduce.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from fauxcall.jsonpath import WILDCARD, format_path
from fauxcall.model.common import Model, RawJson
from fauxcall.timefmt import infer_time_format

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from fauxcall.jsonpath import Segment
    from fauxcall.types import JsonScalar, JsonValue

JsonType = Literal["null", "boolean", "integer", "number", "string", "object", "array"]
"""A JSON Schema type name."""

_SCALAR_TYPES: Final = frozenset({"boolean", "integer", "number", "string"})
_CONTAINER_TYPES: Final = frozenset({"object", "array"})
_TIME_KIND_FORMATS: Final[dict[str, str]] = {"iso": "date-time", "date": "date", "time": "time"}
_MAX_TIME_TEXT: Final = 64
_DIGIT: Final = re.compile(r"\d")

# ---------------------------------------------------------------------------
# Value classification (shared with fauxcall.analysis.profile)
# ---------------------------------------------------------------------------


_PYTHON_TYPES: Final[tuple[tuple[type, JsonType], ...]] = (
    (bool, "boolean"),  # before int: bool is an int subclass
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (dict, "object"),
    (list, "array"),
)


def _type_of(value: object) -> JsonType | None:
    if value is None:
        return "null"
    for python_type, name in _PYTHON_TYPES:
        if isinstance(value, python_type):
            return name
    return None


def json_type(value: JsonValue) -> JsonType:
    """The JSON Schema type name of ``value`` (``True`` is a boolean, not an integer).

    Raises:
        TypeError: ``value`` is not a JSON value.
    """
    name = _type_of(value)
    if name is None:
        raise TypeError(f"{type(value).__name__} is not a JSON value")
    return name


_UUID_RE: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_LABEL: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_EMAIL_RE: Final = re.compile(rf"[^@\s]+@{_LABEL}(?:\.{_LABEL})+")
_URI_RE: Final = re.compile(r"https?://[^\s/?#]+(?:[/?#]\S*)?", re.IGNORECASE)
_IPV4_RE: Final = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_IPV6_RE: Final = re.compile(r"[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


def _is_ipv4(text: str) -> bool:
    if _IPV4_RE.fullmatch(text) is None:
        return False
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _is_ipv6(text: str) -> bool:
    if _IPV6_RE.fullmatch(text) is None:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


_FORMAT_CHECKS: Final[dict[str, Callable[[str], bool]]] = {
    "uuid": lambda text: _UUID_RE.fullmatch(text) is not None,
    "email": lambda text: _EMAIL_RE.fullmatch(text) is not None,
    "uri": lambda text: _URI_RE.fullmatch(text) is not None,
    "ipv4": _is_ipv4,
    "ipv6": _is_ipv6,
}
STRING_FORMATS: Final = tuple(_FORMAT_CHECKS)
"""Formats :func:`string_format` recognizes, besides timestamps."""


def string_format(text: str) -> str | None:
    """The ``format`` of one string: uuid, email, uri (http/https only), ipv4 or ipv6."""
    for name, check in _FORMAT_CHECKS.items():
        if check(text):
            return name
    return None


def has_string_format(text: str, name: str) -> bool:
    """Whether ``text`` has the :func:`string_format` ``name``."""
    return _FORMAT_CHECKS[name](text)


# ---------------------------------------------------------------------------
# Accumulator tree
# ---------------------------------------------------------------------------

type _Key = tuple[str, bool | int | float | str]
"""Identity of a scalar under JSON equality: ``True`` != ``1`` but ``1`` == ``1.0``."""

type _Seen = dict[_Key, tuple[int, JsonScalar]]
"""Distinct scalars with the sequence number of their first sighting (for first-seen order)."""


def _scalar_key(value: JsonValue) -> tuple[JsonType, _Key]:
    if isinstance(value, bool):
        return "boolean", ("b", value)
    if isinstance(value, int):
        return "integer", ("n", value)
    if isinstance(value, float):
        return "number", ("n", value)
    if isinstance(value, str):
        return "string", ("s", value)
    raise TypeError(f"{type(value).__name__} is not a JSON value")


@dataclass(frozen=True, slots=True)
class _Options:
    max_examples: int
    enum_threshold: int


class _Strings:
    """Evidence only strings carry: a shared simple format and candidate timestamps."""

    __slots__ = ("resolved", "seen", "simple", "time_values")

    def __init__(self) -> None:
        self.seen = False
        self.simple: str | None = None
        self.time_values: dict[str, None] | None = {}
        self.resolved: tuple[str | None, JsonValue] | None = None

    def observe(self, text: str) -> None:
        if not self.seen:
            self.seen = True
            self.simple = string_format(text)
        elif self.simple is not None and not has_string_format(text, self.simple):
            self.simple = None
        if self.time_values is not None:
            if len(text) <= _MAX_TIME_TEXT and _DIGIT.search(text):
                self.time_values[text] = None
            else:
                self.time_values = None

    def absorb(self, fmt: str | None, time_format: JsonValue) -> None:
        """Take ``format``/``x-time-format`` from a rendered schema (kept only while all agree)."""
        if self.resolved is None:
            self.resolved = (fmt, time_format)
        else:
            old_fmt, old_time = self.resolved
            self.resolved = (
                fmt if fmt == old_fmt else None,
                time_format if time_format == old_time else None,
            )

    def formats(self) -> tuple[str | None, JsonValue]:
        """``(format, x-time-format)`` shared by every string seen."""
        if self.resolved is not None:
            return self.resolved
        if self.time_values:
            found = infer_time_format(list(self.time_values))
            if found is not None and found.kind != "epoch":
                dumped: JsonValue = found.model_dump(mode="json", exclude_defaults=True)
                return _TIME_KIND_FORMATS.get(found.kind), dumped
        return self.simple, None


class _Scalar:
    """Samples of one scalar type at one position."""

    __slots__ = ("count", "distinct", "examples", "strings")

    def __init__(self, name: str, options: _Options) -> None:
        self.count = 0
        self.examples: _Seen = {}
        self.distinct: _Seen | None = {} if options.enum_threshold > 0 else None
        self.strings = _Strings() if name == "string" else None

    def add_example(self, key: _Key, seq: int, value: JsonScalar, limit: int) -> None:
        if len(self.examples) < limit and key not in self.examples:
            self.examples[key] = (seq, value)

    def add_distinct(self, key: _Key, seq: int, value: JsonScalar, limit: int) -> None:
        distinct = self.distinct
        if distinct is None or key in distinct:
            return
        if len(distinct) >= limit:
            self.distinct = None
        else:
            distinct[key] = (seq, value)

    def observe(self, key: _Key, seq: int, value: JsonScalar, options: _Options) -> None:
        self.count += 1
        self.add_example(key, seq, value, options.max_examples)
        self.add_distinct(key, seq, value, options.enum_threshold)
        if self.strings is not None and isinstance(value, str):
            self.strings.observe(value)


class _Object:
    """Object samples at one position: per-key child nodes and how often each key was present."""

    __slots__ = ("count", "present", "properties")

    def __init__(self) -> None:
        self.count = 0
        self.properties: dict[str, _Node] = {}
        self.present: dict[str, int] = {}

    def child(self, key: str) -> _Node:
        node = self.properties.get(key)
        if node is None:
            node = self.properties[key] = _Node()
            self.present[key] = 0
        return node


class _Array:
    """Array samples at one position; ``items`` merges every element of every sample."""

    __slots__ = ("count", "items")

    def __init__(self) -> None:
        self.count = 0
        self.items: _Node | None = None

    def items_node(self) -> _Node:
        if self.items is None:
            self.items = _Node()
        return self.items


class _Node:
    """Everything observed at one schema position, one part per non-null type (first-seen order)."""

    __slots__ = ("nulls", "parts")

    def __init__(self) -> None:
        self.nulls = 0
        self.parts: dict[str, _Scalar | _Object | _Array] = {}

    def scalar(self, name: str, options: _Options) -> _Scalar:
        part = self.parts.get(name)
        if not isinstance(part, _Scalar):
            part = self.parts[name] = _Scalar(name, options)
        return part

    def object(self) -> _Object:
        part = self.parts.get("object")
        if not isinstance(part, _Object):
            part = self.parts["object"] = _Object()
        return part

    def array(self) -> _Array:
        part = self.parts.get("array")
        if not isinstance(part, _Array):
            part = self.parts["array"] = _Array()
        return part

    def scalars(self, name: str) -> list[_Scalar]:
        """Scalar parts rendered as ``name`` (``number`` covers integers too)."""
        names = ("integer", "number") if name == "number" else (name,)
        return [part for key in names if isinstance(part := self.parts.get(key), _Scalar)]

    def type_names(self) -> list[str]:
        """Non-null type names in first-seen order, integers folded into numbers when both occur."""
        names = list(self.parts)
        if "integer" in names and "number" in names:
            first = min(names.index("integer"), names.index("number"))
            names = [name for name in names if name not in {"integer", "number"}]
            names.insert(first, "number")
        return names


class _Inference:
    """Single-pass accumulation of samples into a :class:`_Node` tree."""

    def __init__(self, options: _Options) -> None:
        self.options = options
        self.root = _Node()
        self.seq = 0

    def observe(self, value: JsonValue) -> None:
        stack: list[tuple[_Node, JsonValue]] = [(self.root, value)]
        while stack:
            node, item = stack.pop()
            if item is None:
                node.nulls += 1
            elif isinstance(item, dict):
                part = node.object()
                part.count += 1
                pending: list[tuple[_Node, JsonValue]] = []
                for key, child in item.items():
                    pending.append((part.child(key), child))
                    part.present[key] += 1
                stack.extend(reversed(pending))  # pop in document order
            elif isinstance(item, list):
                array = node.array()
                array.count += 1
                if item:
                    items = array.items_node()
                    stack.extend((items, element) for element in reversed(item))
            else:
                kind, scalar_key = _scalar_key(item)
                self.seq += 1
                node.scalar(kind, self.options).observe(scalar_key, self.seq, item, self.options)


class _Absorber:
    """Rebuilds an accumulator tree from rendered schemas (the inverse of rendering)."""

    def __init__(self, options: _Options) -> None:
        self.options = options
        self.root = _Node()
        self.seq = 0

    def absorb(self, schema: Mapping[str, JsonValue]) -> None:
        stack: list[tuple[_Node, object]] = [(self.root, schema)]
        while stack:
            node, item = stack.pop()
            if not isinstance(item, Mapping):
                continue
            branches = item.get("anyOf", item.get("oneOf"))
            if isinstance(branches, list):
                stack.extend((node, branch) for branch in reversed(branches))
                continue
            names = _schema_types(item)
            node.nulls += names.count("null")
            scalars = [name for name in names if name in _SCALAR_TYPES]
            if scalars:
                self._scalars(node, scalars, item)
            if "array" in names:
                array = node.array()
                array.count += 1
                items = item.get("items")
                if isinstance(items, Mapping):
                    stack.append((array.items_node(), items))
            if "object" in names:
                stack.extend(reversed(self._object(node.object(), item)))

    def _scalars(self, node: _Node, names: list[str], schema: Mapping[str, JsonValue]) -> None:
        enum = schema.get("enum")
        owned = (
            [found for value in enum if (found := _owned(value, names))]
            if isinstance(enum, list)
            else None
        )
        for name in names:
            part = node.scalar(name, self.options)
            if owned is None:
                part.distinct = None
                part.count += 1
                continue
            values = [(key, value) for owner, key, value in owned if owner == name]
            part.count += 2 * len(values)  # keeps "count >= 2 x distinct" true after merging
            for key, value in values:
                self.seq += 1
                part.add_distinct(key, self.seq, value, sys.maxsize)
        examples = schema.get("examples")
        for example in examples if isinstance(examples, list) else []:
            found = _owned(example, names)
            if found is not None:
                owner, key, value = found
                self.seq += 1
                node.scalar(owner, self.options).add_example(
                    key, self.seq, value, self.options.max_examples
                )
        strings = node.scalar("string", self.options).strings if "string" in names else None
        if strings is not None:
            fmt = schema.get("format")
            strings.absorb(fmt if isinstance(fmt, str) else None, schema.get("x-time-format"))

    @staticmethod
    def _object(part: _Object, schema: Mapping[str, JsonValue]) -> list[tuple[_Node, object]]:
        part.count += 1
        raw_required = schema.get("required")
        required = (
            [r for r in raw_required if isinstance(r, str)]
            if isinstance(raw_required, list)
            else []
        )
        properties = schema.get("properties")
        pending: list[tuple[_Node, object]] = []
        if isinstance(properties, Mapping):
            for key, sub in properties.items():
                pending.append((part.child(key), sub))
        for key in dict.fromkeys(required):
            part.child(key)
            part.present[key] += 1
        return pending


def _schema_types(schema: Mapping[str, JsonValue]) -> list[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [name for name in raw if isinstance(name, str)]
    return []


def _owned(value: JsonValue, names: list[str]) -> tuple[str, _Key, JsonScalar] | None:
    """Which of a schema's scalar types an ``enum``/``examples`` value belongs to, if any."""
    if value is None or isinstance(value, (dict, list)):
        return None
    kind, key = _scalar_key(value)
    if kind == "integer" and kind not in names:
        kind = "number"
    return (kind, key, value) if kind in names else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(root: _Node, options: _Options) -> dict[str, JsonValue]:
    """Turn an accumulator tree into a schema (iteratively: children fill placeholder dicts)."""
    schema: dict[str, JsonValue] = {}
    stack: list[tuple[_Node, dict[str, JsonValue]]] = [(root, schema)]
    while stack:
        node, target = stack.pop()
        stack.extend(_render_node(node, target, options))
    return schema


def _render_node(
    node: _Node, target: dict[str, JsonValue], options: _Options
) -> list[tuple[_Node, dict[str, JsonValue]]]:
    names = node.type_names()
    nullable = node.nulls > 0
    if not names:
        if nullable:
            target["type"] = "null"
        return []
    if len(names) == 1 or _CONTAINER_TYPES.isdisjoint(names):
        return _fill(target, node, names, options, nullable=nullable)
    children: list[tuple[_Node, dict[str, JsonValue]]] = []
    branches: list[JsonValue] = []
    for name in names:
        branch: dict[str, JsonValue] = {}
        children += _fill(branch, node, [name], options, nullable=False)
        branches.append(branch)
    if nullable:
        branches.append({"type": "null"})
    target["anyOf"] = branches
    return children


def _fill(
    target: dict[str, JsonValue],
    node: _Node,
    names: list[str],
    options: _Options,
    *,
    nullable: bool,
) -> list[tuple[_Node, dict[str, JsonValue]]]:
    """Write the keywords for ``names`` (one schema, possibly with a type list) into ``target``."""
    types: list[JsonValue] = [*names, "null"] if nullable else list(names)
    target["type"] = types[0] if len(types) == 1 else types
    children: list[tuple[_Node, dict[str, JsonValue]]] = []
    strings = node.scalars("string")[0].strings if "string" in names else None
    if strings is not None:
        fmt, time_format = strings.formats()
        if fmt is not None:
            target["format"] = fmt
        if time_format is not None:
            target["x-time-format"] = time_format
    if "object" in names:
        children += _object_keywords(target, node.object())
    if "array" in names:
        array = node.array()
        if array.items is not None:
            items: dict[str, JsonValue] = {}
            target["items"] = items
            children.append((array.items, items))
    scalars = [part for name in names if name in _SCALAR_TYPES for part in node.scalars(name)]
    if scalars:
        _scalar_keywords(target, scalars, options, nullable=nullable)
    return children


def _object_keywords(
    target: dict[str, JsonValue], part: _Object
) -> list[tuple[_Node, dict[str, JsonValue]]]:
    if not part.properties:
        return []
    properties: dict[str, JsonValue] = {}
    children: list[tuple[_Node, dict[str, JsonValue]]] = []
    for key, child in part.properties.items():
        sub: dict[str, JsonValue] = {}
        properties[key] = sub
        children.append((child, sub))
    target["properties"] = properties
    required: list[JsonValue] = [key for key, n in part.present.items() if n == part.count]
    if required:
        target["required"] = required
    return children


def _first_seen(maps: Iterable[_Seen]) -> list[JsonScalar]:
    """Values of several first-seen maps, deduplicated and in global first-seen order."""
    merged: _Seen = {}
    for seen in maps:
        for key, (seq, value) in seen.items():
            if key not in merged or seq < merged[key][0]:
                merged[key] = (seq, value)
    return [value for _, value in sorted(merged.values(), key=lambda entry: entry[0])]


def _scalar_keywords(
    target: dict[str, JsonValue], parts: list[_Scalar], options: _Options, *, nullable: bool
) -> None:
    if options.enum_threshold > 0:
        tracked = [part.distinct for part in parts if part.distinct is not None]
        if len(tracked) == len(parts):
            values = _first_seen(tracked)
            count = sum(part.count for part in parts)
            if values and len(values) <= options.enum_threshold and count >= 2 * len(values):
                target["enum"] = [*values, None] if nullable else list(values)
    examples = _first_seen(part.examples for part in parts)[: options.max_examples]
    if examples:
        target["examples"] = list(examples)


# ---------------------------------------------------------------------------
# Public API: inference and merging
# ---------------------------------------------------------------------------


def infer_schema(
    values: Iterable[JsonValue], *, enum_threshold: int = 0, max_examples: int = 3
) -> dict[str, JsonValue]:
    """Infer a JSON Schema (draft 2020-12 subset) that every value in ``values`` validates against.

    * ``null``/booleans/integers/floats/strings map to ``null``/``boolean``/``integer``/``number``/
      ``string``; integers and floats at the same position become ``number``.
    * Strings get a ``format`` when every one of them shares it (``date-time``, ``date``, ``time``,
      ``uuid``, ``email``, ``uri``, ``ipv4``, ``ipv6``) and an ``x-time-format`` (a dumped
      :class:`~fauxcall.timefmt.TimeFormat`) whenever one timestamp spelling fits them all: ISO,
      HTTP-date and strftime spellings (the last two without ``format``). Numeric strings are never
      taken for Unix times here; :mod:`fauxcall.analysis.profile` weighs that with the field name.
    * Objects list ``properties`` in first-seen order; ``required`` holds the keys present in every
      object sample. Arrays merge all elements into ``items`` (none if always empty).
    * Unions of scalars and ``null`` become a ``type`` list (first-seen order, ``null`` last);
      unions involving objects or arrays and another non-null type become ``anyOf``.
    * Leaf schemas carry up to ``max_examples`` distinct first-seen ``examples``. ``enum`` is added
      when ``enum_threshold > 0``, there are at most that many distinct non-null values, and at
      least twice as many samples as distinct values.

    No values at all give ``{}`` (which accepts anything).

    Raises:
        ValueError: ``enum_threshold`` or ``max_examples`` is negative.
        TypeError: a value is not JSON.
    """
    if enum_threshold < 0 or max_examples < 0:
        raise ValueError("enum_threshold and max_examples must not be negative")
    options = _Options(max_examples=max_examples, enum_threshold=enum_threshold)
    inference = _Inference(options)
    for value in values:
        inference.observe(value)
    return _render(inference.root, options)


def merge_schemas(
    a: Mapping[str, JsonValue], b: Mapping[str, JsonValue], *, max_examples: int = 3
) -> dict[str, JsonValue]:
    """The schema :func:`infer_schema` would give for both sample sets, as far as derivable.

    Properties and types are united (``a``'s order first, ``null`` last), ``required`` is the
    intersection, ``format``/``x-time-format`` survive only where both sides agree, ``enum`` only
    when both sides have one (as the union), and examples are merged and capped at
    ``max_examples``. Accepts what :func:`infer_schema` produces; other keywords are ignored.

    Not derivable, hence not reproduced: examples a side dropped when capping a type list
    (``["boolean", "integer"]``) that a merged ``anyOf`` would list per type, and an
    ``x-time-format`` that one spelling could cover for both sides.

    Raises:
        ValueError: ``max_examples`` is negative.
    """
    if max_examples < 0:
        raise ValueError("max_examples must not be negative")
    options = _Options(max_examples=max_examples, enum_threshold=sys.maxsize)
    absorber = _Absorber(options)
    absorber.absorb(a)
    absorber.absorb(b)
    return _render(absorber.root, options)


# ---------------------------------------------------------------------------
# Documented vs observed
# ---------------------------------------------------------------------------

DifferenceKind = Literal[
    "undocumented_field",
    "missing_field",
    "type_mismatch",
    "nullability",
    "required_mismatch",
    "format_mismatch",
]
"""What kind of disagreement a :class:`SchemaDifference` reports."""


class SchemaDifference(Model):
    """One way the observed data disagrees with the documented schema."""

    path: str = Field(description="JSON path of the field, e.g. '$.items[*].price'.")
    kind: DifferenceKind
    documented: RawJson = Field(default=None, description="What the documentation says.")
    observed: RawJson = Field(default=None, description="What the recordings show.")
    note: str = ""


type Resolver = Callable[[Mapping[str, JsonValue]], Mapping[str, JsonValue]]
"""Dereferences a schema node holding a ``$ref`` into the schema it points at."""

type _Fragment = JsonValue | Mapping[str, JsonValue]
"""A documented (sub)schema as written, possibly holding ``$ref``/``allOf``/``oneOf``/``anyOf``."""

type _Extra = list[_Fragment] | Literal["allow", "forbid"] | None
"""``additionalProperties``: schemas for extra keys, allowed, forbidden, or not specified."""

_DETECTED_FORMATS: Final = frozenset({*_TIME_KIND_FORMATS.values(), *STRING_FORMATS})
_MAX_VARIANTS: Final = 64


@dataclass(slots=True)
class _Doc:
    """One flattened alternative of a documented schema: no ``$ref``/``allOf``/``oneOf`` left."""

    anything: bool = False
    types: list[str] | None = None  # allowed non-null types; None: unconstrained
    nullable: bool = False
    fmt: str | None = None
    has_properties: bool = False
    properties: dict[str, list[_Fragment]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    additional: _Extra = None
    patterns: list[tuple[str, list[_Fragment]]] = field(default_factory=list)
    items: list[_Fragment] | None = None
    write_only: bool = False

    def allows(self, name: str) -> bool:
        if self.types is None:
            return True
        return name in self.types or (name == "integer" and "number" in self.types)

    def allows_null(self) -> bool:
        return self.types is None or self.nullable


def _intersect(a: list[str] | None, b: list[str] | None) -> list[str] | None:
    if a is None or b is None:
        return b if a is None else a
    numeric = {"integer", "number"}
    kept = [n if n in b else "integer" for n in a if n in b or (n in numeric and numeric & set(b))]
    return list(dict.fromkeys(kept))


def _conjoin_additional(a: _Extra, b: _Extra) -> _Extra:
    if isinstance(a, list) or isinstance(b, list):
        return [*(a if isinstance(a, list) else []), *(b if isinstance(b, list) else [])]
    if "forbid" in {a, b}:
        return "forbid"
    return "allow" if "allow" in {a, b} else None


def _conjoin(a: _Doc, b: _Doc) -> _Doc:
    """Both ``a`` and ``b`` apply (``allOf``). An unknown part makes the whole unknown."""
    if a.anything or b.anything:
        return _Doc(anything=True)
    properties = {key: list(fragments) for key, fragments in a.properties.items()}
    for key, fragments in b.properties.items():
        properties[key] = [*properties.get(key, []), *fragments]
    items = None if a.items is None and b.items is None else [*(a.items or []), *(b.items or [])]
    return _Doc(
        types=_intersect(a.types, b.types),
        nullable=a.nullable or b.nullable,
        fmt=a.fmt or b.fmt,
        has_properties=a.has_properties or b.has_properties,
        properties=properties,
        required=list(dict.fromkeys([*a.required, *b.required])),
        additional=_conjoin_additional(a.additional, b.additional),
        patterns=[*a.patterns, *b.patterns],
        items=items,
        write_only=a.write_only or b.write_only,
    )


def _product(left: list[_Doc], right: list[_Doc]) -> list[_Doc]:
    return [_conjoin(a, b) for a in left for b in right][:_MAX_VARIANTS]


def _additional(value: JsonValue) -> _Extra:
    if isinstance(value, bool):
        return "allow" if value else "forbid"
    if isinstance(value, Mapping):
        return [value] if value else "allow"
    return None


def _base_types(schema: Mapping[str, JsonValue], doc: _Doc) -> None:
    raw = schema.get("type")
    if isinstance(raw, (str, list)):
        names = _schema_types(schema)
        doc.types = [name for name in names if name != "null"]
        doc.nullable = "null" in names
    enum = schema.get("enum")
    enum_values = enum if isinstance(enum, list) else []
    if schema.get("nullable") is True or schema.get("x-nullable") is True or None in enum_values:
        doc.nullable = True
    if doc.types is None:  # OpenAPI often leaves the type implicit
        implied: list[str] = []
        if doc.has_properties or doc.required or doc.additional is not None or doc.patterns:
            implied.append("object")
        if doc.items is not None:
            implied.append("array")
        if not implied:
            implied = [t for v in enum_values if (t := _type_of(v)) not in {None, "null"}]
        doc.types = list(dict.fromkeys(implied)) or None


def _base(schema: Mapping[str, JsonValue]) -> _Doc:
    """The schema's own keywords (``$ref``/``allOf``/``oneOf``/``anyOf`` aside)."""
    fmt = schema.get("format")
    doc = _Doc(
        fmt=fmt if isinstance(fmt, str) else None, write_only=schema.get("writeOnly") is True
    )
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        doc.has_properties = True
        doc.properties = {str(key): [sub] for key, sub in properties.items()}
    required = schema.get("required")
    if isinstance(required, list):
        doc.required = [name for name in required if isinstance(name, str)]
    doc.additional = _additional(schema.get("additionalProperties"))
    patterns = schema.get("patternProperties")
    if isinstance(patterns, Mapping):
        doc.patterns = [(str(pattern), [sub]) for pattern, sub in patterns.items()]
    items = schema.get("items")
    if isinstance(items, (Mapping, bool)):
        doc.items = [items]
    _base_types(schema, doc)
    return doc


def _observed_pieces(observed: JsonValue) -> tuple[list[tuple[str, dict[str, JsonValue]]], bool]:
    """``([(type, schema that describes it), ...], nullable)`` of an inferred schema."""
    if not isinstance(observed, dict):
        return [], False
    branches = observed.get("anyOf")
    if isinstance(branches, list):
        pieces: list[tuple[str, dict[str, JsonValue]]] = []
        nullable = False
        for branch in branches:
            sub, sub_nullable = _observed_pieces(branch)
            pieces += sub
            nullable = nullable or sub_nullable
        return pieces, nullable
    names = _schema_types(observed)
    return [(name, observed) for name in names if name != "null"], "null" in names


def _as_json(value: object, ancestors: frozenset[int] = frozenset()) -> JsonValue:
    """A JSON copy of a documented fragment (YAML oddities stringified, cycles cut)."""
    if isinstance(value, (Mapping, list, tuple)):
        if id(value) in ancestors:
            return "(recursive)"
        inner = ancestors | {id(value)}
        if isinstance(value, Mapping):
            return {str(key): _as_json(item, inner) for key, item in value.items()}
        return [_as_json(item, inner) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _fragments_json(fragments: Sequence[_Fragment]) -> JsonValue:
    if len(fragments) <= 1:
        return _as_json(fragments[0]) if fragments else None
    return {"allOf": [_as_json(fragment) for fragment in fragments]}


def _types_json(docs: list[_Doc]) -> JsonValue:
    names = [name for doc in docs for name in doc.types or []]
    if any(doc.nullable for doc in docs):
        names.append("null")
    unique: list[JsonValue] = list(dict.fromkeys(names))
    return unique[0] if len(unique) == 1 else unique


def _pattern_matches(pattern: str, key: str) -> bool:
    try:
        return re.search(pattern, key) is not None
    except re.error:
        return False


class _Comparer:
    def __init__(self, resolve: Resolver | None) -> None:
        self.resolve = resolve

    # -- flattening documented schemas -----------------------------------------------------

    def all_of(self, fragments: Iterable[_Fragment], chain: tuple[object, ...] = ()) -> list[_Doc]:
        """Alternatives satisfying every fragment (``[_Doc()]``, unconstrained, for none)."""
        docs = [_Doc()]
        for fragment in fragments:
            docs = _product(docs, self.variants(fragment, chain))
        return docs

    def variants(self, schema: _Fragment, chain: tuple[object, ...]) -> list[_Doc]:
        """Flatten ``schema`` into alternatives (one per ``oneOf``/``anyOf`` choice)."""
        if schema is False:
            return [_Doc(types=[])]
        if not isinstance(schema, Mapping) or id(schema) in chain:
            return [_Doc(anything=True)]
        chain = (*chain, id(schema))
        ref = schema.get("$ref")
        if isinstance(ref, str):
            return self._ref_variants(schema, ref, chain)
        docs = [_base(schema)]
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            docs = _product(docs, self.all_of(all_of, chain))
        for keyword in ("oneOf", "anyOf"):
            alternatives = schema.get(keyword)
            if isinstance(alternatives, list) and alternatives:
                choices = [doc for alt in alternatives for doc in self.variants(alt, chain)]
                docs = _product(docs, choices)
        return docs

    def _ref_variants(
        self, schema: Mapping[str, JsonValue], ref: str, chain: tuple[object, ...]
    ) -> list[_Doc]:
        if self.resolve is None or ref in chain:
            return [_Doc(anything=True)]
        try:
            target = self.resolve(schema)
        except (LookupError, ValueError, TypeError):
            return [_Doc(anything=True)]
        docs = self.variants(target, (*chain, ref))
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        return _product(docs, self.variants(siblings, chain)) if siblings else docs

    # -- comparing -------------------------------------------------------------------------

    def compare(
        self, fragments: Sequence[_Fragment], observed: JsonValue, path: tuple[Segment, ...]
    ) -> list[SchemaDifference]:
        docs = self.all_of(fragments)
        if any(doc.anything for doc in docs):
            return []
        found: list[SchemaDifference] = []
        pieces, nullable = _observed_pieces(observed)
        for name, view in pieces:
            allowing = [doc for doc in docs if doc.allows(name)]
            if allowing:
                candidates = [self._piece(doc, name, view, path) for doc in allowing]
                best: list[SchemaDifference] = min(candidates, key=len)
                found += best
                continue
            documented = _types_json(docs)
            note = f"observed {name}, documented {documented}"
            found.append(_difference(path, "type_mismatch", documented, name, note))
        if nullable and not any(doc.allows_null() for doc in docs):
            documented = _types_json(docs)
            note = "observed null, but the documented schema is not nullable"
            found.append(_difference(path, "nullability", documented, "null", note))
        return found

    def _piece(
        self, doc: _Doc, name: str, view: dict[str, JsonValue], path: tuple[Segment, ...]
    ) -> list[SchemaDifference]:
        if name == "object":
            return self._object(doc, view, path)
        if name == "array":
            items = view.get("items")
            if doc.items is None or not isinstance(items, dict):
                return []
            return self.compare(doc.items, items, (*path, WILDCARD))
        if name == "string" and doc.fmt in _DETECTED_FORMATS:
            observed = view.get("format")
            if observed != doc.fmt:
                seen = observed if isinstance(observed, str) else "no recognizable format"
                note = f"documented format {doc.fmt}, observed {seen}"
                return [_difference(path, "format_mismatch", doc.fmt, observed, note)]
        return []

    def _object(
        self, doc: _Doc, view: dict[str, JsonValue], path: tuple[Segment, ...]
    ) -> list[SchemaDifference]:
        raw_properties = view.get("properties")
        observed = raw_properties if isinstance(raw_properties, dict) else {}
        raw_required = view.get("required")
        listed = raw_required if isinstance(raw_required, list) else []
        always = {key for key in listed if isinstance(key, str)}
        found: list[SchemaDifference] = []
        for name in dict.fromkeys([*doc.properties, *doc.required]):
            fragments = doc.properties.get(name, [])
            where = (*path, name)
            if name in observed:
                if name in doc.required and name not in always:
                    note = "documented as required, but missing from some observed objects"
                    found.append(
                        _difference(where, "required_mismatch", "required", "optional", note)
                    )
                found += self.compare(fragments, observed[name], where)
            elif name in doc.required and not any(d.write_only for d in self.all_of(fragments)):
                documented = _fragments_json(fragments)
                note = "documented as required, but never observed"
                found.append(_difference(where, "missing_field", documented, None, note))
        for name, sub in observed.items():
            if name not in doc.properties and name not in doc.required:
                found += self._extra(doc, name, sub, (*path, name))
        return found

    def _extra(
        self, doc: _Doc, name: str, observed: JsonValue, path: tuple[Segment, ...]
    ) -> list[SchemaDifference]:
        """An observed property the documentation does not list by name."""
        matching = [
            f for pattern, fs in doc.patterns if _pattern_matches(pattern, name) for f in fs
        ]
        if matching:
            return self.compare(matching, observed, path)
        if isinstance(doc.additional, list):
            return self.compare(doc.additional, observed, path)
        free_form = not doc.has_properties and doc.additional is None and not doc.patterns
        if doc.additional == "allow" or free_form:
            return []
        note = "observed, but not documented"
        if doc.additional == "forbid":
            note += " (additionalProperties: false)"
        return [_difference(path, "undocumented_field", None, observed, note)]


def _difference(
    path: tuple[Segment, ...],
    kind: DifferenceKind,
    documented: JsonValue,
    observed: JsonValue,
    note: str,
) -> SchemaDifference:
    return SchemaDifference(
        path=format_path(path), kind=kind, documented=documented, observed=observed, note=note
    )


def compare_schemas(
    documented: Mapping[str, JsonValue],
    observed: Mapping[str, JsonValue],
    *,
    resolve: Resolver | None = None,
) -> list[SchemaDifference]:
    """List where an observed (inferred) schema disagrees with a documented one.

    ``documented`` is OpenAPI-flavored: ``$ref`` (dereferenced through ``resolve``; without it
    a reference accepts anything), ``allOf`` (all apply), ``oneOf``/``anyOf`` (the best-fitting
    alternative is used, per observed type), ``nullable: true`` (OpenAPI 3.0), ``x-nullable``
    (Swagger 2), type lists with ``"null"`` (3.1), ``additionalProperties`` and
    ``patternProperties``. Types are implied by ``properties``/``items``/``enum`` when missing.

    Reported, in document order (undocumented fields after the documented ones):

    * ``undocumented_field``: observed, but not documented (free-form objects without
      ``properties`` and objects with ``additionalProperties: true`` are exempt);
    * ``missing_field``: documented as required (and not ``writeOnly``) but never observed;
      optional fields that never appear are normal and not reported;
    * ``required_mismatch``: documented as required, but absent from some observed objects;
    * ``type_mismatch``: an observed type the documentation does not allow (an integer where a
      number is documented is fine; a number where an integer is documented is not);
    * ``nullability``: ``null`` observed where the documentation does not allow it;
    * ``format_mismatch``: a documented ``format`` FauxCall detects (date-time, date, time, uuid,
      email, uri, ipv4, ipv6) that the observed strings do not share.
    """
    return _Comparer(resolve).compare([dict(documented)], dict(observed), ())
