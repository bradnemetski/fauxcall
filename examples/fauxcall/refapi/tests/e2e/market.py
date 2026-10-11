"""Market-clock helpers shared by the e2e scenarios (quotes over REST and the WebSocket feed)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient


def in_session(exchange: dict[str, Any], now: float) -> bool:
    """Whether ``now`` falls in one of the exchange's weekday sessions, in its own zone."""
    local = datetime.fromtimestamp(now, ZoneInfo(exchange["timezone"]))
    hhmm = local.strftime("%H:%M")
    return local.weekday() < 5 and any(s["open"] <= hhmm < s["close"] for s in exchange["sessions"])


async def exchanges_by_code(api: ApiClient) -> dict[str, dict[str, Any]]:
    return {e["code"]: e for e in (await api.get("/v1/exchanges")).json()}


async def get_quote(api: ApiClient, symbol: str, exchange: dict[str, Any]) -> dict[str, Any]:
    """The REST quote, checked against the exchange's sessions.

    Parity compares ``as_of`` by the model's calendar policy: its distance from each side's
    response time (10 s tolerance). Keep quote calls out of parametrized scenarios whose real
    variants all run before the fake ones, or the two calls drift further apart than that.
    """
    quote: dict[str, Any] = (await api.get(f"/v1/tickers/{symbol}/quote")).json()
    check_quote_clock(quote, exchange, time.time())
    return quote


def check_quote_clock(quote: dict[str, Any], exchange: dict[str, Any], now: float) -> None:
    """``market_state``/``as_of`` against the exchange's weekday sessions in its own zone.

    Open: inside a session and ``as_of`` = now - 1.5 s (documented). Closed: ``as_of`` is a
    session close on a weekday, the latest one before now (holidays: none modeled, see notes).
    """
    zone = ZoneInfo(exchange["timezone"])
    as_of = datetime.fromisoformat(quote["as_of"])
    assert quote["as_of"].endswith("Z")
    assert len(quote["as_of"].split(".")[1]) == 7
    if quote["market_state"] == "open":
        assert in_session(exchange, now), quote
        assert abs(now - 1.5 - as_of.timestamp()) < 10
        return
    assert quote["market_state"] == "closed"
    assert not in_session(exchange, now), quote
    closed_at = as_of.astimezone(zone)
    assert closed_at.weekday() < 5
    assert closed_at.strftime("%H:%M") in {s["close"] for s in exchange["sessions"]}
    assert 0 <= now - as_of.timestamp() < 4 * 86_400
