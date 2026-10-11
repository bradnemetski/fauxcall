"""Profile every field of recorded documents so an agent can choose field policies.

A :class:`FieldProfile` describes one generalized path (``$.data[*].price``): how often it occurs,
which JSON types and how many distinct values it takes, and whether it looks like a timestamp (with
its exact spelling), an identifier, a numeric string or a constant. That is the evidence behind
field policies: static, time-tracking, IDs, enums, volatile numbers and so on.
:func:`profile_table` renders profiles as a compact markdown table for an AI agent.

Paths are spelled exactly like :func:`fauxcall.jsonpath.leaf_paths` spells them. Empty arrays and
objects get a path of their own so their presence stays visible.
"""

from __future__ import annotations

import functools
import math
import re
from typing import TYPE_CHECKING, Final

from pydantic import Field

from fauxcall import jsonfmt
from fauxcall.analysis.schema import has_string_format, json_type, string_format
from fauxcall.jsonfmt import JsonFloat
from fauxcall.jsonpath import format_path
from fauxcall.model.common import Model, RawJson
from fauxcall.timefmt import TimeFormat, infer_time_format

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from fauxcall.types import JsonValue


class FieldProfile(Model):
    """How one field (a generalized JSON path) behaves across a set of documents."""

    path: str = Field(description="Generalized JSON path; list elements are '[*]'.")
    documents: int = Field(default=0, description="Input documents containing the path.")
    occurrences: int = Field(default=0, description="Values at the path (lists add several).")
    nulls: int = 0
    types: dict[str, int] = Field(default_factory=dict, description="Occurrences per JSON type.")
    distinct: int = Field(default=0, description="Distinct non-null values (JSON equality).")
    examples: list[RawJson] = Field(
        default_factory=list, description="The first distinct non-null values, as spelled."
    )
    constant: bool = Field(
        default=False, description="Exactly one distinct value over at least 2 non-null values."
    )
    time_format: TimeFormat | None = Field(
        default=None, description="A timestamp spelling every non-null value fits."
    )
    string_format: str | None = Field(
        default=None, description="uuid, email, uri, ipv4 or ipv6, shared by every string."
    )
    minimum: float | None = Field(default=None, description="Numbers (and numeric strings).")
    maximum: float | None = None
    min_length: int | None = Field(default=None, description="Strings, in characters.")
    max_length: int | None = None
    decimals: int | None = Field(
        default=None,
        description="Most digits after the decimal point (floats and numeric strings only).",
    )
    numeric_string: bool = Field(
        default=False, description="Every string spells a decimal number, e.g. '82940.02'."
    )
    looks_like_id: bool = False
    cardinality: float = Field(default=0.0, description="distinct / occurrences.")


_NUMERIC_STRING: Final = re.compile(r"-?(?:0|[1-9]\d*)(?:\.(\d+))?")
_ID_NAME: Final = re.compile(
    r"(?i:id|ids|uuid|guid|key|code|symbol)"  # the whole key
    r"|.*(?i:[_.-](?:id|ids|uuid|guid))"  # snake_case / kebab-case suffix
    r"|.*[a-z0-9](?:Id|ID|Ids|IDs|Uuid|UUID)"  # camelCase suffix
)
_EXPONENT: Final = re.compile(r"[eE]")
_MIN_UNIQUE_INTEGERS: Final = 2

type _Key = tuple[str, bool | int | float | str]


def looks_like_id_name(key: str | None) -> bool:
    """Name heuristic: ``id``, ``*_id``, ``*Id``, ``*ID``, ``uuid``, ``key``, ``code``, ``symbol``.

    Plural forms (``ids``, ``tag_ids``, ``tagIds``) and ``guid`` count too.
    """
    return key is not None and _ID_NAME.fullmatch(key) is not None


def _decimals(value: float) -> int:
    text = value.lexeme if isinstance(value, JsonFloat) else float.__repr__(value)
    mantissa = _EXPONENT.split(text, maxsplit=1)[0]
    return len(mantissa.partition(".")[2])


def _as_float(value: float) -> float:
    try:
        return float(value)
    except OverflowError:  # integers beyond the float range
        return math.inf if value > 0 else -math.inf


def _time_format(values: list[JsonValue], *, named_id: bool) -> TimeFormat | None:
    if not values:
        return None
    found = infer_time_format(values)  # huge integers are handled (no OverflowError)
    if found is not None and found.kind == "epoch" and named_id:
        return None  # trade_id=1104620943 is not a Unix time
    return found


class _Field:
    """Single-pass accumulator for one path."""

    __slots__ = (
        "common_format",
        "distinct",
        "documents",
        "floats",
        "high",
        "integers",
        "last_document",
        "low",
        "max_length",
        "min_length",
        "name",
        "nulls",
        "number_decimals",
        "numeric_high",
        "numeric_low",
        "numeric_strings",
        "occurrences",
        "string_decimals",
        "strings",
        "types",
    )

    def __init__(self, name: str | None) -> None:
        self.name = name
        self.documents = 0
        self.last_document = -1
        self.occurrences = 0
        self.nulls = 0
        self.types: dict[str, int] = {}
        self.distinct: dict[_Key, JsonValue] = {}
        self.integers = 0
        self.floats = 0
        self.number_decimals = 0
        self.low: float | None = None
        self.high: float | None = None
        self.strings = 0
        self.min_length: int | None = None
        self.max_length: int | None = None
        self.numeric_strings = True
        self.string_decimals = 0
        self.numeric_low: float | None = None
        self.numeric_high: float | None = None
        self.common_format: str | None = None

    def observe(self, value: JsonValue, document: int) -> None:
        if document != self.last_document:
            self.documents += 1
            self.last_document = document
        self.occurrences += 1
        kind = json_type(value)
        self.types[kind] = self.types.get(kind, 0) + 1
        if value is None:
            self.nulls += 1
            return
        if isinstance(value, (dict, list)):
            key: _Key = (kind, jsonfmt.canonical_dumps(value))
        else:
            key = ("number" if kind == "integer" else kind, value)
        self.distinct.setdefault(key, value)
        if isinstance(value, str):
            self._string(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self._number(value)

    def _number(self, value: float) -> None:
        if isinstance(value, int):
            self.integers += 1
        else:
            self.floats += 1
            self.number_decimals = max(self.number_decimals, _decimals(value))
        self.low = value if self.low is None else min(self.low, value)
        self.high = value if self.high is None else max(self.high, value)

    def _string(self, text: str) -> None:
        self.strings += 1
        length = len(text)
        self.min_length = length if self.min_length is None else min(self.min_length, length)
        self.max_length = length if self.max_length is None else max(self.max_length, length)
        if self.numeric_strings:
            match = _NUMERIC_STRING.fullmatch(text)
            if match is None:
                self.numeric_strings = False
            else:
                self.string_decimals = max(self.string_decimals, len(match.group(1) or ""))
                number = float(text)
                low, high = self.numeric_low, self.numeric_high
                self.numeric_low = number if low is None else min(low, number)
                self.numeric_high = number if high is None else max(high, number)
        if self.strings == 1:
            self.common_format = string_format(text)
        elif self.common_format is not None and not has_string_format(text, self.common_format):
            self.common_format = None

    def _bounds(self, *, numeric_string: bool) -> tuple[float | None, float | None]:
        lows = [
            x for x in (self.low, self.numeric_low if numeric_string else None) if x is not None
        ]
        highs = [
            x for x in (self.high, self.numeric_high if numeric_string else None) if x is not None
        ]
        if not lows or not highs:
            return None, None
        return _as_float(min(lows)), _as_float(max(highs))

    def profile(self, path: str, max_examples: int) -> FieldProfile:
        values = list(self.distinct.values())
        non_null = self.occurrences - self.nulls
        named_id = looks_like_id_name(self.name)
        time_format = _time_format(values, named_id=named_id)
        numeric_string = self.strings > 0 and self.numeric_strings
        places = [self.number_decimals] if self.floats else []
        if numeric_string:
            places.append(self.string_decimals)
        common_format = self.common_format if self.strings else None
        unique_integers = (
            self.integers == non_null >= _MIN_UNIQUE_INTEGERS
            and len(values) == non_null
            and time_format is None
        )
        minimum, maximum = self._bounds(numeric_string=numeric_string)
        return FieldProfile(
            path=path,
            documents=self.documents,
            occurrences=self.occurrences,
            nulls=self.nulls,
            types=dict(self.types),
            distinct=len(values),
            examples=values[:max_examples],
            constant=len(values) == 1 and non_null >= _MIN_UNIQUE_INTEGERS,
            time_format=time_format,
            string_format=common_format,
            minimum=minimum,
            maximum=maximum,
            min_length=self.min_length,
            max_length=self.max_length,
            decimals=max(places) if places else None,
            numeric_string=numeric_string,
            looks_like_id=named_id or common_format == "uuid" or unique_integers,
            cardinality=len(values) / self.occurrences if self.occurrences else 0.0,
        )


@functools.lru_cache(maxsize=4096)
def _segment(key: str) -> str:
    """One object key spelled as :func:`fauxcall.jsonpath.format_path` spells it."""
    return format_path((key,))[1:]


def _walk(document: JsonValue) -> Iterator[tuple[str, str | None, JsonValue]]:
    """Yield ``(generalized path, last object key, value)`` for leaves and empty containers."""
    stack: list[tuple[str, str | None, JsonValue]] = [("$", None, document)]
    while stack:
        path, name, node = stack.pop()
        if isinstance(node, dict) and node:
            stack.extend(
                (path + _segment(key), key, child) for key, child in reversed(node.items())
            )
        elif isinstance(node, list) and node:
            inner = path + "[*]"
            stack.extend((inner, name, child) for child in reversed(node))
        else:
            yield path, name, node


def profile_documents(
    documents: Sequence[JsonValue], *, max_examples: int = 5
) -> list[FieldProfile]:
    """Profile every leaf path (plus empty arrays/objects) of ``documents``, in first-seen order.

    List indices are generalized to ``[*]``. ``distinct``, ``examples`` and ``constant`` use JSON
    equality over non-null values (``1`` equals ``1.0``; ``true`` does not equal ``1``).
    ``time_format`` comes from :func:`fauxcall.timefmt.infer_time_format` over every non-null value
    (epoch guesses are dropped for fields whose name looks like an identifier). ``looks_like_id``
    combines name heuristics, uuid strings and integers that never repeat (at least two).

    Raises:
        ValueError: ``max_examples`` is negative.
        TypeError: a document is not JSON.
    """
    if max_examples < 0:
        raise ValueError("max_examples must not be negative")
    fields: dict[str, _Field] = {}
    for index, document in enumerate(documents):
        for path, name, value in _walk(document):
            accumulator = fields.get(path)
            if accumulator is None:
                accumulator = fields[path] = _Field(name)
            accumulator.observe(value, index)
    return [accumulator.profile(path, max_examples) for path, accumulator in fields.items()]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_EXAMPLES_WIDTH: Final = 40
_FRACTION_MODES: Final[dict[str, str]] = {"fixed": "", "trim": " trim", "omit_if_zero": " omit0"}
_ALMOST_ALL: Final = 99


def time_format_summary(fmt: TimeFormat) -> str:
    """A short spelling summary: ``iso 7f Z``, ``iso 6f +HH:MM``, ``date``, ``epoch ms`` etc."""
    if fmt.kind == "epoch":
        parts = ["epoch", fmt.epoch_unit]
        if fmt.epoch_decimals:
            parts.append(f"{fmt.epoch_decimals}dp")
        if fmt.as_string:
            parts.append("string")
        return " ".join(parts)
    if fmt.kind == "strftime":
        return f"strftime {fmt.pattern}"
    if fmt.kind in {"date", "http_date"}:
        return fmt.kind.replace("_", "-")
    parts = [fmt.kind]
    if fmt.kind == "iso" and fmt.separator == " ":
        parts.append("space")
    if not fmt.seconds:
        parts.append("no-seconds")
    if fmt.fraction_digits:
        parts.append(f"{fmt.fraction_digits}f{_FRACTION_MODES[fmt.fraction_mode]}")
    if fmt.kind == "iso":
        parts.append("naive" if fmt.tz_style == "none" else fmt.tz_style)
    return " ".join(parts)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _types_cell(profile: FieldProfile) -> str:
    if len(profile.types) == 1:
        return next(iter(profile.types))
    return ", ".join(f"{name} {count}" for name, count in profile.types.items())


def _nulls_cell(profile: FieldProfile) -> str:
    if not profile.nulls or not profile.occurrences:
        return "0%"
    if profile.nulls == profile.occurrences:
        return "100%"
    percent = 100 * profile.nulls / profile.occurrences
    if percent < 1:
        return "<1%"
    return ">99%" if percent > _ALMOST_ALL else f"{percent:.0f}%"


def _examples_cell(profile: FieldProfile) -> str:
    cell = ""
    for example in profile.examples:
        text = jsonfmt.dumps(example)
        joined = f"{cell}, {text}" if cell else text
        if len(joined) > _EXAMPLES_WIDTH:
            return f"{cell}, …" if cell else text[: _EXAMPLES_WIDTH - 1] + "…"
        cell = joined
    return cell


def _number(value: float) -> str:
    return f"{value:.15g}"


def _notes_cell(profile: FieldProfile, total_documents: int) -> str:
    notes: list[str] = []
    if profile.time_format is not None:
        notes.append(time_format_summary(profile.time_format))
    if profile.string_format is not None:
        notes.append(profile.string_format)
    if profile.numeric_string:
        notes.append(f"numeric-string {profile.decimals}dp")
    elif profile.decimals is not None:
        notes.append(f"{profile.decimals}dp")
    low, high = profile.minimum, profile.maximum
    if low is not None and high is not None and low != high:
        notes.append(f"range {_number(low)}..{_number(high)}")
    if profile.looks_like_id:
        notes.append("id?")
    if profile.constant:
        notes.append("constant")
    if profile.documents < total_documents:
        notes.append(f"in {profile.documents}/{total_documents} docs")
    return ", ".join(notes)


def profile_table(profiles: Sequence[FieldProfile], *, max_rows: int = 200) -> str:
    """Render profiles as a compact markdown table for an AI agent choosing field policies.

    Columns: path | types | null% | distinct | examples (about 40 characters) | notes. Notes
    summarize the evidence: the timestamp spelling (``iso 7f Z``), string format,
    ``numeric-string 2dp`` / ``4dp``, the numeric range, ``id?``, ``constant`` and
    ``in 3/5 docs`` for fields missing from some documents. Rows beyond ``max_rows`` are counted
    in a final line instead of being shown.

    Raises:
        ValueError: ``max_rows`` is negative.
    """
    if max_rows < 0:
        raise ValueError("max_rows must not be negative")
    if not profiles:
        return "(no fields)"
    total_documents = max(profile.documents for profile in profiles)
    lines = [
        "| path | types | null% | distinct | examples | notes |",
        "|---|---|---|---|---|---|",
    ]
    for profile in profiles[:max_rows]:
        cells = [
            profile.path,
            _types_cell(profile),
            _nulls_cell(profile),
            str(profile.distinct),
            _examples_cell(profile),
            _notes_cell(profile, total_documents),
        ]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    hidden = len(profiles) - max_rows
    if hidden > 0:
        noun = "field" if hidden == 1 else "fields"
        lines.append(f"\n({hidden} more {noun} not shown; max_rows={max_rows})")
    return "\n".join(lines)
