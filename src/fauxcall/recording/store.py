"""On-disk storage of recordings: one JSON file per exchange, binary bodies in sidecar files.

``data/recordings/<endpoint-slug>/<exchange-id>.json`` (+ ``<exchange-id>.<part>.bin``). Ids are a
zero-padded counter across the whole fake so recordings sort chronologically and diff cleanly; a
new store continues after the highest id already on disk. Two endpoints whose slugs collide get
separate directories (the later one gets a hash suffix), whether the first one is already on disk
or was only claimed earlier in this process.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fauxcall.logs import get_logger
from fauxcall.model.recording import Exchange
from fauxcall.paths import endpoint_slug, unique_endpoint_slug
from fauxcall.storage import read_model, write_model

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

log = get_logger(__name__)

ID_WIDTH: Final = 6
_ID_RE: Final = re.compile(r"^(\d+)\.json$")
_SIDECAR_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _exchange_files(directory: Path, *, recursive: bool) -> list[Path]:
    """Exchange files (``<digits>.json``) in id order (numeric, so ids wider than 6 digits sort)."""
    paths = directory.rglob("*.json") if recursive else directory.glob("*.json")
    found = [(int(match.group(1)), path) for path in paths if (match := _ID_RE.match(path.name))]
    return [path for _, path in sorted(found, key=lambda item: (item[0], str(item[1])))]


class RecordingStore:
    """Reads and writes exchanges of one fake."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._next = self._scan_next_id()
        self._dirs: dict[str, Path] = {}

    def _scan_next_id(self) -> int:
        highest = 0
        if self.root.is_dir():
            for path in self.root.rglob("*.json"):
                match = _ID_RE.match(path.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest + 1

    def next_id(self) -> str:
        """Reserve the next exchange id."""
        value = f"{self._next:0{ID_WIDTH}d}"
        self._next += 1
        return value

    def directory(self, endpoint_id: str) -> Path:
        """The directory for an endpoint's exchanges (collision-safe)."""
        if endpoint_id in self._dirs:
            return self._dirs[endpoint_id]
        candidate = self.root / endpoint_slug(endpoint_id)
        if self._taken(candidate, endpoint_id):
            candidate = self.root / unique_endpoint_slug(endpoint_id)
        self._dirs[endpoint_id] = candidate
        return candidate

    def _taken(self, directory: Path, endpoint_id: str) -> bool:
        """Whether another endpoint owns ``directory`` (claimed in this process or on disk)."""
        if any(path == directory for other, path in self._dirs.items() if other != endpoint_id):
            return True
        owner = self._owner(directory)
        return owner is not None and owner != endpoint_id

    @staticmethod
    def _owner(directory: Path) -> str | None:
        """The endpoint of the first readable exchange stored in ``directory``."""
        if not directory.is_dir():
            return None
        for path in _exchange_files(directory, recursive=False):
            try:
                return read_model(path, Exchange).endpoint
            except (OSError, ValueError) as exc:
                log.warning("recording_unreadable", path=str(path), error=str(exc)[:200])
        return None

    def save(self, exchange: Exchange, sidecars: Mapping[str, bytes] | None = None) -> Path:
        """Write an exchange (and its binary sidecars); returns the JSON path.

        Sidecars are written first, so an exchange file never points at a missing sidecar. Their
        names must be plain file names (see :meth:`sidecar_name`).
        """
        directory = self.directory(exchange.endpoint)
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in (sidecars or {}).items():
            if not _SIDECAR_RE.match(name) or _ID_RE.match(name):
                raise ValueError(f"invalid sidecar file name {name!r}")
            (directory / name).write_bytes(content)
        path = directory / f"{exchange.id}.json"
        write_model(path, exchange)
        return path

    def iter_exchanges(self) -> Iterator[tuple[Path, Exchange]]:
        """Every stored exchange, in id order."""
        if not self.root.is_dir():
            return
        for path in _exchange_files(self.root, recursive=True):
            yield path, read_model(path, Exchange)

    def for_endpoint(self, endpoint_id: str) -> list[Exchange]:
        """Exchanges of one endpoint, in id order."""
        directory = self.directory(endpoint_id)
        if not directory.is_dir():
            return []
        found = [read_model(p, Exchange) for p in _exchange_files(directory, recursive=False)]
        return [exchange for exchange in found if exchange.endpoint == endpoint_id]

    def delete(self, path: Path, exchange: Exchange) -> None:
        """Remove one stored exchange and its binary sidecars."""
        for sidecar in path.parent.glob(f"{exchange.id}.*.bin"):
            sidecar.unlink()
        path.unlink()

    def sidecar_name(self, exchange_id: str, part: str) -> str:
        """File name for a binary body part."""
        return f"{exchange_id}.{part}.bin"
