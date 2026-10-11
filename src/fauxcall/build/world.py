"""Build the world (collections of records) from recordings.

For each collection, every successful recording of its source endpoints (with an allowed purpose)
contributes the records found at the endpoint's extraction path (``$`` by default; an array found
there contributes its items). A record seen several times keeps its first position (recorded order)
and its latest content (fresher). The result is written to ``data/world/<collection>.jsonl``, then
checked against relations and invariants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fauxcall import jsonpath
from fauxcall.errors import WorldError
from fauxcall.logs import get_logger
from fauxcall.model.world import WorldRecord
from fauxcall.world.io import save_collection
from fauxcall.world.keys import record_key

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from fauxcall.model.api import ApiModel
    from fauxcall.model.recording import Exchange
    from fauxcall.model.world import CollectionModel
    from fauxcall.types import JsonValue

log = get_logger(__name__)


@dataclass(slots=True)
class CollectionBuild:
    """What went into one collection."""

    name: str
    records: list[WorldRecord] = field(default_factory=list)
    exchanges_used: int = 0
    skipped_without_key: int = 0
    replaced: int = 0


def _records(
    collection: CollectionModel, body: JsonValue, path: str, where: str
) -> list[JsonValue]:
    """The records at ``path`` in a response body (arrays there contribute their items).

    Records are objects — or scalars, for a collection whose key is the record itself (``$``).
    """
    try:
        values = jsonpath.get_all(body, path)
    except ValueError as exc:
        raise WorldError(
            f"collection {collection.name!r}: invalid extract path {path!r} for {where}: {exc}"
        ) from exc
    scalars = collection.key.strip() in {"$", ""}

    def is_record(item: JsonValue) -> bool:
        if isinstance(item, dict):
            return True
        return scalars and item is not None and not isinstance(item, list)

    items = [item for value in values for item in (value if isinstance(value, list) else [value])]
    return [item for item in items if is_record(item)]


def build_collection(collection: CollectionModel, exchanges: Iterable[Exchange]) -> CollectionBuild:
    """Collect one collection's records from recordings.

    Raises:
        WorldError: An extract path is not valid JSON-path syntax.
    """
    result = CollectionBuild(name=collection.name)
    sources = set(collection.sources) | set(collection.extract)
    allowed = set(collection.purposes)
    positions: dict[object, int] = {}
    ordered = sorted(exchanges, key=lambda e: (e.started_ns, e.id))
    for exchange in ordered:
        response = exchange.response
        if exchange.endpoint not in sources or exchange.purpose not in allowed or response is None:
            continue
        if not 200 <= response.status < 300:  # noqa: PLR2004
            continue
        body = response.body.parsed()
        if body is None:
            continue
        path = collection.extract.get(exchange.endpoint, "$")
        values = _records(collection, body, path, exchange.endpoint)
        result.exchanges_used += 1
        for value in values:
            try:
                key = record_key(collection, value)
            except WorldError:
                result.skipped_without_key += 1
                continue
            record = WorldRecord(
                key=key, record=value, recorded_at=exchange.started_at, source=exchange.id
            )
            if key in positions:
                result.records[positions[key]] = record
                result.replaced += 1
            else:
                positions[key] = len(result.records)
                result.records.append(record)
    return result


def build_world(model: ApiModel, exchanges: Iterable[Exchange]) -> dict[str, CollectionBuild]:
    """Build every collection of the model."""
    materialized = list(exchanges)
    return {
        collection.name: build_collection(collection, materialized)
        for collection in model.collections
    }


def write_world(data_dir: Path, model: ApiModel, builds: dict[str, CollectionBuild]) -> list[Path]:
    """Write the built collections (only those that were built)."""
    written: list[Path] = []
    for collection in model.collections:
        build = builds.get(collection.name)
        if build is None:
            continue
        path = data_dir / collection.path
        save_collection(path, build.records)
        written.append(path)
        log.info("collection_written", collection=collection.name, records=len(build.records))
    return written


def summary(builds: dict[str, CollectionBuild]) -> str:
    """Markdown table of the build."""
    lines = [
        "| collection | records | from exchanges | replaced (fresher) | without key |",
        "|---|---|---|---|---|",
    ]
    for name, build in builds.items():
        cells = [name, len(build.records), build.exchanges_used, build.replaced]
        cells.append(build.skipped_without_key)
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    return "\n".join(lines)
