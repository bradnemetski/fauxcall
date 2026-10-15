"""Consistency checks: relations between collections and declared invariants.

The world's core promise is consistency across endpoints — every ticker references an exchange
that exists, every uploaded document shows up in its status endpoint. :func:`check_world` verifies
that for one session's view (after every build, and in each fake's tests) and returns
human-readable problems; it never raises for a broken world or a broken check.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections import Counter
from typing import TYPE_CHECKING

from fauxcall.errors import WorldError
from fauxcall.jsonfmt import canonical_dumps
from fauxcall.jsonpath import WILDCARD, format_path, iter_matches
from fauxcall.world.io import copy_json
from fauxcall.world.keys import InvalidKeyError, normalize_key, parse_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from fauxcall.jsonpath import Segment
    from fauxcall.model.api import ApiModel
    from fauxcall.model.world import CollectionModel, InvariantModel, RelationModel, WorldRecord
    from fauxcall.types import JsonValue
    from fauxcall.world.store import SessionWorld


def check_world(
    model: ApiModel,
    world: SessionWorld,
    *,
    import_paths: Sequence[Path] = (),
    after_writes: bool = False,
) -> list[str]:
    """Every relation and invariant violation in ``world``, as human-readable problems.

    * Each required :class:`~fauxcall.model.world.RelationModel`: every non-null value at
      ``from_field`` must exist in ``to_collection`` — as a key (normalized with the target's key
      type) when ``to_field`` is unset or the key, else as a raw value at ``to_field``.
    * :class:`~fauxcall.model.world.InvariantModel` ``unique`` (non-null values unique across the
      collection), ``not_null`` (present and non-null in every record; for ``[*]`` paths every
      element) and ``custom`` (``fn(world) -> list[str]`` imported from ``package.module:function``
      or ``package.module.function``; import and call errors become problems). ``import_paths``
      are searched first, e.g. the fake's ``src`` folder, so a fake's own handlers module can
      hold its checks even when the CLI runs outside the fake's environment).

    * The reverse, for relations with ``min_per_target``: every target record is referenced by at
      least that many records.

    ``after_writes`` checks a world that writes have changed (after a scenario): only the
    references writes keep (:func:`kept_by_writes`), and no reverse minimums — a target created
    through the API has no referrers yet, on the real API too.

    Problems are ordered: relations, then invariants (model order), then records (list order).
    """
    problems: list[str] = []
    for relation in model.relations:
        try:
            if relation.required and (not after_writes or kept_by_writes(model, relation)):
                problems += _relation_problems(world, relation)
            if relation.min_per_target and not after_writes:
                problems += _reverse_problems(world, relation)
        except WorldError as exc:  # unknown collection or invalid path
            problems.append(f"relation {relation.name!r}: {exc}")
    for invariant in model.invariants:
        try:
            problems += _invariant_problems(world, invariant, import_paths)
        except WorldError as exc:
            problems.append(f"invariant {invariant.name!r}: {exc}")
    return problems


def kept_by_writes(model: ApiModel, relation: RelationModel) -> bool:
    """Whether writes keep every reference of ``relation`` resolving, as the real API does.

    Writes to the referencing collection must refuse missing targets (or there are none), and
    deleting a target must restrict, cascade or clear (or nothing deletes targets).
    """
    refuses = relation.on_missing == "refuse" or not model.writes_to(relation.from_collection)
    guarded = relation.on_delete in {"restrict", "cascade", "set_null"}
    guarded = guarded or not model.deletes_from(relation.to_collection)
    return refuses and guarded


def relation_summary(model: ApiModel, world: SessionWorld) -> list[str]:
    """One line per relation: references, dangling ones, and how many targets are referenced.

    The reverse coverage is evidence for ``min_per_target`` (``48 of 48 exchanges referenced``),
    to weigh with domain knowledge: a sample can cover every target by chance.
    """
    lines: list[str] = []
    for relation in model.relations:
        try:
            source = world.collection(relation.from_collection)
            target = world.collection(relation.to_collection)
            token, tokens_of = identities(target, relation.to_field)
            counts = _referrer_counts(world, relation, token)
            references = sum(counts.values())
            exists, _ = target_matcher(world, target, relation.to_field)
            dangling = sum(
                1
                for record in world.list(source.name, copy=False)
                for _, value in iter_matches(record.record, parse_path(relation.from_field))
                if value is not None and not exists(value)
            )
            per_target = [
                max((counts[t] for t in tokens_of(r)), default=0)
                for r in world.list(target.name, copy=False)
            ]
        except WorldError as exc:
            lines.append(f"{relation.name}: {exc}")
            continue
        referenced = sum(1 for n in per_target if n)
        fewest = min(per_target, default=0)
        rule = "undecided" if relation.min_per_target is None else str(relation.min_per_target)
        lines.append(
            f"{relation.name}: {source.name}.{relation.from_field} -> {target.name}: "
            f"{references} references, {dangling} dangling; {referenced} of {len(per_target)} "
            f"{target.name} referenced (fewest referrers: {fewest}; min_per_target: {rule})"
        )
    return lines


def uncovered_targets(
    world: SessionWorld, relation: RelationModel, referrers: Sequence[WorldRecord]
) -> list[JsonValue]:
    """Target values still short of ``min_per_target`` referrers among ``referrers``.

    Each value appears once per missing referrer, in the target collection's order, ready to be
    handed to new referencing records (world expansion).
    """
    need = relation.min_per_target or 0
    if not need:
        return []
    target = world.collection(relation.to_collection)
    token, tokens_of = identities(target, relation.to_field)
    field = parse_path(relation.from_field)
    counts: Counter[str] = Counter()
    for record in referrers:
        counts.update(
            {
                t
                for _, value in iter_matches(record.record, field)
                if value is not None and (t := token(value)) is not None
            }
        )
    segments = parse_path(relation.to_field or target.key)
    by_key = segments == parse_path(target.key)
    wanted: list[JsonValue] = []
    for record in world.list(target.name, copy=False):
        have = max((counts[t] for t in tokens_of(record)), default=0)
        if have >= need:
            continue
        values = [record.key] if by_key else [v for _, v in iter_matches(record.record, segments)]
        if values:
            wanted += [copy_json(values[0])] * (need - have)
    return wanted


def resolve_relation_targets(model: ApiModel, world: SessionWorld) -> dict[str, list[JsonValue]]:
    """The valid target values of every relation (by relation name), for world expansion.

    For each relation: the distinct non-null values at ``to_field`` (default: the target's key)
    across the target collection, in list order. Writing any of them at ``from_field`` keeps the
    relation satisfied.

    Raises:
        WorldError: A relation names an unknown collection or has an invalid path.
    """
    targets: dict[str, list[JsonValue]] = {}
    for relation in model.relations:
        target = world.collection(relation.to_collection)
        segments = parse_path(relation.to_field or target.key)
        by_key = segments == parse_path(target.key)
        targets[relation.name] = _target_values(
            world.list(target.name, copy=False), segments, by_key=by_key
        )
    return targets


def _target_values(
    records: Sequence[WorldRecord], segments: tuple[Segment, ...], *, by_key: bool
) -> list[JsonValue]:
    values: dict[str, JsonValue] = {}
    for record in records:
        found = [value for _, value in iter_matches(record.record, segments)]
        if by_key and not found:  # the payload omits its key; the stored key is the reference
            found = [record.key]
        for value in found:
            if value is not None:
                values.setdefault(canonical_dumps(value), copy_json(value))
    return list(values.values())


def _render(segments: tuple[Segment, ...]) -> str:
    """``.exchange``, ``.tags[0].id``, ``["odd key"]`` (``""`` for the root)."""
    return format_path(segments).removeprefix("$")


# ---------------------------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------------------------


def _relation_problems(world: SessionWorld, relation: RelationModel) -> list[str]:
    source = world.collection(relation.from_collection)
    target = world.collection(relation.to_collection)
    field = parse_path(relation.from_field)
    exists, where = target_matcher(world, target, relation.to_field)
    problems: list[str] = []
    for record in world.list(source.name, copy=False):
        for trail, value in iter_matches(record.record, field):
            if value is not None and not exists(value):
                problems.append(
                    f"relation {relation.name!r}: {source.name}[{record.key!r}]{_render(trail)} "
                    f"= {value!r} not found in {where}"
                )
    return problems


def identities(
    target: CollectionModel, to_field: str | None
) -> tuple[Callable[[JsonValue], str | None], Callable[[WorldRecord], list[str]]]:
    """How references and target records compare: a reference's token, a target's tokens."""
    segments = parse_path(to_field or target.key)
    by_key = segments == parse_path(target.key)

    def token(value: JsonValue) -> str | None:
        if not by_key:
            return canonical_dumps(value)
        try:
            return canonical_dumps(normalize_key(value, target.key_type))
        except InvalidKeyError:
            return None

    def tokens_of(record: WorldRecord) -> list[str]:
        if by_key:
            return [t for t in (token(record.key),) if t is not None]
        return [
            canonical_dumps(value)
            for _, value in iter_matches(record.record, segments)
            if value is not None
        ]

    return token, tokens_of


def _referrer_counts(
    world: SessionWorld, relation: RelationModel, token: Callable[[JsonValue], str | None]
) -> Counter[str]:
    """How many referencing records point at each target token (a record counts once)."""
    field = parse_path(relation.from_field)
    counts: Counter[str] = Counter()
    for record in world.list(relation.from_collection, copy=False):
        counts.update(
            {
                t
                for _, value in iter_matches(record.record, field)
                if value is not None and (t := token(value)) is not None
            }
        )
    return counts


def _reverse_problems(world: SessionWorld, relation: RelationModel) -> list[str]:
    source = world.collection(relation.from_collection)
    target = world.collection(relation.to_collection)
    token, tokens_of = identities(target, relation.to_field)
    counts = _referrer_counts(world, relation, token)
    need = relation.min_per_target or 0
    problems: list[str] = []
    for record in world.list(target.name, copy=False):
        have = max((counts[t] for t in tokens_of(record)), default=0)
        if have < need:
            problems.append(
                f"relation {relation.name!r} (reverse): {target.name}[{record.key!r}] is "
                f"referenced by {have} {source.name} record(s), needs at least {need} "
                f"(record its {source.name}, or expand {source.name} after {target.name})"
            )
    return problems


def target_matcher(
    world: SessionWorld, target: CollectionModel, to_field: str | None
) -> tuple[Callable[[JsonValue], bool], str]:
    """A membership test for referenced values, and how to name the target in messages."""
    segments = parse_path(to_field or target.key)
    if segments == parse_path(target.key):
        keys = set(world.keys(target.name))

        def has_key(value: JsonValue) -> bool:
            try:
                key = normalize_key(value, target.key_type)
            except InvalidKeyError:
                return False
            return key in keys

        return has_key, target.name

    values = {
        canonical_dumps(value)
        for record in world.list(target.name, copy=False)
        for _, value in iter_matches(record.record, segments)
        if value is not None
    }

    def has_value(value: JsonValue) -> bool:
        return canonical_dumps(value) in values

    return has_value, f"{target.name}{_render(segments)}"


# ---------------------------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------------------------


def _invariant_problems(
    world: SessionWorld, invariant: InvariantModel, import_paths: Sequence[Path] = ()
) -> list[str]:
    collection = world.collection(invariant.collection)
    prefix = f"invariant {invariant.name!r}"
    if invariant.kind == "custom":
        return _custom_problems(world, invariant, prefix, import_paths)
    if invariant.field is None:
        return [f"{prefix}: a {invariant.kind!r} invariant needs a field"]
    segments = parse_path(invariant.field)
    records = world.list(collection.name, copy=False)
    if invariant.kind == "unique":
        return _unique_problems(prefix, collection.name, records, segments)
    return _not_null_problems(prefix, collection.name, records, segments)


def _unique_problems(
    prefix: str, name: str, records: Sequence[WorldRecord], segments: tuple[Segment, ...]
) -> list[str]:
    first_holder: dict[str, JsonValue] = {}
    problems: list[str] = []
    for record in records:
        for trail, value in iter_matches(record.record, segments):
            if value is None:
                continue
            token = canonical_dumps(value)
            if token in first_holder:
                problems.append(
                    f"{prefix}: {name}[{record.key!r}]{_render(trail)} = {value!r} "
                    f"duplicates {name}[{first_holder[token]!r}]"
                )
            else:
                first_holder[token] = record.key
    return problems


def _not_null_problems(
    prefix: str, name: str, records: Sequence[WorldRecord], segments: tuple[Segment, ...]
) -> list[str]:
    each_element = WILDCARD in segments  # an empty array has no elements to be null
    problems: list[str] = []
    for record in records:
        values = [value for _, value in iter_matches(record.record, segments)]
        where = f"{name}[{record.key!r}]{_render(segments)}"
        if not values and not each_element:
            problems.append(f"{prefix}: {where} is missing")
        elif any(value is None for value in values):
            problems.append(f"{prefix}: {where} is null")
    return problems


def _custom_problems(
    world: SessionWorld,
    invariant: InvariantModel,
    prefix: str,
    import_paths: Sequence[Path] = (),
) -> list[str]:
    function = invariant.function
    if not function:
        return [f"{prefix}: a 'custom' invariant needs a function"]
    try:
        with _searching(import_paths):
            check = _import_function(function)
    except Exception as exc:  # noqa: BLE001 - a broken check is reported, never raised
        return [f"{prefix}: cannot import {function!r}: {type(exc).__name__}: {exc}"]
    try:
        found = check(world)
    except Exception as exc:  # noqa: BLE001 - same
        return [f"{prefix}: {function!r} raised {type(exc).__name__}: {exc}"]
    if not isinstance(found, list) or not all(isinstance(problem, str) for problem in found):
        return [f"{prefix}: {function!r} must return a list of strings, not {found!r}"]
    return [f"{prefix}: {problem}" for problem in found]


@contextlib.contextmanager
def _searching(paths: Sequence[Path]) -> Iterator[None]:
    """Put ``paths`` first on the import path meanwhile (modules imported stay imported)."""
    added = [str(path) for path in paths if str(path) not in sys.path]
    sys.path[:0] = added
    try:
        yield
    finally:
        for entry in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)


def _import_function(path: str) -> Callable[[SessionWorld], object]:
    """Import ``package.module:function`` (``:Class.method`` too) or ``package.module.function``."""
    module_name, colon, attribute = path.partition(":")
    if not colon:
        module_name, _, attribute = path.rpartition(".")
    if not module_name or not attribute:
        raise ValueError("expected 'package.module:function' or 'package.module.function'")
    target: object = importlib.import_module(module_name)
    for part in attribute.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f"{path!r} is not callable")
    return target
