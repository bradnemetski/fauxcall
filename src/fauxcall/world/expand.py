"""Build-time world expansion: grow a collection to the real API's size, deterministically.

Real lists are often far larger than what was recorded (the API reports ``total: 10342``; we kept
50 records). Expansion clones recorded records round-robin, gives every clone a fresh unique key and
re-samples the fields listed in :attr:`ExpansionSpec.vary` from the values seen in the recordings —
or from ``reference_values`` (typically from
:func:`~fauxcall.world.invariants.resolve_relation_targets`) so references stay valid. The result
is frozen into the world files and invariant-checked like recorded data; the same seed always
produces the same records.
"""

from __future__ import annotations

import itertools
import random
import re
import string
import uuid
from typing import TYPE_CHECKING, Final

from fauxcall.errors import WorldError
from fauxcall.jsonpath import iter_matches, set_all
from fauxcall.model.world import WorldRecord
from fauxcall.world.io import copy_json
from fauxcall.world.keys import collection_key, parse_path, set_key

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fauxcall.model.world import CollectionModel, ExpansionSpec
    from fauxcall.types import JsonValue
    from fauxcall.world.keys import Key

_CODE: Final = re.compile(r"[A-Z]{2,6}")
_RANDOM_CODE_ATTEMPTS: Final = 100


def expand_collection(
    collection: CollectionModel,
    records: Sequence[WorldRecord],
    spec: ExpansionSpec,
    *,
    reference_values: Mapping[str, Sequence[JsonValue]] | None = None,
    first_values: Mapping[str, Sequence[JsonValue]] | None = None,
) -> list[WorldRecord]:
    """The original records followed by synthetic ones, ``spec.target_count`` in total.

    Synthetic records are deep copies of the originals (round-robin) with:

    * a new unique key written at ``collection.key`` — ``int``: max + 1, max + 2 …; ``uuid``:
      seeded random UUID4s; ``str``: new uppercase codes of the template's length when every
      existing key is a 2-6 letter code (tickers, exchange MICs), else ``"<template key>-<n>"``;
    * each ``spec.vary`` path re-sampled (seeded) from the values observed at that path across the
      originals — or from ``reference_values[path]`` when given (paths compare parsed, so
      ``"$.exchange"`` matches ``"exchange"``); ``first_values[path]`` are used up first, in
      order (targets that still need referrers, so every exchange gets its companies);
    * ``synthetic=True``, the template's ``recorded_at`` and ``source="expansion:<template
      source or key>"``.

    Originals are returned unchanged when there are already ``target_count`` of them.

    Raises:
        WorldError: There is nothing to clone, a ``vary`` path is invalid or the root, or the key
            space is exhausted.
    """
    originals = list(records)
    missing = spec.target_count - len(originals)
    if missing <= 0:
        return originals
    if not originals:
        raise WorldError(f"collection {collection.name!r}: cannot expand an empty collection")
    rng = random.Random(spec.seed)
    pools = _pools(originals, spec.vary, reference_values or {})
    firsts = {parse_path(path): list(values) for path, values in (first_values or {}).items()}
    queues = {path: firsts.get(parse_path(path), []) for path, _ in pools}
    keys = _KeyMaker(collection, originals, rng)
    synthetic = [
        _clone(collection, originals[number % len(originals)], pools, keys, rng, queues)
        for number in range(missing)
    ]
    return [*originals, *synthetic]


def _pools(
    originals: Sequence[WorldRecord],
    vary: Sequence[str],
    references: Mapping[str, Sequence[JsonValue]],
) -> list[tuple[str, list[JsonValue]]]:
    """For each path to vary, the values to sample from (observed ones keep their frequency).

    Reference paths match by meaning, not spelling: ``$.exchange`` and ``exchange`` are the same.
    """
    by_path = {parse_path(path): values for path, values in references.items()}
    pools: list[tuple[str, list[JsonValue]]] = []
    for path in vary:
        segments = parse_path(path)
        if not segments:
            raise WorldError("cannot vary the whole record ('$')")
        if segments in by_path:
            pool = list(by_path[segments])
        else:
            pool = [
                value for record in originals for _, value in iter_matches(record.record, segments)
            ]
        pools.append((path, pool))
    return pools


def _given(value: JsonValue) -> Callable[[JsonValue], JsonValue]:
    def give(_old: JsonValue) -> JsonValue:
        return copy_json(value)

    return give


def _sampler(pool: Sequence[JsonValue], rng: random.Random) -> Callable[[JsonValue], JsonValue]:
    def sample(_old: JsonValue) -> JsonValue:
        return copy_json(rng.choice(pool))

    return sample


def _clone(
    collection: CollectionModel,
    template: WorldRecord,
    pools: Sequence[tuple[str, Sequence[JsonValue]]],
    keys: _KeyMaker,
    rng: random.Random,
    queues: Mapping[str, list[JsonValue]],
) -> WorldRecord:
    payload = copy_json(template.record)
    for path, pool in pools:
        queue = queues.get(path)
        if queue:
            set_all(payload, path, _given(queue.pop(0)))
        elif pool:
            set_all(payload, path, _sampler(pool, rng))
    key = keys.new_key(collection_key(collection, template.key))
    spelled = next(
        (value for _, value in iter_matches(template.record, parse_path(collection.key))), None
    )
    payload = set_key(collection, payload, str(key) if isinstance(spelled, str) else key)
    return WorldRecord(
        key=key,
        record=payload,
        recorded_at=template.recorded_at,
        source=f"expansion:{template.source or template.key}",
        synthetic=True,
    )


class _KeyMaker:
    """Hands out keys that never collide with the originals' or with each other."""

    def __init__(
        self, collection: CollectionModel, originals: Sequence[WorldRecord], rng: random.Random
    ) -> None:
        self._collection = collection
        self._rng = rng
        self._used: set[Key] = {collection_key(collection, record.key) for record in originals}
        ints = [key for key in self._used if isinstance(key, int)]
        self._next_int = max(ints, default=0) + 1
        self._codes = all(isinstance(key, str) and _CODE.fullmatch(key) for key in self._used)
        self._suffixes: dict[str, int] = {}

    def new_key(self, template_key: Key) -> Key:
        key: Key
        if self._collection.key_type == "int":
            key = self._next_int
            self._next_int += 1
        elif self._collection.key_type == "uuid":
            key = self._uuid()
        elif self._codes:
            key = self._code(len(str(template_key)))
        else:
            key = self._suffixed(str(template_key))
        self._used.add(key)
        return key

    def _uuid(self) -> str:
        while True:
            key = str(uuid.UUID(int=self._rng.getrandbits(128), version=4))
            if key not in self._used:
                return key

    def _code(self, length: int) -> str:
        letters = string.ascii_uppercase
        for _ in range(_RANDOM_CODE_ATTEMPTS):
            code = "".join(self._rng.choice(letters) for _ in range(length))
            if code not in self._used:
                return code
        for chars in itertools.product(letters, repeat=length):  # nearly exhausted: be thorough
            code = "".join(chars)
            if code not in self._used:
                return code
        raise WorldError(
            f"collection {self._collection.name!r}: every {length}-letter code is taken"
        )

    def _suffixed(self, template_key: str) -> str:
        number = self._suffixes.get(template_key, 0)
        while True:
            number += 1
            key = f"{template_key}-{number}"
            if key not in self._used:
                self._suffixes[template_key] = number
                return key
