"""The world at runtime: one immutable base shared by every session, plus per-session overlays.

The **base** is loaded once from the collection files and never changes while serving. Each
**session** (one isolated agent evaluation) sees it through its own copy-on-write **overlay**:
writes land in the overlay, so parallel evaluations never observe each other's changes, and
:meth:`World.reset` returns a session to the recorded seed instantly. Overlays can be snapshotted,
restored and — opt-in — persisted across restarts (:meth:`World.save_state`).

Records handed out are independent copies, so callers may mutate them freely (``list(...,
copy=False)`` returns the stored objects for fast read-only scans). Every returned
:class:`~fauxcall.model.world.WorldRecord` carries the *normalized* key
(:func:`~fauxcall.world.keys.normalize_key`). Lookups are O(1) dictionary hits.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, ValidationError

from fauxcall.errors import WorldError
from fauxcall.model.common import Model, RawJson
from fauxcall.model.world import WorldRecord
from fauxcall.storage import model_to_json, read_json, write_json
from fauxcall.world.io import copy_json, copy_record, load_collection
from fauxcall.world.keys import (
    DuplicateKeyError,
    collection_key,
    ensure_key,
    payload_key,
    record_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from fauxcall.model.api import ApiModel
    from fauxcall.model.world import CollectionModel
    from fauxcall.types import JsonObject, JsonValue
    from fauxcall.world.keys import Key

STATE_FORMAT: Final = 1
"""Version of the snapshot / state-file format."""


class CollectionState(Model):
    """One collection's overlay inside a session snapshot."""

    changed: list[WorldRecord] = Field(
        default_factory=list, description="Base records changed in place, in base order."
    )
    added: list[WorldRecord] = Field(
        default_factory=list, description="Records added in the session, in insertion order."
    )
    deleted: list[int | str] = Field(
        default_factory=list, description="Keys of base records hidden in the session."
    )
    last_key: int | None = Field(
        default=None, description="Highest integer key the session used (keys are never reused)."
    )


class SessionState(Model):
    """A session's overlay — what :meth:`SessionWorld.snapshot` returns."""

    format_version: Literal[1]
    collections: dict[str, CollectionState] = Field(default_factory=dict)


class WorldState(Model):
    """Every session's overlay — the file :meth:`World.save_state` writes."""

    format_version: Literal[1]
    sessions: dict[str, RawJson] = Field(
        default_factory=dict, description="Session id -> snapshot."
    )


@dataclass(frozen=True, slots=True)
class _Base:
    """One collection as recorded. Shared by every session and never mutated."""

    collection: CollectionModel
    order: tuple[Key, ...]
    records: tuple[WorldRecord, ...]
    index: dict[Key, WorldRecord]
    max_int_key: int | None

    @classmethod
    def build(cls, collection: CollectionModel, records: Iterable[WorldRecord]) -> _Base:
        index: dict[Key, WorldRecord] = {}
        for record in records:
            key = _stored_key(collection, record)
            if key in index:
                raise DuplicateKeyError(f"collection {collection.name!r}: duplicate key {key!r}")
            index[key] = _with_key(record, key)
        order = list(index)
        if collection.order != "recorded":
            order.sort(reverse=collection.order == "key_desc")
        return cls(
            collection=collection,
            order=tuple(order),
            records=tuple(index[key] for key in order),
            index=index,
            max_int_key=max((key for key in order if isinstance(key, int)), default=None),
        )


@dataclass(slots=True)
class _Overlay:
    """One collection's changes in one session."""

    records: dict[Key, WorldRecord] = field(default_factory=dict)
    """Changed base records and added records, by key."""
    deleted: set[Key] = field(default_factory=set)
    """Base keys hidden in this session (deleted, or deleted and then added again)."""
    added: dict[Key, None] = field(default_factory=dict)
    """Keys listed after the base records, in insertion order."""
    last_key: int | None = None
    """Highest integer key inserted or handed out by ``next_int_key``."""

    def is_empty(self) -> bool:
        return not (self.records or self.deleted or self.added) and self.last_key is None

    def see(self, key: Key) -> None:
        if isinstance(key, int) and (self.last_key is None or key > self.last_key):
            self.last_key = key

    def append(self, base: _Base, key: Key, record: WorldRecord) -> None:
        self.records[key] = record
        self.added[key] = None
        if key in base.index:
            self.deleted.add(key)
        self.see(key)


def _stored_key(collection: CollectionModel, record: WorldRecord) -> Key:
    """The normalized key of a stored record, checked against the key field of its payload."""
    key = collection_key(collection, record.key)
    found = payload_key(collection, record.record)
    if found is not None and found != key:
        raise WorldError(
            f"collection {collection.name!r}: record key {key!r} does not match "
            f"{collection.key} = {found!r}"
        )
    return key


def _with_key(record: WorldRecord, key: Key) -> WorldRecord:
    return record.model_copy(update={"key": key, "record": copy_json(record.record)})


def _lookup(bases: Mapping[str, _Base], name: str) -> _Base:
    base = bases.get(name)
    if base is None:
        known = ", ".join(repr(known) for known in bases) or "none"
        raise WorldError(f"unknown collection {name!r} (known: {known})")
    return base


def _first(pair: tuple[Key, WorldRecord]) -> Key:
    return pair[0]


class SessionWorld:
    """One session's view of the world: the shared base plus this session's overlay.

    Obtain it from :meth:`World.session`. Collection names are validated (unknown names raise
    :class:`~fauxcall.errors.WorldError`) and keys are normalized with the collection's key type,
    so ``"42"`` and ``42`` address the same record. Returned records are copies.

    Attributes:
        session_id: The session's id.
    """

    def __init__(self, session_id: str, bases: Mapping[str, _Base]) -> None:
        self.session_id = session_id
        self._bases = bases
        self._overlays = self._empty_overlays()

    def _empty_overlays(self) -> dict[str, _Overlay]:
        return {name: _Overlay() for name in self._bases}

    def _parts(self, collection: str) -> tuple[_Base, _Overlay]:
        return _lookup(self._bases, collection), self._overlays[collection]

    @staticmethod
    def _find(base: _Base, overlay: _Overlay, key: Key) -> WorldRecord | None:
        if key in overlay.records:
            return overlay.records[key]
        if key in overlay.deleted:
            return None
        return base.index.get(key)

    @staticmethod
    def _ordered(base: _Base, overlay: _Overlay) -> builtins.list[tuple[Key, WorldRecord]]:
        changed, deleted = overlay.records, overlay.deleted
        pairs = [
            (key, changed.get(key, record))
            for key, record in zip(base.order, base.records, strict=True)
            if key not in deleted
        ]
        if overlay.added:
            pairs.extend((key, changed[key]) for key in overlay.added)
            if base.collection.order != "recorded":
                pairs.sort(key=_first, reverse=base.collection.order == "key_desc")
        return pairs

    # -- reads ---------------------------------------------------------------------------------

    def collections(self) -> builtins.list[str]:
        """Collection names, in model order."""
        return builtins.list(self._bases)

    def collection(self, name: str) -> CollectionModel:
        """The model of collection ``name``."""
        return _lookup(self._bases, name).collection

    def get(self, collection: str, key: JsonValue) -> WorldRecord | None:
        """The record with this key (any spelling of it), or ``None``.

        Raises:
            InvalidKeyError: ``key`` cannot be a key of the collection (e.g. ``"abc"`` for integer
                ids) — typically answered with the real API's "invalid id" error.
        """
        base, overlay = self._parts(collection)
        found = self._find(base, overlay, collection_key(base.collection, key))
        return None if found is None else copy_record(found)

    def count(self, collection: str) -> int:
        """Number of records the session sees."""
        base, overlay = self._parts(collection)
        return len(base.order) - len(overlay.deleted) + len(overlay.added)

    def keys(self, collection: str) -> builtins.list[Key]:
        """Normalized keys in list order (see :meth:`list`)."""
        base, overlay = self._parts(collection)
        return [key for key, _ in self._ordered(base, overlay)]

    def list(self, collection: str, *, copy: bool = True) -> builtins.list[WorldRecord]:
        """Every record the session sees, in the collection's order.

        ``order="recorded"`` keeps the file order with changed records in place, deleted ones
        removed and added ones appended; ``"key_asc"`` / ``"key_desc"`` sort everything by key.

        Args:
            collection: Collection name.
            copy: ``False`` returns the stored records themselves — much faster for large
                collections, but they must be treated as read-only.
        """
        base, overlay = self._parts(collection)
        pairs = self._ordered(base, overlay)
        if not copy:
            return [record for _, record in pairs]
        return [copy_record(record) for _, record in pairs]

    # -- writes --------------------------------------------------------------------------------

    def insert(
        self,
        collection: str,
        record: JsonObject,
        *,
        recorded_at: datetime | None = None,
        source: str | None = None,
    ) -> WorldRecord:
        """Add a record (its key is read from the record itself). Returns the stored record.

        Raises:
            DuplicateKeyError: A record with the same key exists.
            InvalidKeyError: The record's key field holds a value that cannot be a key.
            WorldError: The record has no key.
        """
        base, overlay = self._parts(collection)
        key = record_key(base.collection, record)
        if self._find(base, overlay, key) is not None:
            raise DuplicateKeyError(
                f"collection {collection!r}: a record with key {key!r} already exists"
            )
        return copy_record(self._add(base, overlay, key, record, recorded_at, source))

    def replace(self, collection: str, key: JsonValue, record: JsonObject) -> WorldRecord | None:
        """Replace the payload of an existing record; ``None`` if there is none.

        The record's key field is set to ``key`` when absent; a different key is refused.
        ``recorded_at``, ``source`` and ``synthetic`` of the existing record are kept.

        Raises:
            WorldError: The record carries a different key.
        """
        base, overlay = self._parts(collection)
        normalized = collection_key(base.collection, key)
        current = self._find(base, overlay, normalized)
        if current is None:
            return None
        return copy_record(self._put(base, overlay, current, normalized, copy_json(record)))

    def update(self, collection: str, key: JsonValue, patch: JsonObject) -> WorldRecord | None:
        """Shallow-merge ``patch`` into an existing record; ``None`` if there is none.

        The key never changes: a patch that removes or nulls the key field gets it put back.
        ``recorded_at``, ``source`` and ``synthetic`` of the existing record are kept.

        Raises:
            WorldError: The patch would change the key, or the record is not a JSON object.
        """
        base, overlay = self._parts(collection)
        normalized = collection_key(base.collection, key)
        current = self._find(base, overlay, normalized)
        if current is None:
            return None
        if not isinstance(current.record, dict):
            raise WorldError(
                f"collection {collection!r}: record {normalized!r} is not an object; "
                "it cannot be patched"
            )
        merged = copy_json({**current.record, **patch})
        return copy_record(self._put(base, overlay, current, normalized, merged))

    def upsert(
        self,
        collection: str,
        record: JsonObject,
        *,
        recorded_at: datetime | None = None,
        source: str | None = None,
    ) -> WorldRecord:
        """Replace the record with the same key, or insert it. Returns the stored record.

        ``recorded_at`` and ``source`` only apply when the record is inserted.
        """
        base, overlay = self._parts(collection)
        key = record_key(base.collection, record)
        current = self._find(base, overlay, key)
        if current is None:
            stored = self._add(base, overlay, key, record, recorded_at, source)
        else:
            stored = self._put(base, overlay, current, key, copy_json(record))
        return copy_record(stored)

    def delete(self, collection: str, key: JsonValue) -> bool:
        """Remove a record. Returns whether it existed."""
        base, overlay = self._parts(collection)
        normalized = collection_key(base.collection, key)
        if self._find(base, overlay, normalized) is None:
            return False
        overlay.records.pop(normalized, None)
        overlay.added.pop(normalized, None)
        if normalized in base.index:
            overlay.deleted.add(normalized)
        return True

    def next_int_key(self, collection: str, *, start: int | None = None) -> int:
        """Hand out the next integer key, like a database sequence.

        The key is ``max(every int key seen in this session — base, inserted, deleted —,
        start - 1) + 1``. Each call consumes the key, so keys are never reused within a session.

        Raises:
            WorldError: The collection does not have integer keys.
        """
        base, overlay = self._parts(collection)
        if base.collection.key_type != "int":
            raise WorldError(
                f"collection {collection!r} has {base.collection.key_type} keys, not int keys"
            )
        seen = [key for key in (base.max_int_key, overlay.last_key) if key is not None]
        if start is not None:
            seen.append(start - 1)
        key = max(seen, default=0) + 1
        overlay.see(key)
        return key

    @staticmethod
    def _add(
        base: _Base,
        overlay: _Overlay,
        key: Key,
        record: JsonValue,
        recorded_at: datetime | None,
        source: str | None,
    ) -> WorldRecord:
        stored = WorldRecord(
            key=key, record=copy_json(record), recorded_at=recorded_at, source=source
        )
        overlay.append(base, key, stored)
        return stored

    @staticmethod
    def _put(
        base: _Base, overlay: _Overlay, current: WorldRecord, key: Key, payload: JsonValue
    ) -> WorldRecord:
        payload = ensure_key(base.collection, payload, key)
        stored = current.model_copy(update={"record": payload})
        overlay.records[key] = stored
        return stored

    # -- state ---------------------------------------------------------------------------------

    def is_modified(self) -> bool:
        """Whether the session differs from the seed (its data, or its integer key sequence)."""
        return any(not overlay.is_empty() for overlay in self._overlays.values())

    def reset(self) -> None:
        """Discard every change: the session sees the recorded seed again."""
        self._overlays = self._empty_overlays()

    def snapshot(self) -> JsonValue:
        """The session's overlay as JSON (a :class:`SessionState`; deterministic ordering).

        Collections are sorted by name, changed records follow the base order, added records
        their insertion order and deleted keys are sorted. Unchanged collections are omitted.
        """
        collections = {
            name: self._collection_state(self._bases[name], overlay)
            for name, overlay in sorted(self._overlays.items())
            if not overlay.is_empty()
        }
        state = SessionState(format_version=STATE_FORMAT, collections=collections)
        return model_to_json(state)

    @staticmethod
    def _collection_state(base: _Base, overlay: _Overlay) -> CollectionState:
        changed = [
            overlay.records[key]
            for key in base.order
            if key in overlay.records and key not in overlay.added
        ]
        return CollectionState(
            changed=changed,
            added=[overlay.records[key] for key in overlay.added],
            deleted=sorted(overlay.deleted),
            last_key=overlay.last_key,
        )

    def restore(self, snapshot: JsonValue) -> None:
        """Replace the session's overlay with a :meth:`snapshot` (atomically).

        Restoring tolerates a base that changed since the snapshot was taken (e.g. the world was
        rebuilt): deletions of records that no longer exist are dropped, and changed records
        whose base record is gone are kept as added records.

        Raises:
            WorldError: The snapshot is invalid or names an unknown collection.
        """
        try:
            state = SessionState.model_validate(copy_json(snapshot))
        except ValidationError as exc:
            raise WorldError(f"invalid world snapshot: {exc}") from exc
        overlays = self._empty_overlays()
        for name, collection_state in state.collections.items():
            base = _lookup(self._bases, name)
            overlays[name] = self._overlay_from(base, collection_state)
        self._overlays = overlays

    @staticmethod
    def _overlay_from(base: _Base, state: CollectionState) -> _Overlay:
        collection = base.collection
        overlay = _Overlay(last_key=state.last_key)
        for value in state.deleted:
            key = collection_key(collection, value)
            if key in base.index:
                overlay.deleted.add(key)
        for record in state.changed:
            key = _stored_key(collection, record)
            if key in base.index and key not in overlay.deleted:
                overlay.records[key] = _with_key(record, key)
            else:
                overlay.append(base, key, _with_key(record, key))
        for record in state.added:
            key = _stored_key(collection, record)
            overlay.append(base, key, _with_key(record, key))
        return overlay


class World:
    """The consistent dataset a fake serves: an immutable base and isolated sessions.

    Attributes:
        model: The API model the world was built for.
    """

    model: ApiModel

    def __init__(self, model: ApiModel, data_dir: Path) -> None:
        """Load every collection of ``model`` from ``data_dir`` (a missing file is empty).

        Raises:
            WorldError: A collection file is invalid, or holds duplicate or inconsistent keys.
        """
        records = {c.name: load_collection(data_dir / c.path) for c in model.collections}
        self._setup(model, records)

    @classmethod
    def from_records(cls, model: ApiModel, records: Mapping[str, Sequence[WorldRecord]]) -> World:
        """Build a world from in-memory records (collections left out are empty).

        Raises:
            WorldError: ``records`` names an unknown collection, or holds duplicate or
                inconsistent keys.
        """
        world: World = cls.__new__(cls)
        world._setup(model, records)
        return world

    def _setup(self, model: ApiModel, records: Mapping[str, Sequence[WorldRecord]]) -> None:
        names = [collection.name for collection in model.collections]
        unknown = sorted(set(records) - set(names))
        if unknown:
            raise WorldError(f"records given for unknown collections: {', '.join(unknown)}")
        self.model = model
        self._bases = {c.name: _Base.build(c, records.get(c.name, ())) for c in model.collections}
        self._sessions: dict[str, SessionWorld] = {}

    def session(self, session_id: str = "default") -> SessionWorld:
        """The session called ``session_id`` (created on first use, at the seed)."""
        session = self._sessions.get(session_id)
        if session is None:
            session = self._sessions[session_id] = SessionWorld(session_id, self._bases)
        return session

    def sessions(self) -> list[str]:
        """Ids of the sessions created so far, sorted."""
        return sorted(self._sessions)

    def reset(self, session_id: str | None = None) -> None:
        """Return one session (or, with ``None``, every session) to the recorded seed."""
        if session_id is None:
            for session in self._sessions.values():
                session.reset()
        elif session_id in self._sessions:
            self._sessions[session_id].reset()

    def drop(self, session_id: str) -> bool:
        """Forget a session and its changes. Returns whether it existed."""
        return self._sessions.pop(session_id, None) is not None

    def base_records(self, collection: str) -> list[WorldRecord]:
        """Copies of the recorded records of ``collection``, in base order."""
        return [copy_record(record) for record in _lookup(self._bases, collection).records]

    def save_state(self, path: Path) -> None:
        """Atomically write every session's overlay to ``path`` (a :class:`WorldState`)."""
        sessions: dict[str, JsonValue] = {
            session_id: self._sessions[session_id].snapshot()
            for session_id in sorted(self._sessions)
        }
        write_json(path, model_to_json(WorldState(format_version=STATE_FORMAT, sessions=sessions)))

    def load_state(self, path: Path) -> None:
        """Restore every session from a :meth:`save_state` file.

        Sessions in the file get its overlay; other existing sessions are reset. Nothing changes
        unless the whole file is valid.

        Raises:
            WorldError: The file is missing or invalid.
        """
        try:
            state = WorldState.model_validate(read_json(path))
        except (OSError, ValueError) as exc:  # ValidationError and JSON errors are ValueErrors
            raise WorldError(f"cannot load world state from {path}: {exc}") from exc
        for session_id, snapshot in state.sessions.items():
            SessionWorld(session_id, self._bases).restore(snapshot)  # validate before applying
        for session_id, session in self._sessions.items():
            if session_id not in state.sessions:
                session.reset()
        for session_id, snapshot in state.sessions.items():
            self.session(session_id).restore(snapshot)
