"""``{{placeholder}}`` rendering for error templates, contract headers and message bodies.

A JSON string that consists of exactly one placeholder is replaced by the placeholder's *value*
(which may be a number, boolean, object …); placeholders embedded in longer strings are rendered as
text in JSON spelling (``true``, ``null``, ``1.50``, ``{"a":1}``). Unknown placeholders are left
untouched so templates never lose information silently.

Syntax: ``{{name}}`` or ``{{name:arg}}``; whitespace inside the braces is ignored
(``{{ now:iso_ms }}``). Names start with a letter or ``_`` and may contain ``.`` and ``-``
(``{{header.x-request-id}}``).
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Final, Protocol

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

_PLACEHOLDER: Final = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)(?::([^}]*?))?\s*\}\}")


class _Unresolved:
    """Marker returned by resolvers that do not know a placeholder."""

    def __repr__(self) -> str:
        return "UNRESOLVED"


UNRESOLVED: Final = _Unresolved()


class Resolver(Protocol):
    """Resolves a placeholder ``name`` (with optional ``arg`` after ``:``) to a value."""

    def __call__(self, name: str, arg: str | None) -> JsonValue | _Unresolved:
        """The placeholder's value, or :data:`UNRESOLVED` when this resolver doesn't know it."""


def as_text(value: JsonValue) -> str:
    """A value as text: strings verbatim, anything else in JSON spelling (``true``, ``1.50``)."""
    if isinstance(value, str):
        return value
    try:
        return jsonfmt.dumps(value)
    except (TypeError, ValueError):  # NaN/inf, or a non-JSON object returned by custom code
        return str(value)


def render_string(text: str, resolve: Resolver) -> JsonValue:
    """Render one string; a lone placeholder may become a non-string value."""
    whole = _PLACEHOLDER.fullmatch(text)
    if whole:
        value = resolve(whole.group(1), whole.group(2))
        if isinstance(value, _Unresolved):
            return text
        # A fresh copy, so callers may mutate the result without touching the resolver's data.
        return copy.deepcopy(value) if isinstance(value, dict | list) else value

    def substitute(match: re.Match[str]) -> str:
        value = resolve(match.group(1), match.group(2))
        return match.group(0) if isinstance(value, _Unresolved) else as_text(value)

    return _PLACEHOLDER.sub(substitute, text)


def render_text(text: str, resolve: Resolver) -> str:
    """Render a template that must produce text (header values, text bodies, object keys).

    Unlike ``str(render_string(...))``, a lone placeholder with a non-string value is spelled as
    JSON: ``{{flag}}`` gives ``true`` (not ``True``) and a ``None`` value gives ``null``.
    """
    return as_text(render_string(text, resolve))


def render(template: JsonValue, resolve: Resolver) -> JsonValue:
    """Render every string (and object key) in a JSON template into a new value.

    The template itself is never modified.
    """
    if isinstance(template, str):
        return render_string(template, resolve)
    if isinstance(template, list):
        return [render(item, resolve) for item in template]
    if isinstance(template, dict):
        return {
            render_text(key, resolve): render(value, resolve) for key, value in template.items()
        }
    return template  # scalars are immutable


def placeholders(template: JsonValue) -> list[str]:
    """Names of every placeholder used in a template (first-seen order, no duplicates)."""
    found: list[str] = []

    def walk(node: JsonValue) -> None:
        if isinstance(node, str):
            for match in _PLACEHOLDER.finditer(node):
                if match.group(1) not in found:
                    found.append(match.group(1))
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(key)
                walk(value)

    walk(template)
    return found


def chain(*resolvers: Resolver) -> Resolver:
    """Combine resolvers: the first one that knows a placeholder wins."""

    def resolve(name: str, arg: str | None) -> JsonValue | _Unresolved:
        for resolver in resolvers:
            value = resolver(name, arg)
            if not isinstance(value, _Unresolved):
                return value
        return UNRESOLVED

    return resolve


def from_mapping(values: dict[str, JsonValue]) -> Resolver:
    """A resolver backed by a plain mapping (``{{id}}`` -> ``values["id"]``; the arg is ignored)."""

    def resolve(name: str, arg: str | None) -> JsonValue | _Unresolved:  # noqa: ARG001
        return values.get(name, UNRESOLVED)

    return resolve
