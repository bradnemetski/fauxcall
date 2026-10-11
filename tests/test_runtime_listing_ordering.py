"""Key cursors that name a record the current filters exclude (the reference API accepts them)."""

from __future__ import annotations

import pytest

from fauxcall.model.endpoints import PaginationModel
from fauxcall.model.world import WorldRecord
from fauxcall.runtime.listing import PageError, make_cursor, paginate

PAGINATION = PaginationModel(
    style="cursor",
    cursor_param="cursor",
    cursor_format="key",
    cursor_template={"after": "{{key}}"},
    cursor_encoding="base64url",
    default_limit=1,
)
LISTED = [("A", "X"), ("B", "Y"), ("C", "X"), ("D", "Y"), ("E", "X")]
EVERYTHING = [
    WorldRecord(key=symbol, record={"symbol": symbol, "exchange": exchange})
    for symbol, exchange in LISTED
]
ON_X = [record for record, (_, exchange) in zip(EVERYTHING, LISTED, strict=True) if exchange == "X"]


def _page(key: str, *, ordering: list[WorldRecord] | None) -> list[object]:
    cursor = make_cursor(PAGINATION, offset=0, key=key)
    page = paginate(
        ON_X, PAGINATION, {"cursor": cursor}, base_url="http://x/t", query=[], ordering=ordering
    )
    return [record.key for record in page.items]


def test_a_filtered_out_record_continues_after_its_place() -> None:
    assert _page("A", ordering=EVERYTHING) == ["C"]  # in the filtered list: as before
    assert _page("B", ordering=EVERYTHING) == ["C"]  # B is on Y: continue after B
    assert _page("D", ordering=EVERYTHING) == ["E"]


@pytest.mark.parametrize("ordering", [None, EVERYTHING])
def test_unknown_or_unplaceable_keys_are_rejected(ordering: list[WorldRecord] | None) -> None:
    with pytest.raises(PageError):
        _page("Z", ordering=ordering)
    if ordering is None:
        with pytest.raises(PageError):
            _page("B", ordering=None)  # without the full ordering there is no place to resume
