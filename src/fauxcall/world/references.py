"""Keeping references as writes change the world, the way the real API does.

The model's relations say what the real API does when a write would break a reference
(:class:`~fauxcall.model.world.RelationModel`):

* :func:`missing_references` — a write's references to targets that don't exist, for relations
  whose ``on_missing`` is ``refuse``;
* :func:`plan_delete` / :func:`apply_delete` — what deleting a record does to the records that
  reference it: refused (``restrict``), deleted too (``cascade``), cleared (``set_null``) or left
  dangling (``ignore`` or undecided). A plan is worked out in full before anything changes, so a
  ``restrict`` anywhere down a cascade refuses the whole delete, as a database would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from fauxcall.jsonfmt import canonical_dumps
from fauxcall.jsonpath import WILDCARD, iter_matches
from fauxcall.world.invariants import identities, target_matcher
from fauxcall.world.io import copy_json
from fauxcall.world.keys import parse_path

if TYPE_CHECKING:
    from fauxcall.model.api import ApiModel
    from fauxcall.model.world import RelationModel, WorldRecord
    from fauxcall.types import JsonObject, JsonValue
    from fauxcall.world.store import SessionWorld


@dataclass
class DeletePlan:
    """Everything one delete changes, or what refuses it."""

    blocked_by: tuple[RelationModel, WorldRecord] | None = None
    """The relation that refuses the delete (``restrict``) and one record still referencing."""
    deletes: list[tuple[str, WorldRecord]] = field(default_factory=list)
    """``(collection, record)``: the record itself first, then what cascades."""
    clears: list[tuple[RelationModel, WorldRecord, frozenset[str]]] = field(default_factory=list)
    """``(relation, referencing record, target tokens)``: references to clear (``set_null``)."""


def missing_references(
    model: ApiModel, world: SessionWorld, collection: str, record: JsonValue
) -> list[tuple[RelationModel, JsonValue]]:
    """The references in ``record`` that point at nothing, where the real API refuses that.

    ``record`` is about to be written to ``collection``; only relations whose ``on_missing`` is
    ``refuse`` count.
    """
    found: list[tuple[RelationModel, JsonValue]] = []
    for relation in model.relations:
        if relation.from_collection != collection or relation.on_missing != "refuse":
            continue
        exists, _ = target_matcher(
            world, world.collection(relation.to_collection), relation.to_field
        )
        found += [
            (relation, value)
            for _, value in iter_matches(record, parse_path(relation.from_field))
            if value is not None and not exists(value)
        ]
    return found


def plan_delete(
    model: ApiModel, world: SessionWorld, collection: str, record: WorldRecord
) -> DeletePlan:
    """What deleting ``record`` from ``collection`` does (nothing is changed yet)."""
    plan = DeletePlan()
    seen: set[tuple[str, str]] = set()
    pending = [(collection, record)]
    while pending:
        current_collection, current = pending.pop(0)
        identity = (current_collection, canonical_dumps(current.key))
        if identity in seen:
            continue
        seen.add(identity)
        plan.deletes.append((current_collection, current))
        for relation in model.relations:
            if relation.to_collection != current_collection or relation.on_delete in {
                None,
                "ignore",
            }:
                continue
            tokens, referrers = _referrers(world, relation, current)
            referrers = [
                r
                for r in referrers
                if (relation.from_collection, canonical_dumps(r.key)) not in seen
            ]
            if not referrers:
                continue
            if relation.on_delete == "restrict":
                return DeletePlan(blocked_by=(relation, referrers[0]))
            if relation.on_delete == "cascade":
                pending += [(relation.from_collection, r) for r in referrers]
            else:
                plan.clears += [(relation, r, tokens) for r in referrers]
    return plan


def apply_delete(world: SessionWorld, plan: DeletePlan) -> None:
    """Carry out a plan from :func:`plan_delete` (one that nothing blocked)."""
    deleted = {(c, canonical_dumps(r.key)) for c, r in plan.deletes}
    for collection, record in plan.deletes:
        world.delete(collection, record.key)
    for relation, referrer, tokens in plan.clears:
        if (relation.from_collection, canonical_dumps(referrer.key)) in deleted:
            continue
        current = world.get(relation.from_collection, referrer.key)
        if current is None:
            continue
        cleared = _cleared(world, relation, current.record, tokens)
        world.replace(relation.from_collection, referrer.key, cast("JsonObject", cleared))


def _referrers(
    world: SessionWorld, relation: RelationModel, target: WorldRecord
) -> tuple[frozenset[str], list[WorldRecord]]:
    """The target's tokens, and the records referencing it through ``relation``."""
    token, tokens_of = identities(world.collection(relation.to_collection), relation.to_field)
    mine = frozenset(tokens_of(target))
    segments = parse_path(relation.from_field)
    return mine, [
        record
        for record in world.list(relation.from_collection)
        if any(
            value is not None and token(value) in mine
            for _, value in iter_matches(record.record, segments)
        )
    ]


def _cleared(
    world: SessionWorld, relation: RelationModel, record: JsonValue, tokens: frozenset[str]
) -> JsonValue:
    """``record`` without its references to ``tokens``: ``null``, or removed from a list."""
    token, _ = identities(world.collection(relation.to_collection), relation.to_field)
    segments = parse_path(relation.from_field)
    copied = copy_json(record)
    trails = [
        trail
        for trail, value in iter_matches(copied, segments)
        if value is not None and token(value) in tokens
    ]
    for trail in reversed(trails):  # last first: removing list items keeps earlier indices
        parent: JsonValue = copied
        for key in trail[:-1]:
            parent = parent[key]  # type: ignore[index]
        if segments[-1] is WILDCARD and isinstance(parent, list):
            del parent[trail[-1]]  # type: ignore[arg-type]
        else:
            parent[trail[-1]] = None  # type: ignore[index]
    return copied
