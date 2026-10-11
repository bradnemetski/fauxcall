"""Custom behavior of this fake: yours to edit, ``fauxcall scaffold`` never overwrites it.

Every timeapi.io endpoint except the static zone list and the health check is computed: the answer
depends on the clock and on the tz database, so nothing is replayed. The logic lives in
:mod:`fauxcall_timeapi.views` (bodies), :mod:`~fauxcall_timeapi.dotnet` (ASP.NET input parsing and
output spelling), :mod:`~fauxcall_timeapi.tzengine` (zone intervals) and
:mod:`~fauxcall_timeapi.geo` (coordinates and IP addresses). This module does the request side:
ASP.NET Core model binding and validation, in the order and with the messages the real server uses.

Two API generations differ in their error spelling:

* v1 (``/api/v1/...``): plain-text errors (``text/plain``), validation problems as
  ``application/problem+json``.
* legacy (``/api/...``): errors are JSON strings, validation problems plain ``application/json``.
"""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

from babel import Locale, UnknownLocaleError
from babel.dates import format_date, format_time

from fauxcall_timeapi import dotnet, geo, tzengine, views

if TYPE_CHECKING:
    import ipaddress
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from fastapi import Response, WebSocket
    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

HANDLERS: dict[str, Callable[[RequestContext], Awaitable[Response]]] = {}
"""Endpoint id -> custom HTTP handler."""

WEBSOCKET_HANDLERS: dict[str, Callable[[WebSocket, RequestContext], Awaitable[None]]] = {}
"""Endpoint id -> custom WebSocket handler."""

TOOL_HANDLERS: dict[str, Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]] = {}
"""MCP tool name -> custom tool handler (tools whose behavior is ``computed``)."""

Api = Literal["v1", "legacy"]

PROBLEM_TYPE: Final = "https://tools.ietf.org/html/rfc9110#section-15.5.1"
PROBLEM_TITLE: Final = "One or more validation errors occurred."
PROBLEM_MEDIA: Final = {
    "v1": "application/problem+json; charset=utf-8",
    "legacy": "application/json; charset=utf-8",
}
LATITUDE_RANGE: Final = 90.0
LONGITUDE_RANGE: Final = 180.0
AMBIGUOUS: Final = (
    "Time given is ambiguous. This could be because of DST switching. "
    "Provide a DstAmbiguity in the request to solve this."
)


class Rejected(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Stop handling and answer with ``response``."""

    def __init__(self, response: Response) -> None:
        super().__init__("rejected")
        self.response = response


# -- responses ----------------------------------------------------------------------------------


def trace_id(ctx: RequestContext) -> str:
    """A W3C ``traceparent`` like ASP.NET puts in ``traceId``."""
    return f"00-{ctx.rng.getrandbits(128):032x}-{ctx.rng.getrandbits(64):016x}-00"


def validation_problem(ctx: RequestContext, api: Api, errors: dict[str, list[str]]) -> Response:
    """ASP.NET's automatic 400 for model-binding/validation errors."""
    body: JsonObject = {
        "type": PROBLEM_TYPE,
        "title": PROBLEM_TITLE,
        "status": 400,
        "errors": {name: list(texts) for name, texts in errors.items()},
        "traceId": trace_id(ctx),
    }
    return ctx.respond(body, status=400, content_type=PROBLEM_MEDIA[api])


def message(ctx: RequestContext, api: Api, text: str, status: int = 400) -> Response:
    """An error message: plain text on v1, a JSON string on the legacy API."""
    if api == "v1":
        return ctx.respond_bytes(
            text.encode("utf-8"), status=status, content_type="text/plain; charset=utf-8"
        )
    return ctx.respond(text, status=status, content_type="application/json; charset=utf-8")


# -- query binding ------------------------------------------------------------------------------


def query_value(ctx: RequestContext, name: str) -> str | None:
    """A query parameter as ASP.NET binds a scalar: name case-insensitive, first value wins."""
    wanted = name.lower()
    for key, value in ctx.request.query_params.multi_items():
        if key.lower() == wanted:
            return value
    return None


def required_string(ctx: RequestContext, api: Api, name: str) -> str:
    """A required ``string`` query parameter (absent or empty: validation problem)."""
    value = query_value(ctx, name)
    if value is None or not value.strip():  # the string binder turns blank into null (ws-zone)
        raise Rejected(validation_problem(ctx, api, {name: [f"The {name} field is required."]}))
    return value


def coordinates(ctx: RequestContext, api: Api) -> tuple[float, float]:
    """``latitude``/``longitude`` (``float``, default 0) with the real API's checks and order."""
    errors: dict[str, list[str]] = {}
    values: dict[str, float] = {}
    for name in ("latitude", "longitude"):
        raw = query_value(ctx, name)
        if raw is None:
            values[name] = 0.0
            continue
        parsed = dotnet.parse_float(raw)
        if parsed is None:
            text = (
                f"The value '{raw}' is invalid."
                if not raw.strip()
                else f"The value '{raw}' is not valid."
            )
            errors[name] = [text]
        else:
            values[name] = parsed
    if errors:
        raise Rejected(validation_problem(ctx, api, errors))
    latitude, longitude = values["latitude"], values["longitude"]
    if abs(longitude) > LONGITUDE_RANGE:
        raise Rejected(message(ctx, api, "Longitude has to between 180 and -180"))
    if abs(latitude) > LATITUDE_RANGE:
        raise Rejected(message(ctx, api, "Latitude has to between 90 and -90"))
    return latitude, longitude


def ip_address(ctx: RequestContext, api: Api) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The required ``ipAddress`` parameter, parsed like ``IPAddress.TryParse``."""
    raw = required_string(ctx, api, "ipAddress")
    parsed = dotnet.parse_ip(raw)
    if parsed is None:
        text = "Invalid IP address" if api == "v1" else "Invalid ip address"
        raise Rejected(message(ctx, api, text))
    return parsed


def zone_param(ctx: RequestContext, api: Api, name: str, invalid: str, status: int = 400) -> str:
    """A required IANA zone parameter, matched case-insensitively."""
    zone = tzengine.canonical_zone(required_string(ctx, api, name))
    if zone is None:
        if status == 404:  # noqa: PLR2004 - legacy /api/timezone/zone answers a bare Not Found
            raise Rejected(ctx.error("not_found"))
        raise Rejected(message(ctx, api, invalid))
    return zone


# -- JSON body binding --------------------------------------------------------------------------


def body_fields(ctx: RequestContext, api: Api) -> dict[str, JsonValue]:
    """The JSON object body, keys lower-cased (System.Text.Json web defaults: case-insensitive)."""
    content_type = (ctx.header("content-type") or "").split(";")[0].strip().lower()
    if not (content_type == "application/json" or content_type.endswith("+json")):
        raise Rejected(ctx.error("unsupported_media_type"))
    if not ctx.body_bytes.strip():
        raise Rejected(
            validation_problem(ctx, api, {"": ["A non-empty request body is required."]})
        )
    body = ctx.json_body
    if ctx.json_error or not isinstance(body, dict):
        raise Rejected(
            validation_problem(
                ctx,
                api,
                {
                    "$": ["The JSON value could not be converted to the request model. Path: $"],
                    "request": ["The request field is required."],
                },
            )
        )
    return {str(key).lower(): value for key, value in body.items()}


def require_fields(
    ctx: RequestContext,
    api: Api,
    fields: dict[str, JsonValue],
    order: Sequence[str],
    *,
    empty_allowed: Sequence[str] = (),
) -> dict[str, str]:
    """``[Required]`` string properties, reported in the server's (per-model) order."""
    errors: dict[str, list[str]] = {}
    values: dict[str, str] = {}
    for name in order:
        value = fields.get(name.lower())
        text = value if isinstance(value, str) else None
        if text is None or (text == "" and name not in empty_allowed):
            errors[name] = [f"The {name} field is required."]
        else:
            values[name] = text
    if errors:
        raise Rejected(validation_problem(ctx, api, errors))
    return values


def ambiguity(ctx: RequestContext, text: str) -> views.Ambiguity:
    """``dstAmbiguity``: empty, ``earlier`` or ``later`` (case-insensitive)."""
    lowered = text.lower()
    if lowered == "earlier":
        return "earlier"
    if lowered == "later":
        return "later"
    if lowered == "":
        return ""
    raise Rejected(message(ctx, "legacy", "Failed parsing DstAmbiguity."))


def body_zone(ctx: RequestContext, text: str, invalid: str) -> str:
    """A zone from a request body."""
    zone = tzengine.canonical_zone(text)
    if zone is None:
        raise Rejected(message(ctx, "legacy", invalid))
    return zone


def resolve_local(
    ctx: RequestContext, zone: str, local: dotnet.LocalDateTime, how: views.Ambiguity
) -> int:
    """A local time -> instant ticks, or the ambiguity error."""
    try:
        instant, _ = views.resolve(zone, local, how)
    except views.AmbiguousTimeError:
        raise Rejected(message(ctx, "legacy", AMBIGUOUS)) from None
    return instant


# -- handler plumbing ---------------------------------------------------------------------------


def endpoint(*ids: str) -> Callable[[Callable[[RequestContext], Response]], None]:
    """Register a synchronous body builder for endpoint ids (``Rejected`` becomes the answer)."""

    def register(build: Callable[[RequestContext], Response]) -> None:
        async def handler(ctx: RequestContext) -> Response:
            try:
                return build(ctx)
            except Rejected as rejected:
                return rejected.response
            except views.OutOfRangeError:
                return ctx.error("custom", template="server_error")

        for endpoint_id in ids:
            HANDLERS[endpoint_id] = handler

    return register


def now(ctx: RequestContext) -> int:
    """The fake clock in Unix ticks (100 ns).

    On hosts whose clock has microsecond resolution (macOS) the last tick digit is filled in from
    the request's seeded random stream, so 7-digit fractions look like the real server's. Whole
    seconds (a clock frozen by an evaluation) stay exact.
    """
    ticks = views.now_ticks(ctx.now_ns)
    if ctx.now_ns % 1000 == 0 and ctx.now_ns % 1_000_000_000 != 0:
        ticks += ctx.rng.randrange(10)
    return ticks


@endpoint("GET /api/health/check")
def health(ctx: RequestContext) -> Response:
    """An empty 200 without a content type."""
    return ctx.empty(200)


# -- v1 -----------------------------------------------------------------------------------------


def _register_unix() -> None:
    units: dict[str, Literal["s", "ms", "us", "ns"]] = {
        "unix": "s",
        "unix_ms": "ms",
        "unix_us": "us",
        "unix_ns": "ns",
    }
    for leaf, unit in units.items():

        def build(ctx: RequestContext, unit: Literal["s", "ms", "us", "ns"] = unit) -> Response:
            return ctx.respond(views.v1_unix(now(ctx), unit))

        endpoint(f"GET /api/v1/time/current/{leaf}")(build)


_register_unix()


@endpoint("GET /api/v1/time/current/utc")
def v1_utc(ctx: RequestContext) -> Response:
    """Current UTC time."""
    return ctx.respond(views.v1_utc(now(ctx)))


@endpoint("GET /api/v1/time/current/zone")
def v1_current_zone(ctx: RequestContext) -> Response:
    """Current time in a zone."""
    zone = zone_param(ctx, "v1", "timezone", "Invalid Timezone")
    return ctx.respond(views.v1_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/v1/time/current/coordinate")
def v1_current_coordinate(ctx: RequestContext) -> Response:
    """Current time at a coordinate."""
    zone = geo.zone_at(*coordinates(ctx, "v1"))
    return ctx.respond(views.v1_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/v1/time/current/ip")
def v1_current_ip(ctx: RequestContext) -> Response:
    """Current time where an IP address is."""
    zone = geo.zone_of_ip(ip_address(ctx, "v1"))
    return ctx.respond(views.v1_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/v1/timezone/zone")
def v1_timezone_zone(ctx: RequestContext) -> Response:
    """Zone info by name."""
    zone = zone_param(ctx, "v1", "timeZone", "Timezone not found")
    return ctx.respond(views.v1_timezone(views.state(zone, now(ctx))))


@endpoint("GET /api/v1/timezone/coordinate")
def v1_timezone_coordinate(ctx: RequestContext) -> Response:
    """Zone info at a coordinate."""
    zone = geo.zone_at(*coordinates(ctx, "v1"))
    return ctx.respond(views.v1_timezone(views.state(zone, now(ctx))))


# -- legacy: time and zones -----------------------------------------------------------------------


@endpoint("GET /api/time/current/zone")
def legacy_current_zone(ctx: RequestContext) -> Response:
    """Current time in a zone."""
    zone = zone_param(ctx, "legacy", "timeZone", "Invalid Timezone")
    return ctx.respond(views.legacy_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/time/current/coordinate")
def legacy_current_coordinate(ctx: RequestContext) -> Response:
    """Current time at a coordinate."""
    zone = geo.zone_at(*coordinates(ctx, "legacy"))
    return ctx.respond(views.legacy_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/time/current/ip")
def legacy_current_ip(ctx: RequestContext) -> Response:
    """Current time where an IP address is."""
    zone = geo.zone_of_ip(ip_address(ctx, "legacy"))
    return ctx.respond(views.legacy_current_time(views.state(zone, now(ctx))))


@endpoint("GET /api/timezone/zone")
def legacy_timezone_zone(ctx: RequestContext) -> Response:
    """Zone info by name (an unknown zone is a 404 problem)."""
    zone = zone_param(ctx, "legacy", "timeZone", "Not Found", status=404)
    return ctx.respond(views.legacy_timezone(views.state(zone, now(ctx))))


@endpoint("GET /api/timezone/coordinate")
def legacy_timezone_coordinate(ctx: RequestContext) -> Response:
    """Zone info at a coordinate."""
    zone = geo.zone_at(*coordinates(ctx, "legacy"))
    return ctx.respond(views.legacy_timezone(views.state(zone, now(ctx))))


@endpoint("GET /api/timezone/ip")
def legacy_timezone_ip(ctx: RequestContext) -> Response:
    """Zone info where an IP address is."""
    zone = geo.zone_of_ip(ip_address(ctx, "legacy"))
    return ctx.respond(views.legacy_timezone(views.state(zone, now(ctx))))


# -- legacy: conversions ---------------------------------------------------------------------------


def _date_path(ctx: RequestContext) -> dotnet.LocalDateTime:
    date = dotnet.parse_date(ctx.path_param("date") or "")
    if date is None:
        raise Rejected(message(ctx, "legacy", "Invalid date"))
    return date


@endpoint("GET /api/conversion/dayoftheweek/{date}")
def day_of_the_week(ctx: RequestContext) -> Response:
    """Day name of a date."""
    return ctx.respond({"dayOfWeek": _date_path(ctx).day_name})


@endpoint("GET /api/conversion/dayoftheyear/{date}")
def day_of_the_year(ctx: RequestContext) -> Response:
    """1-based day of the year of a date."""
    return ctx.respond({"day": _date_path(ctx).day_of_year})


@endpoint("POST /api/conversion/converttimezone")
def convert_time_zone(ctx: RequestContext) -> Response:
    """A local time in one zone, in another zone."""
    fields = body_fields(ctx, "legacy")
    values = require_fields(
        ctx,
        "legacy",
        fields,
        ("DateTime", "ToTimeZone", "DstAmbiguity", "FromTimeZone"),
        empty_allowed=("DstAmbiguity",),
    )
    from_zone = body_zone(ctx, values["FromTimeZone"], "Invalid from timezone")
    to_zone = body_zone(ctx, values["ToTimeZone"], "Invalid to timezone")
    local = dotnet.parse_local(values["DateTime"])
    if local is None:
        raise Rejected(message(ctx, "legacy", "Invalid DateTime format."))
    how = ambiguity(ctx, values["DstAmbiguity"])
    try:
        body = views.conversion(from_zone, local, to_zone, how)
    except views.AmbiguousTimeError:
        raise Rejected(message(ctx, "legacy", AMBIGUOUS)) from None
    return ctx.respond(body)


def _translation_locale(code: str) -> Locale | None:
    if len(code) != 2 or not code.isalpha():  # noqa: PLR2004 - two-letter ISO 639-1 codes only
        return None
    try:
        return Locale.parse(code.lower())
    except (UnknownLocaleError, ValueError):
        return None


@endpoint("POST /api/conversion/translate")
def translate(ctx: RequestContext) -> Response:
    """A date time spelled out in a language (CLDR full date + medium time)."""
    fields = body_fields(ctx, "legacy")
    values = require_fields(ctx, "legacy", fields, ("DateTime", "LanguageCode"))
    local = dotnet.parse_local(values["DateTime"])
    if local is None:
        raise Rejected(message(ctx, "legacy", "Invalid DateTime format."))
    locale = _translation_locale(values["LanguageCode"])
    if locale is None:
        raise Rejected(message(ctx, "legacy", "Couldn't find a language with that code"))
    moment = _as_datetime(local)
    friendly_date = _friendly_date(moment, locale)
    friendly_time = format_time(moment, _time_pattern(locale, moment.hour), locale=locale)
    return ctx.respond(
        {
            "dateTime": values["DateTime"],
            "languageCode": values["LanguageCode"],
            "friendlyDateTime": f"{friendly_date} {friendly_time}",
            "friendlyDate": friendly_date,
            "friendlyTime": friendly_time,
        }
    )


PERSIAN_MONTHS: Final = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)  # fmt: skip


def _friendly_date(moment: datetime, locale: Locale) -> str:
    """CLDR full date in the culture's default .NET calendar.

    Thai cultures count Buddhist years (tr-th: "… พ.ศ. 2564"), Persian ones the Solar Hijri
    calendar in .NET's own pattern (tr-fa: "1399 اسفند 24, یکشنبه"). Other cultures: Gregorian.
    """
    if locale.language == "th":
        text = format_date(moment, "full", locale=locale)
        return text.replace("ค.ศ.", "พ.ศ.").replace(str(moment.year), str(moment.year + 543))
    if locale.language == "fa":
        year, month, day = dotnet.persian_date(moment.year, moment.month, moment.day)
        weekday = format_date(moment, "EEEE", locale=locale)
        return f"{year} {PERSIAN_MONTHS[month - 1]} {day}, {weekday}"
    return format_date(moment, "full", locale=locale)


def _time_pattern(locale: Locale, hour: int) -> str:
    """The medium time pattern, with .NET's AM/PM designator (CLDR wide name, else abbreviated).

    ICU-backed .NET reads ``AMDesignator`` from the wide day periods: Korean says 오전, where the
    abbreviated CLDR form is "AM" (parity run 2, test_translate_languages[ko]).
    """
    pattern = str(locale.time_formats["medium"].pattern)
    if "a" not in pattern.replace("'", ""):
        return pattern
    period = "am" if hour < 12 else "pm"  # noqa: PLR2004
    periods = locale.day_periods["format"]
    name = periods["wide"].get(period) or periods["abbreviated"].get(period) or period.upper()
    literal = "'" + str(name).replace("'", "''") + "'"
    return re.sub(r"a+(?=(?:[^']*'[^']*')*[^']*$)", literal, pattern)


def _as_datetime(local: dotnet.LocalDateTime) -> datetime:
    from datetime import datetime  # noqa: PLC0415 - only translate needs it

    return datetime(local.year, local.month, local.day, local.hour, local.minute, local.second)  # noqa: DTZ001


# -- legacy: calculations --------------------------------------------------------------------------


def _span(ctx: RequestContext, text: str) -> dotnet.TimeSpan:
    span = dotnet.parse_span(text)
    if span is None:
        raise Rejected(message(ctx, "legacy", "Invalid TimeSpan or incorrect format."))
    return span


def calculate_current(ctx: RequestContext, sign: Literal[1, -1]) -> Response:
    """The current time in a zone ± a time span."""
    fields = body_fields(ctx, "legacy")
    values = require_fields(ctx, "legacy", fields, ("TimeSpan", "TimeZone"))
    zone = body_zone(ctx, values["TimeZone"], "Invalid timezone")
    span = _span(ctx, values["TimeSpan"])
    return ctx.respond(views.calculation(zone, now(ctx), span, sign))


def calculate_custom(ctx: RequestContext, sign: Literal[1, -1], invalid_date: str) -> Response:
    """A local time in a zone ± a time span."""
    fields = body_fields(ctx, "legacy")
    values = require_fields(
        ctx,
        "legacy",
        fields,
        ("DateTime", "TimeSpan", "TimeZone", "DstAmbiguity"),
        empty_allowed=("DstAmbiguity",),
    )
    zone = body_zone(ctx, values["TimeZone"], "Invalid timezone")
    local = dotnet.parse_local(values["DateTime"])
    if local is None:
        raise Rejected(message(ctx, "legacy", invalid_date))
    span = _span(ctx, values["TimeSpan"])
    instant = resolve_local(ctx, zone, local, ambiguity(ctx, values["DstAmbiguity"]))
    return ctx.respond(views.calculation(zone, instant, span, sign))


endpoint("POST /api/calculation/current/increment")(partial(calculate_current, sign=1))
endpoint("POST /api/calculation/current/decrement")(partial(calculate_current, sign=-1))
endpoint("POST /api/calculation/custom/increment")(
    partial(calculate_custom, sign=1, invalid_date="Invalid DateTime or incorrect format used.")
)
endpoint("POST /api/calculation/custom/decrement")(
    partial(calculate_custom, sign=-1, invalid_date="Invalid DateTime format.")
)
