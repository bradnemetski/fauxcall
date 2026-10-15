"""Find cross-endpoint references by value overlap.

Example: ``tickers[*].exchange`` ⊆ ``exchanges[*].code``.

Consistency across endpoints is the most important property of a fake. This module proposes
candidate relations with evidence; the agent confirms them (domain knowledge beats statistics) and
records them as :class:`~fauxcall.model.world.RelationModel`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Final

from pydantic import Field

from fauxcall import jsonfmt, jsonpath
from fauxcall.model.common import Model, RawJson

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fauxcall.types import JsonValue

MIN_COVERAGE: Final = 0.6
MIN_DISTINCT: Final = 2
MAX_KEY_LENGTH: Final = 80
MAX_MISSING: Final = 5
_MIN_PARTIAL_NAME: Final = 3
_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_ALNUM: Final = re.compile(r"[^0-9a-z]+")
_ID_SUFFIX: Final = re.compile(r"_ids?$")
_BARE_ID_SUFFIX: Final = re.compile(r"(?<=[a-z0-9])ids?$")
_HTTP_METHOD: Final = re.compile(r"^[A-Z]+\s+")


class RelationCandidate(Model):
    """A possible reference from one field to another group's identifying field."""

    from_group: str
    from_field: str
    to_group: str
    to_field: str
    coverage: float = Field(description="Share of distinct referencing values found in the target.")
    distinct_values: int
    missing: list[RawJson] = Field(default_factory=list, description="Up to 5 unresolved values.")
    name_hint: bool = Field(description="The field name mentions the target (e.g. 'exchange').")
    reverse_coverage: float = Field(
        default=0.0,
        description=(
            "Share of the target's distinct keys that some record references: evidence for "
            "min_per_target (1.0: every recorded exchange has a ticker)."
        ),
    )
    unreferenced: list[RawJson] = Field(
        default_factory=list, description="Up to 5 target keys nothing references."
    )


def _key_text(value: JsonValue) -> str | None:
    """The comparable text of a leaf that could be a key or a reference (``None`` if it can't)."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return text if 0 < len(text) <= MAX_KEY_LENGTH else None


def _leaf_texts(document: JsonValue) -> dict[str, list[str]]:
    """Every usable leaf of one document, by generalized path (repetitions kept)."""
    found: defaultdict[str, list[str]] = defaultdict(list)
    for path, value in jsonpath.leaf_paths(document):
        text = _key_text(value)
        if text is not None:
            found[path].append(text)
    return found


def _scalar_values(documents: Sequence[JsonValue]) -> dict[str, set[str]]:
    values: defaultdict[str, set[str]] = defaultdict(set)
    for document in documents:
        for path, texts in _leaf_texts(document).items():
            values[path].update(texts)
    return values


def _identifying(
    values: Mapping[str, set[str]], documents: Sequence[JsonValue]
) -> dict[str, set[str]]:
    """Fields that look like keys: distinct across items (no value repeated within a sample).

    A field that occurs several times in one document (``$.data[*].code`` in a listing) is a key
    when no value repeats inside any single document, so the same listing recorded twice still
    counts. A field that occurs at most once per document (individual records, detail responses)
    treats the whole group as the sample: no value may repeat across documents, except that
    identical documents (the same record recorded twice) count once.
    """
    per_document = [_leaf_texts(document) for document in documents]
    listings = {path for found in per_document for path, texts in found.items() if len(texts) > 1}
    repeated = {
        path
        for found in per_document
        for path, texts in found.items()
        if path in listings and len(set(texts)) < len(texts)
    }
    unique: dict[str, dict[str, list[str]]] = {}
    for document, found in zip(documents, per_document, strict=True):
        unique.setdefault(jsonfmt.canonical_dumps(document), found)
    across: Counter[tuple[str, str]] = Counter(
        (path, text)
        for found in unique.values()
        for path, texts in found.items()
        if path not in listings
        for text in texts
    )
    repeated |= {path for (path, _), count in across.items() if count > 1}
    return {
        path: distinct
        for path, distinct in values.items()
        if len(distinct) >= MIN_DISTINCT and path not in repeated
    }


def _words(name: str) -> str:
    """``exchangeId`` / ``Exchange-ID`` -> ``exchange_id``."""
    return _NOT_ALNUM.sub("_", _CAMEL_BOUNDARY.sub("_", name).lower()).strip("_")


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _leaf_name(path: str) -> str:
    """The last named segment of a JSON path (``$.tickers[*].exchangeId`` -> ``exchange_id``)."""
    names = [segment for segment in jsonpath.parse(path) if isinstance(segment, str)]
    return _words(names[-1]) if names else ""


def _resource_name(group: str) -> str:
    """``GET /v1/exchanges/{code}`` -> ``exchange``; a collection ``exchanges`` -> ``exchange``."""
    target = _HTTP_METHOD.sub("", group)
    segments = [
        segment
        for segment in re.split(r"[/:]", target)
        if segment and not (segment.startswith("{") and segment.endswith("}"))
    ]
    return _singular(_words(segments[-1])) if segments else ""


def _contains(outer: str, inner: str) -> bool:
    """Whether ``inner`` appears in ``outer`` as whole ``_``-separated words."""
    return len(inner) >= _MIN_PARTIAL_NAME and f"_{inner}_" in f"_{outer}_"


def _name_hint(field: str, group: str, target_field: str) -> bool:
    """Whether the referencing field's name points at the target group or its key.

    ``exchange``, ``exchangeId``, ``exchange_code``, ``exchanges[*]`` and ``primary_exchange`` all
    point at an ``exchanges`` group; a field named like the target key (``code`` -> ``code``) does
    too. A bare ``id`` names nothing in particular.
    """
    base = _ID_SUFFIX.sub("", _leaf_name(field))
    if base in {"", "id", "ids"}:
        return False
    resource = _resource_name(group)
    target_leaf = _leaf_name(target_field)
    # A bare "id" suffix ("exchangeid") is only stripped as an extra guess: "paid" and "valid"
    # must not lose letters for the comparisons below unless the stripped form actually matches.
    variants = {base, _singular(base), _BARE_ID_SUFFIX.sub("", base)}
    for variant in variants:
        if variant in {resource, target_leaf}:
            return True
        if resource and (_contains(variant, resource) or _contains(resource, variant)):
            return True
    return False


def relation_candidates(groups: Mapping[str, Sequence[JsonValue]]) -> list[RelationCandidate]:
    """Candidate relations between groups of documents (endpoint ids or collection names).

    ``groups`` maps a name to the documents it returned (whole bodies or individual records).
    """
    values = {name: _scalar_values(docs) for name, docs in groups.items()}
    keys = {name: _identifying(values[name], docs) for name, docs in groups.items()}
    found: list[RelationCandidate] = []
    for from_group, fields in values.items():
        for field, referencing in fields.items():
            if len(referencing) < MIN_DISTINCT:
                continue
            for to_group, target_keys in keys.items():
                for to_field, targets in target_keys.items():
                    if to_group == from_group and to_field == field:
                        continue
                    covered = referencing & targets
                    coverage = len(covered) / len(referencing)
                    if coverage < MIN_COVERAGE:
                        continue
                    missing = sorted(referencing - targets)[:MAX_MISSING]
                    unreferenced = sorted(targets - referencing)[:MAX_MISSING]
                    found.append(
                        RelationCandidate(
                            from_group=from_group,
                            from_field=field,
                            to_group=to_group,
                            to_field=to_field,
                            coverage=round(coverage, 3),
                            distinct_values=len(referencing),
                            missing=list(missing),
                            name_hint=_name_hint(field, to_group, to_field),
                            reverse_coverage=round(len(covered) / len(targets), 3),
                            unreferenced=list(unreferenced),
                        )
                    )
    found.sort(
        key=lambda c: (not c.name_hint, -c.coverage, -c.distinct_values, c.from_group, c.from_field)
    )
    return found


def candidates_table(candidates: Sequence[RelationCandidate], *, limit: int = 40) -> str:
    """Markdown table for the agent.

    *coverage* is the forward direction (references found among the targets), *reverse* the other
    way (targets that something references): evidence for ``min_per_target``, to weigh with what
    the domain says (every team has a city; a city needs a team only in a sports API).
    """
    lines = [
        (
            "| from | field | to | key field | coverage | distinct | name hint | missing "
            "| reverse | unreferenced |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in candidates[:limit]:
        missing = ", ".join(str(m) for m in item.missing) or "-"
        unreferenced = ", ".join(str(u) for u in item.unreferenced) or "-"
        hint = "yes" if item.name_hint else ""
        lines.append(
            f"| {item.from_group} | `{item.from_field}` | {item.to_group} | `{item.to_field}` | "
            f"{item.coverage:.0%} | {item.distinct_values} | {hint} | {missing} "
            f"| {item.reverse_coverage:.0%} | {unreferenced} |"
        )
    return "\n".join(lines)
