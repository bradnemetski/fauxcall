"""The inputs a request is matched on: path parameters, query values, JSON leaves, tool arguments.

Keys are ``path.<name>``, ``query.<name>``, ``body.<json path>``, ``args.<json path>`` and
``params.<json path>`` (other MCP methods). Nothing is lost on the way: a query parameter given
several times maps to the list of its values, and leaves under arrays (paths containing ``[*]``)
map to the list of every element's value, in order — ``{"symbols": ["AAPL", "MSFT"]}`` becomes
``{"args.symbols[*]": ["AAPL", "MSFT"]}``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fauxcall import jsonpath

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from fauxcall.types import JsonValue


def json_inputs(prefix: str, value: JsonValue) -> dict[str, JsonValue]:
    """Leaves of a JSON object keyed ``<prefix>.<path>`` (nothing for non-objects)."""
    flat: dict[str, JsonValue] = {}
    if not isinstance(value, dict):
        return flat
    collected: dict[str, list[JsonValue]] = {}
    for path, leaf in jsonpath.leaf_paths(value):
        key = prefix + path.removeprefix("$")
        if jsonpath.WILDCARD in jsonpath.parse(path):
            collected.setdefault(key, []).append(leaf)
            flat.setdefault(key, None)  # keep first-seen key order
        else:
            flat[key] = leaf
    for key, values in collected.items():
        flat[key] = list(values)
    return flat


def query_inputs(pairs: Iterable[tuple[str, str]]) -> dict[str, JsonValue]:
    """``query.<name>`` -> the value, or the list of values for repeated parameters."""
    grouped: dict[str, list[JsonValue]] = {}
    for name, value in pairs:
        grouped.setdefault(name, []).append(value)
    return {f"query.{name}": vals[0] if len(vals) == 1 else vals for name, vals in grouped.items()}


_TEMPLATE_PARAM: Final = re.compile(r"\{([^/{}]+)\}")


def path_inputs(params: Mapping[str, object]) -> dict[str, JsonValue]:
    """``path.<name>`` -> the raw path parameter as text."""
    return {f"path.{name}": str(value) for name, value in params.items()}


def template_names(path: str) -> list[str]:
    """The parameter names of a path template, as the spec spells them (``pet-id``)."""
    return _TEMPLATE_PARAM.findall(path)
