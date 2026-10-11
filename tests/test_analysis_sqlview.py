from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from fauxcall import jsonfmt
from fauxcall.analysis.sqlview import READ_ONLY_MESSAGE, as_table, build, query
from fauxcall.model.recording import (
    Body,
    CapturedRequest,
    CapturedResponse,
    Exchange,
    StreamEvent,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from collections.abc import Iterator

RECORDED = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


def exchange(
    number: int,
    endpoint: str,
    *,
    request_body: Body | None = None,
    response: CapturedResponse | None = None,
    query_params: list[tuple[str, str]] | None = None,
    tags: list[str] | None = None,
    stream: list[StreamEvent] | None = None,
) -> Exchange:
    method, path = endpoint.split(" ", 1)
    return Exchange(
        id=f"{number:06d}",
        endpoint=endpoint,
        plan_call=f"call{number}",
        started_at=RECORDED,
        started_ns=1_791_000_000_000_000_000 + number,
        elapsed_ms=12.5,
        request=CapturedRequest(
            method=HttpMethod(method),
            url=f"https://api.test{path}",
            path=path,
            query=query_params or [],
            body=request_body or Body(),
        ),
        response=response,
        stream=stream or [],
        tags=tags or [],
    )


EXCHANGES = [
    exchange(
        1,
        "GET /v1/quote",
        query_params=[("symbol", "AAPL"), ("symbol", "MSFT")],
        tags=["sample", "poll:x"],
        response=CapturedResponse(
            status=200,
            headers=[("Content-Type", "application/json")],
            body=Body(kind="json", json_value=jsonfmt.loads('{"symbol":"AAPL","price":105.50}')),
        ),
    ),
    exchange(
        2,
        "POST /v1/orders",
        request_body=Body(kind="json", json_value={"qty": 2}),
        response=CapturedResponse(
            status=201, body=Body(kind="text", content_type="text/plain", text="Created")
        ),
    ),
    exchange(
        3,
        "GET /v1/stream",
        response=CapturedResponse(status=200),
        stream=[
            StreamEvent(t_ms=1.0, kind="sse", data="a"),
            StreamEvent(t_ms=2.0, kind="sse", data="b"),
        ],
    ),
    exchange(4, "GET /v1/broken"),
    exchange(
        5,
        "GET /v1/logo",
        request_body=Body(kind="text", text="ignored"),
        response=CapturedResponse(
            status=200, body=Body(kind="binary", content_type="image/png", file="000005.bin")
        ),
    ),
]


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    built = build(EXCHANGES)
    yield built
    built.close()


def test_one_row_per_exchange(connection: sqlite3.Connection) -> None:
    columns, rows = query(connection, "SELECT * FROM exchanges ORDER BY id")
    assert columns == [
        "id",
        "endpoint",
        "purpose",
        "plan_call",
        "tags",
        "started_at",
        "started_ns",
        "elapsed_ms",
        "method",
        "path",
        "query_json",
        "request_json",
        "status",
        "content_type",
        "response_json",
        "response_text",
        "stream_events",
    ]
    assert rows[0] == (
        "000001",
        "GET /v1/quote",
        "sample",
        "call1",
        "sample,poll:x",
        "2026-10-09T15:00:00+00:00",
        1_791_000_000_000_000_001,
        12.5,
        "GET",
        "/v1/quote",
        '[["symbol","AAPL"],["symbol","MSFT"]]',
        None,
        200,
        "application/json",
        '{"symbol":"AAPL","price":105.50}',
        None,
        0,
    )
    by_id = {row[0]: dict(zip(columns, row, strict=True)) for row in rows}
    assert by_id["000002"]["request_json"] == '{"qty":2}'
    assert by_id["000002"]["response_text"] == "Created"
    assert by_id["000002"]["response_json"] is None
    assert by_id["000002"]["content_type"] is None
    assert by_id["000003"]["stream_events"] == 2
    assert by_id["000004"]["status"] is None
    assert by_id["000004"]["response_json"] is None
    assert by_id["000005"]["request_json"] is None
    assert by_id["000005"]["response_text"] is None


def test_json_functions(connection: sqlite3.Connection) -> None:
    columns, rows = query(
        connection,
        "SELECT endpoint, json_extract(response_json, '$.price') AS price, count(*) AS n "
        "FROM exchanges WHERE response_json IS NOT NULL GROUP BY 1, 2",
    )
    assert columns == ["endpoint", "price", "n"]
    assert rows == [("GET /v1/quote", 105.5, 1)]
    _, nulls = query(connection, "SELECT count(*) FROM exchanges WHERE request_json IS NULL;")
    assert nulls == [(4,)]


@pytest.mark.parametrize(
    "sql",
    [
        "  -- why\n/* note */ SELECT 1",
        (
            "with recursive c(n) as (select 1 union all select n + 1 from c where n < 3) "
            "select count(*) from c"
        ),
        "EXPLAIN SELECT id FROM exchanges",
        "pragma  table_info(exchanges)",
        "PRAGMA table_xinfo(exchanges);",
        "SELECT name FROM sqlite_master",
    ],
)
def test_reads_are_allowed(connection: sqlite3.Connection, sql: str) -> None:
    columns, rows = query(connection, sql)
    assert columns
    assert rows


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM exchanges",
        "UPDATE exchanges SET status = 1",
        "DROP TABLE exchanges",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA query_only = OFF",
        "/* unterminated SELECT 1",
        "selection",
        "WITH gone AS (SELECT 1) DELETE FROM exchanges",
        "WITH x AS (SELECT 1) INSERT INTO exchanges (id, endpoint) VALUES ('9', 'GET /')",
        "with x as (select 1) update exchanges set status = 0",
        "EXPLAIN DELETE FROM exchanges",
    ],
)
def test_writes_are_refused(connection: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(ValueError, match="read-only") as refused:
        query(connection, sql)
    assert str(refused.value) == READ_ONLY_MESSAGE
    assert query(connection, "SELECT count(*), sum(status) FROM exchanges")[1] == [(5, 801)]


def test_invalid_sql_keeps_its_sqlite_error(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        query(connection, "SELECT nope FROM exchanges")
    with pytest.raises(sqlite3.ProgrammingError):
        query(connection, "SELECT 1; SELECT 2")
    assert query(connection, "SELECT 1")[1] == [(1,)]


def test_the_database_itself_is_read_only(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("DELETE FROM exchanges")
    query(connection, "SELECT 1")  # the strict authorizer is back after every query
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("INSERT INTO exchanges (id, endpoint) VALUES ('9', 'GET /')")
    connection.set_authorizer(None)
    assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("DELETE FROM exchanges")


def test_statements_without_results() -> None:
    empty = build([])
    assert query(empty, "SELECT * FROM exchanges") == (
        [
            "id",
            "endpoint",
            "purpose",
            "plan_call",
            "tags",
            "started_at",
            "started_ns",
            "elapsed_ms",
            "method",
            "path",
            "query_json",
            "request_json",
            "status",
            "content_type",
            "response_json",
            "response_text",
            "stream_events",
        ],
        [],
    )
    assert query(empty, "EXPLAIN QUERY PLAN SELECT 1")[0] == ["id", "parent", "notused", "detail"]


def test_as_table() -> None:
    table = as_table(
        ["endpoint", "note", "n"],
        [("GET /a", "pipe | and\nnewline", 1), ("GET /b", None, 2), ("GET /c", "x" * 70, 3)],
        max_width=20,
    ).splitlines()
    assert table == [
        "| endpoint | note | n |",
        "|---|---|---|",
        "| GET /a | pipe \\| and newline | 1 |",
        "| GET /b |  | 2 |",
        f"| GET /c | {'x' * 19}… | 3 |",
    ]
    assert as_table(["a"], []) == "| a |\n|---|"
    assert as_table(["a"], [("y" * 60,)]) == f"| a |\n|---|\n| {'y' * 60} |"
