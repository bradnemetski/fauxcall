"""A deliberately small JSON path dialect for addressing fields in records and bodies.

Syntax: ``$`` (the root, optional), ``.name`` or ``name``, ``["odd key"]``, ``[0]``, ``[-1]`` and
``[*]`` (every element of a list / every value of an object). Examples::

    dueDate
    $.data[*].attributes.time
    rates.EUR
    items[0]["unit price"]

Missing paths are not errors: :func:`get_all` returns nothing and :func:`set_all` changes nothing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fauxcall.types import JsonValue

WILDCARD: Final = object()
_TOKEN_RE: Final = re.compile(
    r"""
      \.?(?P<name>[A-Za-z_@$][\w@$-]*)        # .name or leading name
    | \[(?P<index>-?\d+)\]                    # [0]
    | \[(?P<star>\*)\]                        # [*]
    | \.(?P<dotstar>\*)                       # .*
    | \[(?P<quote>["'])(?P<key>(?:\\.|(?!(?P=quote)).)*)(?P=quote)\]  # ["key"]
    """,
    re.VERBOSE,
)

type Segment = str | int | object
"""A parsed path segment: a key, a list index, or :data:`WILDCARD`."""


def parse(path: str) -> tuple[Segment, ...]:
    """Parse a path into segments. ``""`` and ``"$"`` address the root."""
    text = path.strip()
    text = text.removeprefix("$")
    segments: list[Segment] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            raise ValueError(f"invalid JSON path {path!r} at position {position + 1}")
        if match.group("name") is not None:
            segments.append(match.group("name"))
        elif match.group("index") is not None:
            segments.append(int(match.group("index")))
        elif match.group("star") or match.group("dotstar"):
            segments.append(WILDCARD)
        else:
            segments.append(re.sub(r"\\(.)", r"\1", match.group("key")))
        position = match.end()
    return tuple(segments)


def format_path(segments: tuple[Segment, ...]) -> str:
    """Render segments back into a canonical path string (``$`` for the root)."""
    parts = ["$"]
    for segment in segments:
        if segment is WILDCARD:
            parts.append("[*]")
        elif isinstance(segment, int):
            parts.append(f"[{segment}]")
        elif isinstance(segment, str) and re.fullmatch(r"[A-Za-z_][\w-]*", segment):
            parts.append(f".{segment}")
        else:
            escaped = str(segment).replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'["{escaped}"]')
    return "".join(parts)


def _children(value: JsonValue, segment: Segment) -> Iterator[tuple[Segment, JsonValue]]:
    if segment is WILDCARD:
        if isinstance(value, list):
            yield from enumerate(value)
        elif isinstance(value, dict):
            yield from value.items()
    elif isinstance(segment, int):
        if isinstance(value, list) and -len(value) <= segment < len(value):
            yield segment, value[segment]
    elif isinstance(value, dict) and isinstance(segment, str) and segment in value:
        yield segment, value[segment]


def iter_matches(
    value: JsonValue, path: str | tuple[Segment, ...]
) -> Iterator[tuple[tuple[Segment, ...], JsonValue]]:
    """Yield ``(concrete_segments, matched_value)`` for every match of ``path``."""
    segments = parse(path) if isinstance(path, str) else path

    def walk(
        node: JsonValue, rest: tuple[Segment, ...], trail: tuple[Segment, ...]
    ) -> Iterator[tuple[tuple[Segment, ...], JsonValue]]:
        if not rest:
            yield trail, node
            return
        for key, child in _children(node, rest[0]):
            yield from walk(child, rest[1:], (*trail, key))

    yield from walk(value, segments, ())


def get_all(value: JsonValue, path: str) -> list[JsonValue]:
    """Every value matched by ``path`` (empty when nothing matches)."""
    return [match for _, match in iter_matches(value, path)]


def get_one(value: JsonValue, path: str, default: JsonValue = None) -> JsonValue:
    """The first value matched by ``path``, or ``default``."""
    for _, match in iter_matches(value, path):
        return match
    return default


def set_all(value: JsonValue, path: str, update: Callable[[JsonValue], JsonValue]) -> int:
    """Replace every match of ``path`` in place with ``update(old)``. Returns the match count.

    The root itself cannot be replaced in place; use the return value of ``update`` instead.
    """
    segments = parse(path)
    if not segments:
        raise ValueError("cannot replace the root in place")
    count = 0
    for trail, old in list(iter_matches(value, segments)):
        parent = value
        for key in trail[:-1]:
            parent = parent[key]  # type: ignore[index]
        parent[trail[-1]] = update(old)  # type: ignore[index]
        count += 1
    return count


def leaf_paths(
    value: JsonValue, *, generalize_lists: bool = True
) -> Iterator[tuple[str, JsonValue]]:
    """Yield ``(path, leaf)`` for every scalar in ``value``.

    With ``generalize_lists`` list indices become ``[*]`` so many observations of the same field
    share one path (``items[*].price``).
    """

    def walk(node: JsonValue, trail: tuple[Segment, ...]) -> Iterator[tuple[str, JsonValue]]:
        if isinstance(node, dict):
            for key, child in node.items():
                yield from walk(child, (*trail, key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from walk(child, (*trail, WILDCARD if generalize_lists else index))
        else:
            yield format_path(trail), node

    yield from walk(value, ())
