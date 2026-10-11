"""The call journal: every request an agent made, queryable through the admin API.

Evaluations assert on agent *behavior* ("did it poll the upload status before downloading?"), so the
fake keeps a bounded, redacted log of requests and responses per session.
"""

from __future__ import annotations

import codecs
import itertools
from collections import deque
from typing import TYPE_CHECKING, Final

from pydantic import AwareDatetime, Field

from fauxcall import jsonfmt
from fauxcall.logs import REDACTED, is_secret_key
from fauxcall.model.common import Model, RawJson

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

BODY_LIMIT: Final = 64 * 1024


class JournalEntry(Model):
    """One request/response pair seen by the fake."""

    seq: int
    session: str
    request_id: str
    at: AwareDatetime = Field(description="The fake clock's time when the request arrived.")
    method: str
    path: str
    query: list[tuple[str, str]] = Field(default_factory=list)
    endpoint: str | None = None
    request_headers: list[tuple[str, str]] = Field(default_factory=list)
    request_body: RawJson = None
    request_text: str | None = None
    status: int | None = None
    response_headers: list[tuple[str, str]] = Field(default_factory=list)
    response_body: RawJson = None
    response_text: str | None = None
    streamed: bool = False
    duration_ms: float = 0.0
    fault: str | None = None
    identity: str | None = None
    mcp_method: str | None = Field(default=None, description="JSON-RPC method for MCP calls.")


def redact_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Blank out credential-looking header/query values."""
    return [(name, REDACTED if is_secret_key(name) else value) for name, value in pairs]


def body_for_journal(raw: bytes, content_type: str | None) -> tuple[RawJson, str | None]:
    """Decode a body for the journal: ``(json, None)`` for JSON, else ``(None, text)``.

    JSON bodies (by content type) are parsed unless they exceed :data:`BODY_LIMIT`; longer text is
    cut at the limit (never inside a UTF-8 character) and marked with ``…``; non-UTF-8 content is
    summarized as ``<n bytes of binary data>``.
    """
    if not raw:
        return None, None
    truncated = len(raw) > BODY_LIMIT
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # Not final when truncated: a character split by the cut is dropped, not an error.
        text = decoder.decode(raw[:BODY_LIMIT], final=not truncated)
    except UnicodeDecodeError:
        return None, f"<{len(raw)} bytes of binary data>"
    if not truncated and content_type and "json" in content_type.lower() and not _too_deep(text):
        try:
            return jsonfmt.loads(text), None
        except (ValueError, RecursionError):
            pass
    return None, text + ("…" if truncated else "")


MAX_JSON_DEPTH = 256
"""Deeper JSON is journaled as text: nesting that deep only strains whatever walks it later."""


def _too_deep(text: str) -> bool:
    """Whether brackets nest past :data:`MAX_JSON_DEPTH` (outside strings); no parsing needed."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif char in "]}":
            depth -= 1
    return False


class Journal:
    """A bounded in-memory journal shared by all sessions."""

    def __init__(self, limit: int) -> None:
        self._entries: deque[JournalEntry] = deque(maxlen=max(0, limit))
        self._seq = itertools.count(1)
        self.enabled = limit > 0

    def next_seq(self) -> int:
        """Reserve the next sequence number."""
        return next(self._seq)

    def add(self, entry: JournalEntry) -> None:
        """Store an entry (dropping the oldest when full)."""
        if self.enabled:
            self._entries.append(entry)

    def query(
        self,
        *,
        session: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        path_prefix: str | None = None,
        since_seq: int | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Entries matching every given filter, oldest first (``limit`` keeps the newest)."""
        selected = [
            entry
            for entry in self._entries
            if (session is None or entry.session == session)
            and (endpoint is None or entry.endpoint == endpoint)
            and (method is None or entry.method.upper() == method.upper())
            and (path_prefix is None or entry.path.startswith(path_prefix))
            and (since_seq is None or entry.seq > since_seq)
        ]
        if limit is not None:
            selected = selected[-limit:] if limit > 0 else []
        return selected

    def clear(self, session: str | None = None) -> int:
        """Remove entries (of one session, or all). Returns how many were removed."""
        before = len(self._entries)
        if session is None:
            self._entries.clear()
        else:
            kept: Sequence[JournalEntry] = [e for e in self._entries if e.session != session]
            self._entries.clear()
            self._entries.extend(kept)
        return before - len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
