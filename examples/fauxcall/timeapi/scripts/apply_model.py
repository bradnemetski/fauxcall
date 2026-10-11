"""Apply the hand-modeled parts of data/model.json (idempotent; re-run after `analyze --apply`).

Everything here is derived from recordings (exchange ids in notes) or, where marked, from the
documentation / ASP.NET Core defaults because it could not be recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "data" / "model.json"

V1_ISO6 = {"kind": "iso", "fraction_digits": 6, "fraction_mode": "fixed", "tz_style": "+HH:MM"}
ISO7_TRIM = {"kind": "iso", "fraction_digits": 7, "fraction_mode": "trim", "tz_style": "none"}
TRACE = {
    "kind": "random",
    "scope": "body",
    "path": "traceId",
    "generator": "traceparent",
    "note": "ASP.NET traceId in validation problems",
}


def now(path: str, fmt: dict, zone: str = "UTC") -> dict:
    return {"kind": "now", "scope": "body", "path": path, "format": fmt, "zone": zone}


def computed(path: str, note: str = "derived from the clock in the handler") -> dict:
    return {"kind": "computed", "scope": "body", "path": path, "function": "views", "note": note}


V1_CURRENT = [
    now("date_time", V1_ISO6, "field:timezone"),
    now("date", {"kind": "date"}, "field:timezone"),
    now("time", {"kind": "time", "fraction_digits": 6}, "field:timezone"),
    TRACE,
]
V1_TIMEZONE = [
    now("local_time", V1_ISO6, "field:timezone"),
    now("utc_time", V1_ISO6),
    now("unix_timestamp", {"kind": "epoch", "epoch_unit": "s"}),
    TRACE,
]
PARTS = ["year", "month", "day", "hour", "minute", "seconds", "milliSeconds", "date", "time"]
LEGACY_CURRENT = [
    now("dateTime", ISO7_TRIM, "field:timeZone"),
    *[computed(p) for p in PARTS],
    TRACE,
]
LEGACY_TIMEZONE = [now("currentLocalTime", ISO7_TRIM, "field:timeZone"), TRACE]
CALC_CURRENT = [
    now("originalDateTime", ISO7_TRIM, "field:timeZone"),
    {
        "kind": "relative",
        "scope": "body",
        "path": "calculationResult.dateTime",
        "format": ISO7_TRIM,
    },
    *[computed(f"calculationResult.{p}") for p in PARTS],
    TRACE,
]

ENDPOINTS: dict[str, dict] = {
    "GET /api/v1/time/current/unix": {
        "fields": [now("unix_timestamp", {"kind": "epoch", "epoch_unit": "s"})]
    },
    "GET /api/v1/time/current/unix_ms": {
        "fields": [now("unix_timestamp", {"kind": "epoch", "epoch_unit": "ms"})]
    },
    "GET /api/v1/time/current/unix_us": {
        "fields": [now("unix_timestamp", {"kind": "epoch", "epoch_unit": "us"})]
    },
    "GET /api/v1/time/current/unix_ns": {
        "fields": [now("unix_timestamp", {"kind": "epoch", "epoch_unit": "ns"})],
        "notes": ["Nanoseconds come in steps of 100 (.NET ticks), e.g. 000039."],
    },
    "GET /api/v1/time/current/utc": {
        "fields": [
            now(
                "utc_time",
                {"kind": "iso", "fraction_digits": 7, "fraction_mode": "trim", "tz_style": "Z"},
            )
        ],
        "notes": ["Fraction trims trailing zeros (utc-spell recordings: 11:05:04.369445Z)."],
    },
    "GET /api/v1/time/current/zone": {
        "fields": V1_CURRENT,
        "notes": [
            "timezone matched case-insensitively, canonical spelling echoed (000085, 000086); param name case-insensitive (000087, 000088); first of repeated values wins (000097).",
            "Missing/empty -> application/problem+json validation error (000095, 000096); unknown or padded zone -> 400 text/plain 'Invalid Timezone' (000089, 000094); 'Factory' rejected (000521).",
            "Abbreviation zones of the tz database (EST, CET) are valid (000092, 000093).",
        ],
    },
    "GET /api/v1/time/current/coordinate": {
        "fields": V1_CURRENT,
        "notes": [
            "latitude/longitude bind as float (single precision, invariant culture, ',' = thousands separator: 000171); absent = 0 (000167-000169); '' -> \"The value '' is invalid.\" and junk -> \"The value 'abc' is not valid.\" (000165, 000170).",
            "Longitude range checked before latitude (000171); bounds inclusive (000145-000148).",
            "Land: timezone-boundary-builder polygons (timezonefinder); open sea: Etc/GMT±round_half_toward_zero(lon/15), 0 -> UTC (000144, 000149, 000524-000527). NaN latitude -> Antarctica/McMurdo (000522).",
        ],
    },
    "GET /api/v1/time/current/ip": {
        "fields": V1_CURRENT,
        "notes": [
            "IPAddress.TryParse semantics: inet_aton forms (8.8.8 = 8.8.0.8, hex, integer), IPv6, v4-mapped IPv6; padded -> 400 'Invalid IP address'.",
            "No GeoIP database: zones of recorded addresses (ip_zones.json from 55 addresses); unknown public IPv4 -> recorded /24 or /16 neighbour, else UTC; private/loopback/IPv6 -> UTC (000228-000230). LOWER FIDELITY for unrecorded addresses.",
        ],
    },
    "GET /api/v1/timezone/zone": {
        "fields": V1_TIMEZONE,
        "notes": [
            "dst_* fields follow NodaTime ZoneInterval semantics: the current interval if it has savings, else the next one if it has; savings may be negative (Europe/Dublin 000135, Africa/Casablanca 000110).",
            "Unknown zone -> 400 text/plain 'Timezone not found' (000126).",
        ],
    },
    "GET /api/v1/timezone/coordinate": {"fields": V1_TIMEZONE},
    "GET /api/v1/timezone/availabletimezones": {
        "role": "replay",
        "notes": [
            "Static: 597 zones = tzdata zones minus 'Factory', in the server's order (000057)."
        ],
    },
    "GET /": {
        "role": "replay",
        "notes": ["The website's home page (HTML), served as recorded (000529)."],
    },
    "GET /api/health/check": {"notes": ["Empty 200 without content type (000005)."]},
    "GET /api/time/current/zone": {"fields": LEGACY_CURRENT},
    "GET /api/time/current/coordinate": {"fields": LEGACY_CURRENT},
    "GET /api/time/current/ip": {"fields": LEGACY_CURRENT},
    "GET /api/timezone/availabletimezones": {"role": "replay"},
    "GET /api/timezone/zone": {
        "fields": LEGACY_TIMEZONE,
        "errors": [{"when": "not_found", "template": "legacy_not_found"}],
        "notes": ["Unknown zone -> 404 ProblemDetails 'Not Found' (000284)."],
    },
    "GET /api/timezone/coordinate": {"fields": LEGACY_TIMEZONE},
    "GET /api/timezone/ip": {"fields": LEGACY_TIMEZONE},
    "GET /api/conversion/dayoftheweek/{date}": {
        "fields": [],
        "notes": [
            'Strict yyyy-MM-dd, years 0001-9999; anything else (incl. trailing space 000516) -> "Invalid date".'
        ],
    },
    "GET /api/conversion/dayoftheyear/{date}": {"fields": []},
    "POST /api/conversion/converttimezone": {
        "fields": [TRACE],
        "notes": [
            "dstAmbiguity is required (missing/null -> validation error, '' allowed: 000323, 000448); errors listed DateTime, ToTimeZone, DstAmbiguity, FromTimeZone (000334).",
            "Ambiguous local time with '' -> 400 message (000450); gaps shift forward (000512).",
            "ASSUMED (not recorded): an unparsable dateTime answers 'Invalid DateTime format.' as translate does; check order from-zone, to-zone, dateTime, dstAmbiguity.",
        ],
    },
    "POST /api/conversion/translate": {
        "fields": [TRACE],
        "notes": [
            "CLDR full date + medium time (Babel); the real server uses ICU, same data (000335-000340, U+202F before AM in 000338).",
            "Only two-letter codes (000345 en-US rejected), case-insensitive (000342).",
            "ASSUMED: required-field order DateTime, LanguageCode.",
        ],
    },
    "POST /api/calculation/current/increment": {
        "fields": CALC_CURRENT,
        "notes": [
            "timeSpan must be d:hh:mm:ss or d:hh:mm:ss.fff (exactly 3 digits: 000464); arithmetic on instants (DST aware: 000346).",
            "ASSUMED: required-field order TimeSpan, TimeZone; zone checked before span.",
        ],
    },
    "POST /api/calculation/current/decrement": {"fields": CALC_CURRENT},
    "POST /api/calculation/custom/increment": {
        "fields": [TRACE],
        "notes": [
            "Invalid dateTime message differs per endpoint: increment 'Invalid DateTime or incorrect format used.' (000454), decrement 'Invalid DateTime format.' (000460, 000511).",
            "Required-field order DateTime, TimeSpan, TimeZone, DstAmbiguity (000458); '' for the others is 'required' (000463).",
        ],
    },
    "POST /api/calculation/custom/decrement": {"fields": [TRACE]},
}

PROBLEM_404 = {
    "type": "https://tools.ietf.org/html/rfc9110#section-15.5.5",
    "title": "Not Found",
    "status": 404,
    "traceId": "{{traceparent}}",
}
TEMPLATES = [
    {"name": "unknown_route", "status": 404, "examples": ["000003", "000010", "000517"]},
    {
        "name": "method_not_allowed",
        "status": 405,
        "headers": {"Allow": "GET"},
        "examples": ["000008"],
    },
    {
        "name": "legacy_not_found",
        "status": 404,
        "content_type": "application/json; charset=utf-8",
        "body_json": PROBLEM_404,
        "examples": ["000284"],
    },
    {
        "name": "unsupported_media_type",
        "status": 415,
        "content_type": "application/json; charset=utf-8",
        "body_json": {
            "type": "https://tools.ietf.org/html/rfc9110#section-15.5.16",
            "title": "Unsupported Media Type",
            "status": 415,
            "traceId": "{{traceparent}}",
        },
    },
    {
        "name": "v1_invalid_timezone",
        "status": 400,
        "content_type": "text/plain; charset=utf-8",
        "body_text": "Invalid Timezone",
        "examples": ["000089"],
    },
    {
        "name": "v1_timezone_not_found",
        "status": 400,
        "content_type": "text/plain; charset=utf-8",
        "body_text": "Timezone not found",
        "examples": ["000126"],
    },
    {
        "name": "legacy_invalid_timezone",
        "status": 400,
        "content_type": "application/json; charset=utf-8",
        "body_json": "Invalid Timezone",
        "examples": ["000257"],
    },
]


SERVER_ERROR_EXCHANGE = "000579"
"""custom/decrement of 0001-01-01 by one day: .NET DateTime underflow, the site's 500 page."""


def server_error_template() -> dict:
    path = next((ROOT / "data" / "recordings").glob(f"*/{SERVER_ERROR_EXCHANGE}.json"))
    response = json.loads(path.read_text(encoding="utf-8"))["response"]
    return {
        "name": "server_error",
        "status": 500,
        "content_type": "text/html; charset=utf-8",
        "body_text": response["body"]["text"],
        "examples": [SERVER_ERROR_EXCHANGE],
    }


def main() -> None:
    model = json.loads(MODEL.read_text(encoding="utf-8"))
    if not any(e["id"] == "GET /" for e in model["endpoints"]):
        model["endpoints"].append(
            {
                "id": "GET /",
                "method": "GET",
                "path": "/",
                "summary": "Home page",
                "tags": ["Website"],
                "role": "replay",
                "params": [],
                "responses": [],
            }
        )
    for endpoint in model["endpoints"]:
        spec = ENDPOINTS[endpoint["id"]]
        endpoint["role"] = spec.get("role", "computed")
        if endpoint["role"] == "computed":
            endpoint["handler"] = "fauxcall_timeapi.handlers"
        endpoint["fields"] = spec.get("fields", [])
        endpoint["errors"] = spec.get("errors", [])
        endpoint["notes"] = spec.get("notes", [])
        for param in endpoint.get("params", []):
            # The handlers bind parameters like ASP.NET does; the engine must not pre-validate.
            param["schema"] = {"type": "string"}
            param["required"] = False
            param["on_invalid"] = "coerce"
        if endpoint["method"] == "POST":
            body = endpoint.get("request_body") or {}
            body.update(
                {"content_types": ["application/json"], "required": False, "validate_schema": False}
            )
            endpoint["request_body"] = body
    model["routing"] = {
        "case_insensitive": True,
        "trailing_slash": "ignore",
        "unknown_route_template": "unknown_route",
        "method_not_allowed_template": "method_not_allowed",
        "spec_paths": ["/swagger/v1/swagger.json", "/swagger/v0.99/swagger.json"],
    }
    model["errors"] = [*TEMPLATES, server_error_template()]
    model["source"]["spec_file"] = "source/openapi.json"
    model["notes"] = [
        "ASSUMED (no recording, ASP.NET Core defaults): POST bodies without a JSON content type -> 415 ProblemDetails; empty body -> errors {'': ['A non-empty request body is required.']}; malformed/non-object JSON and non-string fields -> simplified validation problems (real messages name System.Text.Json paths/positions).",
        "ASSUMED: DST gaps shift forward for every dstAmbiguity value (only '' recorded: 000457, 000512); custom-calculation check order zone -> dateTime -> span -> dstAmbiguity; translate accepts any two-letter code Babel knows (aliases such as 'iw' may differ).",
        "Results outside DateTime (years 1-9999) -> the site's HTML 500 page (000579, custom/decrement); applied to every conversion/calculation. Spans above TimeSpan.MaxValue are invalid spans (cc-maxspan).",
        "Translate uses the culture's default calendar: Thai Buddhist era (tr-th), Persian Solar Hijri with .NET's 'yyyy MMMM d, dddd' (tr-fa; day padding unverified); ar and other cultures assumed Gregorian.",
        "Blank query values bind as null -> 'required' (ws-zone, ws-ip); blank floats -> \"The value ' ' is invalid.\" (ws-lat); only ASCII digits parse (uni-lat, uni-date); IP: bracketed IPv6 accepted, junk/overflow rejected (ip-bracket, ip-junk, ip-big).",
        "Time values: on hosts with a microsecond clock (macOS) the 7th fraction digit is filled from the seeded request RNG; Linux clocks give it natively.",
        "Drift watch: tzdata pin 2026.2 rests on Africa/Casablanca (000071); America/Edmonton (canary-edmonton) and Casablanca are e2e canaries. Negative DST *while active* (Europe/Dublin after 2026-10-25) is modeled from tz semantics, not yet recorded — re-probe then.",
        "Two specs on one server: v1 (/swagger/v1/swagger.json) and legacy v0.99 (/swagger/v0.99/swagger.json, source/openapi-v0.99.json), both in scope.",
        "Routing is case-insensitive and ignores a trailing slash (000006, 000007, 000019, 000020); unknown routes are an empty 404, wrong methods an empty 405 with Allow (000008, 000514).",
        "tz database: the server's matches python tzdata 2026.2 (Africa/Casablanca rules changed in 2026.3); the fake pins tzdata==2026.2.",
        "No authentication and no documented rate limits; no rate-limit headers were observed.",
    ]
    MODEL.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("model updated")


if __name__ == "__main__":
    main()
