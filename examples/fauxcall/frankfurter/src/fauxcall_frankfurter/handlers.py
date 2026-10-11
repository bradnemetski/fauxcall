"""Custom behavior of this fake: Frankfurter v1's rate engine, rebuilt from evidence.

Every rate endpoint is computed from one dataset: the EUR-base daily ECB history recorded from
``GET /v1/1999-01-04..`` (world collection ``ecb_history``). The logic mirrors the real server
(``lib/versions/v1``: ``Query``, ``Quote::EndOfDay``, ``Quote::Interval``, ``Roundable``,
``CarryForward``) and reproduces every recorded response byte for byte:

* ``/latest`` (alias ``/current``) and ``/{date}``: each currency's latest fixing within the 14 days
  up to the date (carry-forward); fixings are grouped by date and the first group that holds the
  base wins (so a currency that stopped quoting can pull the answer back to its last day).
* ``/{start}..{end}``: every fixing from the last one on or before ``start`` through ``end``.
* Rebasing and ``amount``: when the base is not EUR or ``amount != 1`` every EUR rate is first
  multiplied by ``amount`` and rounded (exact decimal product, half-even), then divided by the
  base's rate and rounded again (``Roundable``: >5000 integer, >80 2 dp, >20 3 dp, >1 4 dp,
  >0.0001 5 dp, else 6 dp).

The history is frozen at the last recording; ECB publications the fake clock passes after it
(calendar ``ecb``) repeat the last recorded rates so the dates keep moving like the real API's.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue
    from fauxcall.world.store import SessionWorld

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

HISTORY_COLLECTION: Final = "ecb_history"
HISTORY_KEY: Final = "1999-01-04"
NAMES_COLLECTION: Final = "currency_names"
NAMES_KEY: Final = "Euro"
CALENDAR: Final = "ecb"
PIVOT: Final = "EUR"
LOOKBACK_DAYS: Final = 14

DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
RANGE_RE: Final = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.\.([0-9]{4}-[0-9]{2}-[0-9]{2})?")
TO_F_RE: Final = re.compile(
    r"\s*([+-]?(?:[0-9]+(?:_[0-9]+)*)?(?:\.[0-9]+(?:_[0-9]+)*)?)([eE][+-]?[0-9]+(?:_[0-9]+)*)?"
)

LEGACY_NAMES: Final[dict[str, str]] = {
    # Names (Money gem) for currencies the ECB no longer quotes; only reachable when the fake clock
    # is moved back to when they were quoted. BGN is the spec's own example; the rest come from
    # the Money gem's ISO table (documentation, not recorded).
    "ARS": "Argentine Peso",
    "BGN": "Bulgarian Lev",
    "CYP": "Cypriot Pound",
    "DZD": "Algerian Dinar",
    "EEK": "Estonian Kroon",
    "GRD": "Greek Drachma",
    "HRK": "Croatian Kuna",
    "LTL": "Lithuanian Litas",
    "LVL": "Latvian Lats",
    "MAD": "Moroccan Dirham",
    "MTL": "Maltese Lira",
    "RUB": "Russian Ruble",
    "SIT": "Slovenian Tolar",
    "SKK": "Slovak Koruna",
    "TWD": "New Taiwan Dollar",
}

type Number = float | int
type DayRates = dict[str, float]
type Param = str | list[str] | dict[str, str]
HASH_CLASS: Final = "Roda::RodaPlugins::IndifferentParams::QueryParser::Params"
BRACKET_RE: Final = re.compile(r"([^\[\]]+)\[([^\]]*)\]")


class QueryError(Exception):
    """A request the real API rejects with 422 ``{"message": ...}`` (Roda's error handler)."""

    def __init__(self, template: str) -> None:
        super().__init__(template)
        self.template = template


# -- numbers -------------------------------------------------------------------------------------


def ruby_to_f(text: str) -> float:
    """Ruby's ``String#to_f``: the longest numeric prefix, else 0.0 (``"10abc"`` -> 10.0)."""
    match = TO_F_RE.match(text)
    assert match is not None  # noqa: S101 - the pattern matches the empty string
    mantissa, exponent = match.group(1), match.group(2)
    if not any(char.isdigit() for char in mantissa):
        return 0.0
    literal = (mantissa + (exponent or "")).replace("_", "")
    if literal.endswith((".", "+", "-")):
        literal = literal.rstrip(".")
    try:
        return float(literal)
    except ValueError:  # pragma: no cover - the pattern only admits float literals
        return 0.0


PRECISION: Final = 800  # digits: enough for any finite double (rates of amount=1e300 included)
NAN_MESSAGE: Final = 'invalid value for Float(): "NaN"'


def _quantize(value: Decimal, places: int) -> float:
    with localcontext() as context:
        context.prec = PRECISION
        return float(value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN))


def _round_half_up(value: float) -> int:
    with localcontext() as context:
        context.prec = PRECISION
        return int(Decimal(value).quantize(Decimal(1), ROUND_HALF_UP))


def _places(value: float | Decimal) -> int:
    for limit, places in ((80, 2), (20, 3), (1, 4)):
        if value > limit:
            return places
    return 5 if value > Decimal("0.0001") else 6


def round_rate(value: float | Decimal) -> Number:
    """``Roundable#round``: quoting precision by magnitude (integers above 5000).

    Above 5000 the float value rounds half away from zero (``Float#round``); below, decimal
    inputs (``amount`` times a stored rate) round exactly, half-even, and floats (rebased rates)
    round their binary value like ``format("%.Nf")``.

    Raises:
        QueryError: NaN (``Float("NaN")`` fails) or infinity (``FloatDomainError``), as the real
            error handler words them.
    """
    if isinstance(value, float) and math.isnan(value):
        raise QueryError(NAN_MESSAGE)
    if value > 5000:  # noqa: PLR2004
        number = float(value)
        if math.isinf(number):
            raise QueryError("Infinity")
        return _round_half_up(number)
    places = _places(value)
    if isinstance(value, Decimal):
        return _quantize(value, places)
    return float(f"{value:.{places}f}")


def divide(numerator: float, divisor: float) -> float:
    """IEEE division like Ruby's ``Float#/`` (a zero divisor gives infinity or NaN)."""
    if divisor == 0:
        return math.nan if numerator == 0 else math.copysign(math.inf, numerator)
    return numerator / divisor


def scale(rate: float, amount: float) -> Decimal | float:
    """``amount * rate`` as the real server computes it (exact, the rate being a stored decimal)."""
    if not math.isfinite(amount):
        return amount * rate
    return Decimal(repr(amount)) * Decimal(repr(rate))


# -- the query -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    """A rate request (``Versions::V1::Query``)."""

    amount: float = 1.0
    base: str = PIVOT
    symbols: list[str] | None = None

    @property
    def must_rebase(self) -> bool:
        """Whether rates are divided by the base's rate."""
        return self.base != PIVOT

    @property
    def should_round(self) -> bool:
        """Whether rates are rounded (rebased or scaled)."""
        return self.amount != 1.0 or self.must_rebase

    def wanted(self, currency: str) -> bool:
        """Whether a stored rate is fetched (``Rate.only(*(symbols + [base]))``)."""
        return self.symbols is None or currency in self.symbols or currency == self.base


def ruby_split(text: str) -> list[str]:
    """Ruby's ``String#split(",")``: trailing empty fields are dropped."""
    parts = text.split(",")
    while parts and not parts[-1]:
        parts.pop()
    return parts


def ruby_class(value: Param) -> str:
    """The Ruby class Rack/Roda gives a bracketed query parameter (for NoMethodError messages)."""
    return "Array" if isinstance(value, list) else HASH_CLASS


def _string(value: Param, method: str) -> str:
    """``value`` if it is a string, else the real server's NoMethodError (422).

    Raises:
        QueryError: ``value`` is an Array or Hash (``symbols[]=``, ``base[x]=``).
    """
    if isinstance(value, str):
        return value
    raise QueryError(f"undefined method '{method}' for an instance of {ruby_class(value)}")


def _first(params: Mapping[str, Param], *names: str) -> Param | None:
    """``params.values_at(*names).compact.first``."""
    return next((params[name] for name in names if name in params), None)


def parse_query(params: Mapping[str, Param], *, period: bool = False) -> Query:
    """``Query#to_h``: ``amount``, ``from``/``base``, ``date`` (periods), ``to``/``symbols``.

    The last repeated value wins (Rack); evaluation order decides which error is reported.

    Raises:
        QueryError: ``invalid amount`` (not positive after ``to_f``), ``invalid date``, or a
            Ruby error message for bracketed (Array/Hash) parameters.
    """
    amount = 1.0
    if "amount" in params:
        amount = ruby_to_f(_string(params["amount"], "to_f"))
        if not amount > 0:
            raise QueryError("invalid_amount")
    raw_base = _first(params, "from", "base")
    base = None if raw_base is None else _string(raw_base, "upcase").upper()
    if period and "date" in params:
        check_date_param(params["date"])
    raw_symbols = _first(params, "to", "symbols")
    symbols = None if raw_symbols is None else ruby_split(_string(raw_symbols, "upcase").upper())
    return Query(amount=amount, base=PIVOT if base is None else base, symbols=symbols)


def check_date_param(value: Param) -> None:
    """A ``date`` query parameter on a period route: ``Query#date`` prefers it to the range.

    ``Date.parse`` either fails (``invalid date``) or yields a Date the interval quote can't use.

    Raises:
        QueryError: Always.
    """
    if not isinstance(value, str):
        message = f"no implicit conversion of {ruby_class(value)} into String"
        raise QueryError(message)
    text = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%d %b %Y", "%b %d %Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            datetime.strptime(text, pattern)  # noqa: DTZ007 - only checks that it parses
        except ValueError:
            continue
        raise QueryError("undefined method 'begin' for an instance of Date")
    raise QueryError("invalid_date")


def check_pair(params: Mapping[str, Param], query: Query) -> None:
    """``bad currency pair``: an explicit base whose only symbol is itself.

    Raises:
        QueryError: The pair is bad.
    """
    if _first(params, "from", "base") is not None and query.symbols == [query.base]:
        raise QueryError("bad_currency_pair")


def parse_date(text: str) -> date | None:
    """``Date.parse`` of a ``YYYY-MM-DD`` capture; ``None`` for year 0 (valid, but has no data).

    Raises:
        QueryError: ``invalid date`` (no such day).
    """
    year, month, day = (int(part) for part in text.split("-"))
    if year == 0:  # proleptic year 0 is a leap year, like 2000
        try:
            date(2000, month, day)
        except ValueError:
            raise QueryError("invalid_date") from None
        return None
    try:
        return date(year, month, day)
    except ValueError:
        raise QueryError("invalid_date") from None


# -- the data ------------------------------------------------------------------------------------


@dataclass(slots=True)
class History:
    """EUR-base fixings by date (ascending), recorded plus synthesized after the last recording."""

    days: list[date]
    rates: dict[date, DayRates]
    synthesized: dict[date, DayRates] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: JsonValue) -> History:
        """Flatten the recorded period response."""
        assert isinstance(record, dict)  # noqa: S101
        by_day = record["rates"]
        assert isinstance(by_day, dict)  # noqa: S101
        rates: dict[date, DayRates] = {}
        for key, values in by_day.items():
            assert isinstance(values, dict)  # noqa: S101
            rates[date.fromisoformat(key)] = {
                code: float(value)  # type: ignore[arg-type]
                for code, value in sorted(values.items())
            }
        return cls(days=sorted(rates), rates=rates)

    def as_of(self, newest: date | None, publications: list[date]) -> History:
        """The history the real server holds once the ECB published ``newest``.

        Recorded days after ``newest`` are hidden (the clock was moved back); publications after
        the last recorded day repeat its rates (values frozen, dates moving).
        """
        last = self.days[-1]
        if newest is None:
            return History(days=[], rates={})
        if newest < last:
            days = self.days[: bisect.bisect_right(self.days, newest)]
            return History(days=days, rates=self.rates)
        extra = {day: self.rates[last] for day in publications if day > last}
        if not extra:
            return self
        merged = {**self.rates, **extra}
        return History(days=sorted(merged), rates=merged, synthesized=extra)

    def between(self, first: date, last: date) -> list[date]:
        """Fixing days in ``[first, last]``."""
        return self.days[
            bisect.bisect_left(self.days, first) : bisect.bisect_right(self.days, last)
        ]

    def on_or_before(self, day: date) -> date | None:
        """The last fixing day on or before ``day``."""
        index = bisect.bisect_right(self.days, day)
        return self.days[index - 1] if index else None


_HISTORY_CACHE: dict[int, History] = {}


def _shared(world: SessionWorld, collection: str, key: str) -> JsonValue:
    """A read-only record without the defensive copy ``world.get`` makes (3.5 MB of history)."""
    for record in world.list(collection, copy=False):
        if record.key == key:
            return record.record
    return None


def history(ctx: RequestContext) -> History:
    """The recorded history (parsed once per world record), extended to the fake clock."""
    record = _shared(ctx.world, HISTORY_COLLECTION, HISTORY_KEY)
    if record is None:  # pragma: no cover - the world is built from recordings
        raise LookupError("ecb_history is missing: run `fauxcall world build frankfurter`")
    key = id(record)
    base = _HISTORY_CACHE.get(key)
    if base is None:
        base = History.from_record(record)
        _HISTORY_CACHE.clear()
        _HISTORY_CACHE[key] = base
    newest = newest_publication(ctx)
    return base.as_of(newest, publications_between(ctx, base.days[-1], newest))


def newest_publication(ctx: RequestContext) -> date | None:
    """The day of the ECB's latest publication at the fake clock (Berlin date)."""
    calendar = ctx.calendar(CALENDAR)
    published_ns = calendar.last_publication(ctx.now_ns)
    if published_ns is None:  # pragma: no cover - the calendar always publishes on weekdays
        return None
    return datetime.fromtimestamp(published_ns / 1e9, tz=calendar.zone).date()


def publications_between(ctx: RequestContext, last: date, newest: date | None) -> list[date]:
    """ECB publication days after ``last`` up to ``newest``."""
    if newest is None:  # pragma: no cover - see newest_publication
        return []
    calendar = ctx.calendar(CALENDAR)
    days: list[date] = []
    day = last + timedelta(days=1)
    while day <= newest:
        if calendar.is_business_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def today(ctx: RequestContext) -> date:
    """``Date.today`` on the real (UTC) server, by the fake clock."""
    return datetime.fromtimestamp(ctx.now_ns / 1e9, tz=UTC).date()


# -- quotes --------------------------------------------------------------------------------------


def prepare(rates: DayRates, query: Query) -> dict[str, Number | Decimal]:
    """``Quote::Base#prepare_rates`` for one day: scale by ``amount``, round when needed."""
    prepared: dict[str, Number | Decimal] = {}
    for code, rate in rates.items():
        value = scale(rate, query.amount)
        prepared[code] = round_rate(value) if query.should_round else query.amount * rate
    return prepared


def rebase(prepared: dict[str, Number | Decimal], query: Query) -> dict[str, Number] | None:
    """``Quote::Base#rebase_rates`` for one day (``None`` drops the day)."""
    if not query.must_rebase:
        return {code: _number(value) for code, value in sorted(prepared.items())}
    rates = dict(prepared)
    if query.symbols is None or PIVOT in query.symbols:
        rates[PIVOT] = query.amount
    divisor = rates.pop(query.base, None)
    if divisor is None or not rates:
        return None
    return {
        code: round_rate(divide(query.amount * float(rate), float(divisor)))
        for code, rate in sorted(rates.items())
    }


def _number(value: Number | Decimal) -> Number:
    return float(value) if isinstance(value, Decimal) else value


def end_of_day(data: History, day: date, query: Query) -> tuple[date, dict[str, Number]] | None:
    """``Quote::EndOfDay`` (carry-forward snapshot within the lookback window)."""
    window = data.between(day - timedelta(days=LOOKBACK_DAYS), day)
    latest: dict[str, date] = {}
    for fixing in window:
        for code in data.rates[fixing]:
            if query.wanted(code):
                latest[code] = fixing
    groups: dict[date, DayRates] = {}
    for code, fixing in latest.items():
        groups.setdefault(fixing, {})[code] = data.rates[fixing][code]
    for fixing, rates in groups.items():
        result = rebase(prepare(rates, query), query)
        if result:
            return fixing, result
    return None


def interval(
    data: History, first: date, last: date, now: date, query: Query
) -> dict[date, dict[str, Number]]:
    """``Quote::Interval`` (``Rate.between``): from the last fixing on or before ``first``."""
    if first > now:
        return {}
    start = data.on_or_before(first) or first
    result: dict[date, dict[str, Number]] = {}
    for fixing in data.between(start, last):
        rates = {code: rate for code, rate in data.rates[fixing].items() if query.wanted(code)}
        if not rates:
            continue
        rebased = rebase(prepare(rates, query), query)
        if rebased:
            result[fixing] = rebased
    return result


def etag(day: date) -> str:
    """The ETag the real API sends (``MD5(date)``, weakened by Cloudflare's compression)."""
    digest = hashlib.md5(day.isoformat().encode(), usedforsecurity=False).hexdigest()
    return f'W/"{digest}"'


# -- handlers ------------------------------------------------------------------------------------


def rack_params(pairs: list[tuple[str, str]]) -> dict[str, Param]:
    """Query parameters as Rack nests them: ``a[]=`` an Array, ``a[k]=`` a Hash; last wins."""
    params: dict[str, Param] = {}
    for key, value in pairs:
        nested = BRACKET_RE.fullmatch(key)
        if nested is None:
            params[key] = value
            continue
        name, inner = nested.group(1), nested.group(2)
        current = params.get(name)
        if inner == "":
            params[name] = [*current, value] if isinstance(current, list) else [value]
        else:
            params[name] = (
                {**current, inner: value} if isinstance(current, dict) else {inner: value}
            )
    return params


def _params(ctx: RequestContext) -> dict[str, Param]:
    return rack_params(ctx.request.query_params.multi_items())


def _encoded(ctx: RequestContext) -> bool:
    """Whether the path was percent-encoded (Rack matches the raw path; Starlette decodes it)."""
    raw = ctx.request.scope.get("raw_path") or b""
    return b"%" in raw


def _respond(ctx: RequestContext, body: JsonValue, tag: str) -> Response:
    if_none_match = ctx.header("if-none-match")
    if if_none_match is not None and _etag_matches(if_none_match, tag):
        # Roda answers 304 itself (strong tag; nothing for the CDN to compress), keeping the
        # route's cache-control.
        headers = {"etag": tag.removeprefix("W/"), "cache-control": "public, max-age=86400"}
        return ctx.empty(304, headers=headers)
    return ctx.respond(body, headers={"etag": tag})


def _etag_matches(header: str, tag: str) -> bool:
    opaque = tag.removeprefix("W/")
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return "*" in candidates or opaque in candidates


async def rates(ctx: RequestContext) -> Response:
    """Every rate endpoint: ``/latest``, ``/current``, ``/{date}``, ``/{start}..[{end}]``."""
    segment = ctx.request.url.path.removeprefix("/v1/")
    single = segment in {"latest", "current"} or DATE_RE.fullmatch(segment) is not None
    period = RANGE_RE.fullmatch(segment)
    if (not single and period is None) or _encoded(ctx):
        return ctx.error("unknown_route")
    params = _params(ctx)
    try:
        query = parse_query(params, period=period is not None and not single)
        if single:
            return _single(ctx, segment, params, query)
        assert period is not None  # noqa: S101
        return _period(ctx, period, params, query)
    except QueryError as problem:
        if problem.template in {"invalid_amount", "invalid_date", "bad_currency_pair"}:
            return ctx.error("custom", template=problem.template)
        return ctx.respond({"message": problem.template}, status=422)


def _single(ctx: RequestContext, segment: str, params: dict[str, Param], query: Query) -> Response:
    day = today(ctx) if segment in {"latest", "current"} else parse_date(segment)
    check_pair(params, query)
    found = None if day is None else end_of_day(history(ctx), day, query)
    if found is None:
        return ctx.error("not_found")
    fixing, result = found
    body: JsonValue = {
        "amount": query.amount,
        "base": query.base,
        "date": fixing.isoformat(),
        "rates": result,  # type: ignore[dict-item]
    }
    return _respond(ctx, body, etag(fixing))


def _period(
    ctx: RequestContext, period: re.Match[str], params: dict[str, Param], query: Query
) -> Response:
    now = today(ctx)
    first = parse_date(period.group(1)) or date.min  # year 0: before every fixing
    last = parse_date(period.group(2)) if period.group(2) else now
    check_pair(params, query)
    found = {} if last is None else interval(history(ctx), first, last, now, query)
    if not found:
        return ctx.error("not_found")
    days = list(found)
    body: JsonValue = {
        "amount": query.amount,
        "base": query.base,
        "start_date": days[0].isoformat(),
        "end_date": days[-1].isoformat(),
        "rates": {day.isoformat(): values for day, values in found.items()},  # type: ignore[misc]
    }
    return _respond(ctx, body, etag(days[-1]))


def currency_names(world: SessionWorld) -> dict[str, str]:
    """Recorded names (``/currencies``) plus documented names of no-longer-quoted currencies."""
    recorded = _shared(world, NAMES_COLLECTION, NAMES_KEY) or {}
    assert isinstance(recorded, dict)  # noqa: S101
    return {**LEGACY_NAMES, **{code: str(name) for code, name in recorded.items()}}


def quoted_today(ctx: RequestContext) -> tuple[list[str], date | None]:
    """Currencies quoted in the carry-forward window before today, and the first one's day."""
    data = history(ctx)
    now = today(ctx)
    latest: dict[str, date] = {}
    for fixing in data.between(now - timedelta(days=LOOKBACK_DAYS), now):
        for code in data.rates[fixing]:
            latest[code] = fixing
    first_day = next(iter(latest.values()), None)
    return list(latest), first_day


async def currencies(ctx: RequestContext) -> Response:
    """``GET /v1/currencies`` (``CurrencyNames``)."""
    if _encoded(ctx):
        return ctx.error("unknown_route")
    codes, first_day = quoted_today(ctx)
    if not codes:
        return ctx.respond({})
    names = currency_names(ctx.world)
    body: dict[str, JsonValue] = {code: names.get(code, code) for code in sorted([*codes, PIVOT])}
    assert first_day is not None  # noqa: S101
    return _respond(ctx, body, etag(first_day))


def names_cover_quotes(world: SessionWorld) -> list[str]:
    """Invariant: every currency in the ECB history has a name for ``/currencies``."""
    names = currency_names(world)
    record = _shared(world, HISTORY_COLLECTION, HISTORY_KEY)
    if record is None:
        return ["ecb_history is missing"]
    data = History.from_record(record)
    quoted = {code for rates_on_day in data.rates.values() for code in rates_on_day}
    return [f"{code} has no name" for code in sorted(quoted - set(names))]


for _endpoint in (
    "GET /v1/latest",
    "GET /v1/current",
    "GET /v1/{date}",
    "GET /v1/{start_date}..",
    "GET /v1/{start_date}..{end_date}",
):
    HANDLERS[_endpoint] = rates
HANDLERS["GET /v1/currencies"] = currencies
