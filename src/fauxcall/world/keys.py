"""Primary keys of world records.

A key has one canonical form per collection, so a lookup does not depend on how the value arrived:
path parameters are strings (``"42"``), recorded records usually hold numbers (``42``), and UUIDs
come in either case. :func:`normalize_key` maps all of them to the same value, and the helpers
below read and write a collection's key field (``CollectionModel.key``, a :mod:`fauxcall.jsonpath`
path) inside a record.
"""

from __future__ import annotations

import functools
import re
import uuid
from typing import TYPE_CHECKING, Final

from fauxcall import jsonpath
from fauxcall.errors import WorldError

if TYPE_CHECKING:
    from fauxcall.model.world import CollectionModel, KeyType
    from fauxcall.types import JsonValue

type Key = int | str
"""A normalized key: ``int`` in ``"int"`` collections, ``str`` in ``"str"`` and ``"uuid"`` ones."""

_INT_KEY: Final = re.compile(r"-?[0-9]{1,100}")


class InvalidKeyError(WorldError):
    """A value cannot be a key of the collection (``"abc"`` for integer ids, a malformed UUID …)."""


class DuplicateKeyError(WorldError):
    """A record with the same key already exists in the collection."""


@functools.lru_cache(maxsize=1024)
def parse_path(path: str) -> tuple[jsonpath.Segment, ...]:
    """Parse a :mod:`fauxcall.jsonpath` field path (cached).

    Raises:
        WorldError: The path is not valid syntax.
    """
    try:
        segments = jsonpath.parse(path)
    except ValueError as exc:
        raise WorldError(str(exc)) from exc
    return segments


def normalize_key(value: JsonValue, key_type: KeyType) -> Key:
    """The canonical form of a key, used for every lookup.

    * ``"int"``: integers, integral floats (``42.0``) and integer strings of up to 100 digits
      (``"42"``, ``"-7"``, ``"007"``; no ``+`` or whitespace) become an ``int``.
    * ``"uuid"``: any spelling :class:`uuid.UUID` accepts becomes the lowercase hyphenated form.
    * ``"str"``: strings are kept as they are; integers are stringified.

    Raises:
        InvalidKeyError: The value cannot be a key of this type (booleans, fractions, ``None``,
            arrays, objects, malformed strings).
    """
    key: Key | None
    if key_type == "int":
        key = _int_key(value)
    elif key_type == "uuid":
        key = _uuid_key(value)
    else:
        key = _str_key(value)
    if key is None:
        raise InvalidKeyError(f"{value!r} is not a valid {key_type} key")
    return key


def _int_key(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and _INT_KEY.fullmatch(value):
        return int(value)
    return None


def _uuid_key(value: JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return str(parsed)


def _str_key(value: JsonValue) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def collection_key(collection: CollectionModel, value: JsonValue) -> Key:
    """:func:`normalize_key` with the collection's key type (errors name the collection).

    Raises:
        InvalidKeyError: The value cannot be a key of this collection.
    """
    try:
        key = normalize_key(value, collection.key_type)
    except InvalidKeyError as exc:
        raise InvalidKeyError(f"collection {collection.name!r}: {exc}") from exc
    return key


def payload_key(collection: CollectionModel, record: JsonValue) -> Key | None:
    """The normalized key stored inside ``record``, or ``None`` when it is absent or null.

    Raises:
        InvalidKeyError: The key field holds a value that cannot be a key.
    """
    matches = jsonpath.iter_matches(record, parse_path(collection.key))
    value = next((match for _, match in matches), None)
    if value is None:
        return None
    try:
        key = normalize_key(value, collection.key_type)
    except InvalidKeyError as exc:
        raise InvalidKeyError(
            f"collection {collection.name!r}: {value!r} at {collection.key!r} "
            f"is not a valid {collection.key_type} key"
        ) from exc
    return key


def record_key(collection: CollectionModel, record: JsonValue) -> Key:
    """Extract and normalize the key of ``record`` (at ``collection.key``).

    Raises:
        WorldError: The key field is missing or null.
        InvalidKeyError: The key field holds a value that cannot be a key.
    """
    key = payload_key(collection, record)
    if key is None:
        raise WorldError(
            f"collection {collection.name!r}: record has no key at {collection.key!r} "
            "(missing or null)"
        )
    return key


def set_key(collection: CollectionModel, record: JsonValue, key: JsonValue) -> JsonValue:
    """Write ``key`` into ``record`` at the key path, in place, creating missing parent objects.

    Returns:
        The record — or ``key`` itself when the key path is the root (``$``), i.e. the records of
        the collection are the keys.

    Raises:
        WorldError: The key path goes through something that is not an object (or uses indexes
            or wildcards).
    """
    segments = parse_path(collection.key)
    if not segments:
        return key
    node = record
    for segment in segments[:-1]:
        node = _child_object(collection, node, segment)
    last = segments[-1]
    if not isinstance(node, dict) or not isinstance(last, str):
        raise _unwritable(collection)
    node[last] = key
    return record


def _child_object(
    collection: CollectionModel, node: JsonValue, segment: jsonpath.Segment
) -> JsonValue:
    if not isinstance(node, dict) or not isinstance(segment, str):
        raise _unwritable(collection)
    if node.get(segment) is None:
        node[segment] = {}
    return node[segment]


def _unwritable(collection: CollectionModel) -> WorldError:
    return WorldError(f"collection {collection.name!r}: cannot write a key at {collection.key!r}")


def ensure_key(collection: CollectionModel, record: JsonValue, key: Key) -> JsonValue:
    """Make ``record`` carry ``key``: write it (in place) when absent or null, refuse another key.

    Returns:
        The record (see :func:`set_key` for the root-path case).

    Raises:
        WorldError: The record holds a different key.
        InvalidKeyError: The record's key field holds a value that cannot be a key.
    """
    found = payload_key(collection, record)
    if found is None:
        return set_key(collection, record, key)
    if found != key:
        raise WorldError(
            f"collection {collection.name!r}: cannot change the key of record {key!r} to {found!r}"
        )
    return record
