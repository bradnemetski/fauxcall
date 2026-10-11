"""In-memory state: watchlists (CRUD) and uploads (async jobs whose status derives from time).

State lives for the life of the process and is shared by every API key.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from fauxcall_reference_api.clock import NS_PER_SECOND

FIRST_WATCHLIST_ID: Final = 1001
QUEUED_NS: Final = 2 * NS_PER_SECOND
PROCESSING_NS: Final = 6 * NS_PER_SECOND
CORRUPT_MARKER: Final = b"CORRUPT"
_PERCENT: Final = 100

type UploadStatus = Literal["queued", "processing", "done", "failed"]


@dataclass(slots=True)
class Watchlist:
    """A named list of canonical ticker symbols."""

    id: int
    name: str
    symbols: list[str]
    created_ns: int
    updated_ns: int


class WatchlistStore:
    """Watchlists by id. Ids start at 1001, increase by one and are never reused."""

    def __init__(self) -> None:
        self._items: dict[int, Watchlist] = {}
        self._ids = itertools.count(FIRST_WATCHLIST_ID)

    def create(self, name: str, symbols: list[str], now_ns: int) -> Watchlist:
        """Store a new watchlist."""
        watchlist = Watchlist(next(self._ids), name, symbols, now_ns, now_ns)
        self._items[watchlist.id] = watchlist
        return watchlist

    def all(self) -> list[Watchlist]:
        """Every watchlist, by id."""
        return [self._items[key] for key in sorted(self._items)]

    def get(self, watchlist_id: int) -> Watchlist | None:
        """One watchlist, if it exists."""
        return self._items.get(watchlist_id)

    def delete(self, watchlist_id: int) -> bool:
        """Remove a watchlist; ``False`` if it did not exist."""
        return self._items.pop(watchlist_id, None) is not None


@dataclass(frozen=True, slots=True)
class UploadView:
    """An upload's state as observed at one instant."""

    status: UploadStatus
    progress: int
    completed_ns: int | None
    error: Literal["corrupt_file"] | None


@dataclass(slots=True)
class Upload:
    """A stored upload. Its status is a pure function of the time elapsed since creation."""

    id: str
    filename: str
    content_type: str
    content: bytes
    label: str | None
    created_ns: int
    sequence: int

    @property
    def size(self) -> int:
        """Size of the uploaded file in bytes."""
        return len(self.content)

    @property
    def corrupt(self) -> bool:
        """Whether processing will fail (the bytes contain ``CORRUPT``)."""
        return CORRUPT_MARKER in self.content

    def view(self, now_ns: int) -> UploadView:
        """``queued`` for 2 s, ``processing`` for 6 s (progress 0-99), then ``done``/``failed``."""
        elapsed = now_ns - self.created_ns
        if elapsed < QUEUED_NS:
            return UploadView("queued", 0, None, None)
        if elapsed < QUEUED_NS + PROCESSING_NS:
            progress = (elapsed - QUEUED_NS) * _PERCENT // PROCESSING_NS
            return UploadView("processing", progress, None, None)
        completed_ns = self.created_ns + QUEUED_NS + PROCESSING_NS
        if self.corrupt:
            return UploadView("failed", _PERCENT, completed_ns, "corrupt_file")
        return UploadView("done", _PERCENT, completed_ns, None)


class UploadStore:
    """Uploads by id (UUID4)."""

    def __init__(self) -> None:
        self._items: dict[str, Upload] = {}
        self._sequence = itertools.count(1)

    def create(
        self, *, filename: str, content_type: str, content: bytes, label: str | None, now_ns: int
    ) -> Upload:
        """Store a new upload (status ``queued``)."""
        upload = Upload(
            id=str(uuid.uuid4()),
            filename=filename,
            content_type=content_type,
            content=content,
            label=label,
            created_ns=now_ns,
            sequence=next(self._sequence),
        )
        self._items[upload.id] = upload
        return upload

    def all(self) -> list[Upload]:
        """Every upload, newest first (ties: most recently created first)."""
        return sorted(self._items.values(), key=lambda u: (u.created_ns, u.sequence), reverse=True)

    def get(self, upload_id: str) -> Upload | None:
        """One upload, if it exists."""
        return self._items.get(upload_id)

    def delete(self, upload_id: str) -> bool:
        """Remove an upload; ``False`` if it did not exist."""
        return self._items.pop(upload_id, None) is not None
