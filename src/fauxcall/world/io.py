"""Collection files and record construction.

Each collection is a JSONL file (``data/world/<name>.jsonl``, see ``CollectionModel.path``) with one
:class:`~fauxcall.model.world.WorldRecord` per line. Files go through :mod:`fauxcall.storage`, so
they are written atomically and float spellings (``105.50``) survive a round trip.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from fauxcall.errors import WorldError
from fauxcall.logs import get_logger
from fauxcall.model.world import WorldRecord
from fauxcall.storage import model_to_json, read_jsonl, write_jsonl
from fauxcall.world.keys import record_key

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from pathlib import Path

    from fauxcall.model.world import CollectionModel
    from fauxcall.types import JsonValue
    from fauxcall.world.keys import Key

_log = get_logger(__name__)
_MAX_LOGGED_KEYS = 20


def copy_json(value: JsonValue) -> JsonValue:
    """A deep copy of a JSON value (several times faster than :func:`copy.deepcopy`).

    Scalars are shared: they are immutable, :class:`~fauxcall.jsonfmt.JsonFloat` included.
    """
    if isinstance(value, dict):
        return {key: copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_json(item) for item in value]
    return value


def copy_record(record: WorldRecord) -> WorldRecord:
    """A copy of ``record`` whose payload can be mutated without touching the original."""
    return record.model_copy(update={"record": copy_json(record.record)})


def load_collection(path: Path) -> list[WorldRecord]:
    """Read a collection file. A missing file is an empty collection.

    Raises:
        WorldError: A line is not valid JSON or not a valid world record (names ``file:line``).
    """
    records: list[WorldRecord] = []
    try:
        for value in read_jsonl(path):
            records.append(WorldRecord.model_validate(value))  # noqa: PERF401 - len() locates errors
    except ValueError as exc:  # invalid JSON, NaN/Infinity, or a pydantic ValidationError
        line = _line_number(path, len(records))
        raise WorldError(f"{path}:{line}: invalid world record: {_reason(exc)}") from exc
    return records


def _line_number(path: Path, index: int) -> int | str:
    """The line holding the ``index``-th value (``read_jsonl`` skips blank lines)."""
    with path.open(encoding="utf-8") as handle:
        numbers = (number for number, line in enumerate(handle, start=1) if line.strip())
        return next(itertools.islice(numbers, index, None), "?")


def _reason(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        problems = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            problems.append(f"{location}: {error['msg']}" if location else error["msg"])
        return "; ".join(problems)
    if isinstance(exc.__cause__, json.JSONDecodeError):
        return f"invalid JSON ({exc.__cause__.msg})"
    return str(exc)


def save_collection(path: Path, records: Iterable[WorldRecord]) -> None:
    """Atomically write a collection file: one compact record per line, defaults omitted."""
    write_jsonl(path, (model_to_json(record) for record in records))


def records_from_values(
    collection: CollectionModel,
    values: Iterable[JsonValue],
    *,
    recorded_at: datetime | None = None,
    source: str | None = None,
) -> list[WorldRecord]:
    """Turn recorded values (e.g. the items of list responses) into world records.

    Keys are extracted from each value at ``collection.key`` and normalized. When several values
    share a key, the record of the *last* one is kept (later recordings are fresher) at the
    position of the *first* one, and a warning is logged. Values are copied.

    Raises:
        WorldError: A value has no key, or an invalid one.
    """
    records: dict[Key, WorldRecord] = {}
    duplicates: dict[Key, None] = {}
    for value in values:
        key = record_key(collection, value)
        if key in records:
            duplicates[key] = None
        records[key] = WorldRecord(
            key=key, record=copy_json(value), recorded_at=recorded_at, source=source
        )
    if duplicates:
        _log.warning(
            "duplicate world keys; keeping the last occurrence of each",
            collection=collection.name,
            count=len(duplicates),
            keys=list(duplicates)[:_MAX_LOGGED_KEYS],
        )
    return list(records.values())
