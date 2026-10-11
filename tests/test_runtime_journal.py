from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fauxcall.jsonfmt import JsonFloat, dumps
from fauxcall.logs import REDACTED
from fauxcall.runtime.journal import (
    BODY_LIMIT,
    Journal,
    JournalEntry,
    body_for_journal,
    redact_pairs,
)

START = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)


def entry(
    seq: int,
    *,
    session: str = "default",
    method: str = "GET",
    path: str = "/v1/pets",
    endpoint: str | None = "GET /v1/pets",
) -> JournalEntry:
    return JournalEntry(
        seq=seq,
        session=session,
        request_id=f"req-{seq}",
        at=START + timedelta(seconds=seq),
        method=method,
        path=path,
        endpoint=endpoint,
    )


def seqs(entries: list[JournalEntry]) -> list[int]:
    return [e.seq for e in entries]


def test_sequence_numbers_start_at_one() -> None:
    journal = Journal(10)
    assert [journal.next_seq() for _ in range(3)] == [1, 2, 3]


def test_bounded_size_drops_the_oldest() -> None:
    journal = Journal(3)
    for seq in range(1, 6):
        journal.add(entry(seq))
    assert len(journal) == 3
    assert seqs(journal.query()) == [3, 4, 5]


@pytest.mark.parametrize("limit", [0, -5])
def test_a_zero_limit_disables_the_journal(limit: int) -> None:
    journal = Journal(limit)
    assert not journal.enabled
    journal.add(entry(1))
    assert len(journal) == 0
    assert journal.query() == []


def test_filters_combine() -> None:
    journal = Journal(100)
    journal.add(entry(1, session="a"))
    journal.add(entry(2, session="b", method="POST", path="/v1/orders", endpoint="POST /v1/orders"))
    journal.add(entry(3, session="a", path="/v1/pets/7", endpoint="GET /v1/pets/{id}"))
    journal.add(entry(4, session="a", method="post", path="/v1/pets", endpoint="POST /v1/pets"))
    journal.add(entry(5, session="b", path="/healthz", endpoint=None))
    assert seqs(journal.query(session="a")) == [1, 3, 4]
    assert seqs(journal.query(endpoint="GET /v1/pets/{id}")) == [3]
    assert seqs(journal.query(method="POST")) == [2, 4]
    assert seqs(journal.query(method="get")) == [1, 3, 5]
    assert seqs(journal.query(path_prefix="/v1/pets")) == [1, 3, 4]
    assert seqs(journal.query(since_seq=3)) == [4, 5]
    assert seqs(journal.query(session="a", method="GET", path_prefix="/v1/pets/")) == [3]
    assert journal.query(session="nobody") == []


def test_limit_keeps_the_newest_in_chronological_order() -> None:
    journal = Journal(100)
    for seq in range(1, 6):
        journal.add(entry(seq))
    assert seqs(journal.query(limit=2)) == [4, 5]
    assert seqs(journal.query(limit=50)) == [1, 2, 3, 4, 5]
    assert journal.query(limit=0) == []
    assert seqs(journal.query(since_seq=1, limit=3)) == [3, 4, 5]


def test_clear_one_session_or_everything() -> None:
    journal = Journal(4)
    for seq, session in enumerate(["a", "b", "a", "b"], start=1):
        journal.add(entry(seq, session=session))
    assert journal.clear("a") == 2
    assert seqs(journal.query()) == [2, 4]
    assert journal.clear("nobody") == 0
    for seq in range(5, 9):  # still bounded after a partial clear
        journal.add(entry(seq, session="c"))
    assert seqs(journal.query()) == [5, 6, 7, 8]
    assert journal.clear() == 4
    assert len(journal) == 0


def test_redact_pairs_blanks_credentials_only() -> None:
    pairs = [
        ("Authorization", "Bearer secret"),
        ("x-api-key", "k-123"),
        ("Cookie", "sid=1"),
        ("api_key", "k-456"),
        ("token", "t"),
        ("content-type", "application/json"),
        ("X-Fauxcall-Session", "eval-1"),
        ("q", "apple"),
    ]
    assert redact_pairs(pairs) == [
        ("Authorization", REDACTED),
        ("x-api-key", REDACTED),
        ("Cookie", REDACTED),
        ("api_key", REDACTED),
        ("token", REDACTED),
        ("content-type", "application/json"),
        ("X-Fauxcall-Session", "eval-1"),
        ("q", "apple"),
    ]


def test_empty_bodies() -> None:
    assert body_for_journal(b"", "application/json") == (None, None)


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/problem+json", "Application/JSON; charset=utf-8"],
)
def test_json_bodies_are_parsed_keeping_float_spellings(content_type: str) -> None:
    parsed, text = body_for_journal(b'{"price":105.50,"tags":["a"]}', content_type)
    assert text is None
    assert parsed == {"price": 105.5, "tags": ["a"]}
    assert isinstance(parsed, dict)
    assert isinstance(parsed["price"], JsonFloat)
    assert dumps(parsed) == '{"price":105.50,"tags":["a"]}'


@pytest.mark.parametrize(
    ("raw", "content_type"),
    [
        (b'{"broken": ', "application/json"),
        (b'{"a": 1}', None),
        (b'{"a": 1}', "text/plain"),
        ("Moët & Chandon".encode(), "text/plain; charset=utf-8"),
    ],
)
def test_other_bodies_are_kept_as_text(raw: bytes, content_type: str | None) -> None:
    assert body_for_journal(raw, content_type) == (None, raw.decode())


def test_deeply_nested_json_is_kept_as_text() -> None:
    raw = b"[" * 30_000 + b"]" * 30_000
    assert len(raw) <= BODY_LIMIT
    parsed, text = body_for_journal(raw, "application/json")
    assert parsed is None
    assert text == raw.decode()


def test_binary_bodies_are_summarized() -> None:
    raw = b"\x89PNG\r\n\x1a\n\xff\xfe"
    assert body_for_journal(raw, "image/png") == (None, f"<{len(raw)} bytes of binary data>")


def test_long_bodies_are_truncated_and_marked() -> None:
    raw = b"x" * (BODY_LIMIT + 10)
    parsed, text = body_for_journal(raw, "text/plain")
    assert parsed is None
    assert text == "x" * BODY_LIMIT + "…"


def test_long_json_is_not_parsed() -> None:
    raw = b'["' + b"y" * BODY_LIMIT + b'"]'
    parsed, text = body_for_journal(raw, "application/json")
    assert parsed is None
    assert text is not None
    assert text.endswith("…")
    assert len(text) == BODY_LIMIT + 1


def test_truncation_never_splits_a_character() -> None:
    raw = ("a" * (BODY_LIMIT - 1) + "é" + "b" * 10).encode()
    parsed, text = body_for_journal(raw, "text/plain")
    assert parsed is None
    assert text == "a" * (BODY_LIMIT - 1) + "…"


def test_truncated_binary_is_still_binary() -> None:
    raw = b"\xff" * (BODY_LIMIT + 1)
    assert body_for_journal(raw, None) == (None, f"<{BODY_LIMIT + 1} bytes of binary data>")


def test_entries_dump_with_defaults() -> None:
    dumped = entry(1).model_dump(mode="json")
    assert dumped["query"] == []
    assert dumped["status"] is None
    assert dumped["streamed"] is False
    assert dumped["at"] == "2026-10-10T14:30:01Z"


def test_brackets_inside_strings_do_not_count_as_nesting() -> None:
    raw = json.dumps({"text": "[" * 1000 + '\\"]' + "{" * 1000}).encode()
    parsed, text = body_for_journal(raw, "application/json")
    assert isinstance(parsed, dict)
    assert text is None
