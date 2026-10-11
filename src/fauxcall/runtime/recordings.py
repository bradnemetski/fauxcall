"""Read-only access to recorded exchanges at runtime (replay, matching, streams, MCP).

Layout written by the recorder: ``data/recordings/<endpoint-slug>/<exchange-id>.json`` with binary
bodies in sidecar files next to them. Files that can't be read or don't validate are skipped with a
warning, so one bad recording never takes the whole fake down.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from fauxcall.logs import get_logger
from fauxcall.model.recording import Exchange
from fauxcall.storage import read_model

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.model.recording import Body

log = get_logger(__name__)


class RecordingIndex:
    """All exchanges of one fake, indexed by endpoint id (loaded lazily, once)."""

    def __init__(self, recordings_dir: Path) -> None:
        self._dir = recordings_dir
        self._by_endpoint: dict[str, list[Exchange]] | None = None
        self._by_id: dict[str, Exchange] = {}
        self._paths: dict[str, Path] = {}

    def _load(self) -> dict[str, list[Exchange]]:
        if self._by_endpoint is not None:
            return self._by_endpoint
        grouped: defaultdict[str, list[Exchange]] = defaultdict(list)
        if self._dir.is_dir():
            for path in sorted(self._dir.rglob("*.json")):
                if not path.is_file():
                    continue
                try:
                    exchange = read_model(path, Exchange)
                except (OSError, ValueError) as exc:  # unreadable, invalid JSON, invalid model
                    log.warning("recording_unreadable", path=str(path), error=str(exc)[:200])
                    continue
                grouped[exchange.endpoint].append(exchange)
                self._by_id[exchange.id] = exchange
                self._paths[exchange.id] = path
        for exchanges in grouped.values():
            exchanges.sort(key=lambda e: (e.started_ns, e.id))
        self._by_endpoint = dict(grouped)
        return self._by_endpoint

    def for_endpoint(self, endpoint_id: str) -> list[Exchange]:
        """Exchanges recorded for an endpoint, oldest first."""
        return list(self._load().get(endpoint_id, []))

    def get(self, exchange_id: str) -> Exchange | None:
        """One exchange by id."""
        self._load()
        return self._by_id.get(exchange_id)

    def endpoints(self) -> list[str]:
        """Endpoint ids with at least one recording."""
        return sorted(self._load())

    def binary(self, exchange: Exchange, body: Body) -> bytes | None:
        """The bytes of a binary body stored in a sidecar file (``None`` if it is missing)."""
        self._load()
        if body.file is None or exchange.id not in self._paths:
            return None
        sidecar = self._paths[exchange.id].parent / body.file
        try:
            return sidecar.read_bytes()
        except OSError:
            log.warning("recording_sidecar_missing", path=str(sidecar), exchange=exchange.id)
            return None
