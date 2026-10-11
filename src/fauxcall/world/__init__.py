"""The consistent world a fake serves: collections of records, sessions, invariants, expansion.

* :mod:`~fauxcall.world.keys` — key normalization (``"42"`` and ``42`` find the same record).
* :mod:`~fauxcall.world.io` — collection files: JSONL of :class:`~fauxcall.model.world.WorldRecord`.
* :mod:`~fauxcall.world.store` — the immutable base world and per-session copy-on-write overlays.
* :mod:`~fauxcall.world.invariants` — relation and invariant checks.
* :mod:`~fauxcall.world.expand` — deterministic build-time world expansion.
"""

from fauxcall.world.expand import expand_collection
from fauxcall.world.invariants import check_world, resolve_relation_targets
from fauxcall.world.io import (
    copy_json,
    copy_record,
    load_collection,
    records_from_values,
    save_collection,
)
from fauxcall.world.keys import (
    DuplicateKeyError,
    InvalidKeyError,
    Key,
    collection_key,
    normalize_key,
    record_key,
)
from fauxcall.world.store import (
    CollectionState,
    SessionState,
    SessionWorld,
    World,
    WorldState,
)

__all__ = [
    "CollectionState",
    "DuplicateKeyError",
    "InvalidKeyError",
    "Key",
    "SessionState",
    "SessionWorld",
    "World",
    "WorldState",
    "check_world",
    "collection_key",
    "copy_json",
    "copy_record",
    "expand_collection",
    "load_collection",
    "normalize_key",
    "record_key",
    "records_from_values",
    "resolve_relation_targets",
    "save_collection",
]
