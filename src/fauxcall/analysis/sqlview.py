"""Ad-hoc SQL over recordings through a throwaway in-memory SQLite database.

Recordings stay JSON on disk (the source of truth); when an agent wants to slice thousands of them
("distinct statuses per endpoint", "which samples had state=processing"), this builds a temporary
database with SQLite's JSON functions available::

    SELECT endpoint, json_extract(response_json, '$.status') AS s, count(*)
    FROM exchanges GROUP BY 1, 2

The database is read-only once built: ``PRAGMA query_only`` is on and an authorizer refuses every
statement that is not a read, so ``WITH … DELETE`` and friends fail too.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fauxcall.model.recording import Body, Exchange
    from fauxcall.types import JsonValue

SCHEMA: Final = """
CREATE TABLE exchanges (
    id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    purpose TEXT,
    plan_call TEXT,
    tags TEXT,
    started_at TEXT,
    started_ns INTEGER,
    elapsed_ms REAL,
    method TEXT,
    path TEXT,
    query_json TEXT,
    request_json TEXT,
    status INTEGER,
    content_type TEXT,
    response_json TEXT,
    response_text TEXT,
    stream_events INTEGER
)
"""
_INSERT: Final = (
    "INSERT INTO exchanges VALUES (:id, :endpoint, :purpose, :plan_call, :tags, :started_at, "
    ":started_ns, :elapsed_ms, :method, :path, :query_json, :request_json, :status, "
    ":content_type, :response_json, :response_text, :stream_events)"
)
READ_ONLY_MESSAGE: Final = "only read-only queries (SELECT / WITH / EXPLAIN) are allowed"
_LEADING_NOISE: Final = re.compile(r"(?:\s+|--[^\n]*|/\*.*?\*/)*", re.DOTALL)
_READ_STATEMENT: Final = re.compile(r"(?:select|with|explain)\b|pragma\s+table_x?info\b")
_READ_ACTIONS: Final = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
_READ_PRAGMAS: Final = frozenset({"table_info", "table_xinfo"})


def _allows(action: int, arg1: str | None) -> bool:
    """Whether an authorizer action only reads."""
    if action == sqlite3.SQLITE_PRAGMA:
        return (arg1 or "").lower() in _READ_PRAGMAS
    return action in _READ_ACTIONS


def _read_only(
    action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None
) -> int:
    """SQLite authorizer that refuses everything but reads."""
    del arg2, database, trigger
    return sqlite3.SQLITE_OK if _allows(action, arg1) else sqlite3.SQLITE_DENY


def _json_text(body: Body | None) -> str | None:
    """A JSON body re-serialized (float spellings kept); SQL NULL for every other kind of body."""
    if body is None or body.kind != "json":
        return None
    value: JsonValue = body.json_value
    return jsonfmt.dumps(value)


def build(exchanges: Iterable[Exchange]) -> sqlite3.Connection:
    """An in-memory, read-only database with one row per exchange."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(SCHEMA)
    rows = []
    for exchange in exchanges:
        response = exchange.response
        body = response.body if response is not None else None
        rows.append(
            {
                "id": exchange.id,
                "endpoint": exchange.endpoint,
                "purpose": exchange.purpose,
                "plan_call": exchange.plan_call,
                "tags": ",".join(exchange.tags),
                "started_at": exchange.started_at.isoformat(),
                "started_ns": exchange.started_ns,
                "elapsed_ms": exchange.elapsed_ms,
                "method": exchange.request.method.value,
                "path": exchange.request.path,
                "query_json": jsonfmt.dumps([list(pair) for pair in exchange.request.query]),
                "request_json": _json_text(exchange.request.body),
                "status": response.status if response is not None else None,
                "content_type": response.header("content-type") if response is not None else None,
                "response_json": _json_text(body),
                "response_text": body.text if body is not None and body.kind == "text" else None,
                "stream_events": len(exchange.stream),
            }
        )
    connection.executemany(_INSERT, rows)
    connection.commit()
    connection.execute("PRAGMA query_only = ON")
    connection.set_authorizer(_read_only)
    return connection


def query(connection: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
    """Run one read-only statement; returns column names and rows.

    Raises:
        ValueError: The statement is not a read (``SELECT`` / ``WITH`` / ``EXPLAIN`` /
            ``PRAGMA table_info``) or tries to change anything.
        sqlite3.Error: The statement is invalid (unknown column, syntax error …).
    """
    statement = sql.strip().rstrip(";")
    head = _LEADING_NOISE.sub("", statement, count=1).lower()
    if not _READ_STATEMENT.match(head):
        raise ValueError(READ_ONLY_MESSAGE)
    denied: list[int] = []

    def authorizer(
        action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None
    ) -> int:
        verdict = _read_only(action, arg1, arg2, database, trigger)
        if verdict != sqlite3.SQLITE_OK:
            denied.append(action)
        return verdict

    connection.set_authorizer(authorizer)
    try:
        cursor = connection.execute(statement)
        rows = [tuple(row) for row in cursor.fetchall()]
    except sqlite3.DatabaseError as exc:
        if denied:
            raise ValueError(READ_ONLY_MESSAGE) from exc
        raise
    finally:
        connection.set_authorizer(_read_only)
    columns = [description[0] for description in cursor.description or []]
    return columns, rows


def as_table(columns: list[str], rows: list[tuple[object, ...]], *, max_width: int = 60) -> str:
    """Render a result as a markdown table (cells truncated)."""

    def cell(value: object) -> str:
        text = "" if value is None else str(value).replace("\n", " ").replace("|", "\\|")
        return text if len(text) <= max_width else text[: max_width - 1] + "…"

    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "---|" * len(columns)
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])
