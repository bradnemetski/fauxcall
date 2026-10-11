"""Handlers beyond the recordings: consistency across endpoints, time travel, unrecorded inputs."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import httpx2
import pytest

from fauxcall_timeapi import dotnet, geo, tzengine, views
from fauxcall_timeapi.app import build_app
from fauxcall_timeapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(Settings(fast=True, log_level="WARNING"))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        yield client
        await client.post("/__fauxcall/clock", json={"op": "real"})


async def freeze(client: httpx2.AsyncClient, at: str) -> None:
    response = await client.post("/__fauxcall/clock", json={"op": "freeze", "at": at})
    assert response.status_code == 200


# -- consistency -----------------------------------------------------------------------------------


async def test_every_listed_zone_resolves_on_both_apis(client: httpx2.AsyncClient) -> None:
    await freeze(client, "2026-10-11T10:00:00Z")
    zones = (await client.get("/api/v1/timezone/availabletimezones")).json()
    legacy = (await client.get("/api/timezone/availabletimezones")).json()
    assert zones == legacy
    assert set(zones) == set(tzengine.zone_ids())
    for zone in zones:
        v1 = (await client.get("/api/v1/timezone/zone", params={"timeZone": zone})).json()
        old = (await client.get("/api/timezone/zone", params={"timeZone": zone})).json()
        now = (await client.get("/api/v1/time/current/zone", params={"timezone": zone})).json()
        assert v1["timezone"] == old["timeZone"] == now["timezone"] == zone
        assert v1["current_utc_offset_seconds"] == old["currentUtcOffset"]["seconds"]
        assert v1["current_utc_offset_seconds"] == now["utc_offset_seconds"]
        assert v1["has_dst"] == old["hasDayLightSaving"]
        assert v1["dst_active"] == old["isDayLightSavingActive"] == now["dst_active"]
        if v1["has_dst"]:
            interval = old["dstInterval"]
            assert interval["dstOffsetToStandardTime"]["seconds"] == v1["dst_offset_seconds"]
            assert v1["dst_from"].replace(".000000+00:00", "Z") == interval["dstStart"]


async def test_coordinate_and_ip_answers_match_zone_answers(client: httpx2.AsyncClient) -> None:
    await freeze(client, "2026-07-01T12:00:00Z")
    by_coordinate = await client.get(
        "/api/v1/timezone/coordinate", params={"latitude": "52.37", "longitude": "4.89"}
    )
    by_ip = await client.get("/api/timezone/ip", params={"ipAddress": "157.240.1.35"})
    by_zone = await client.get("/api/v1/timezone/zone", params={"timeZone": "Europe/Amsterdam"})
    assert by_coordinate.json() == by_zone.json()
    assert by_ip.json()["timeZone"] == "Europe/Amsterdam"
    assert by_ip.json()["currentUtcOffset"]["seconds"] == 7200


async def test_dst_end_seen_through_the_clock(client: httpx2.AsyncClient) -> None:
    params = {"timeZone": "Europe/Amsterdam"}
    await freeze(client, "2026-10-25T00:59:59Z")
    before = (await client.get("/api/v1/timezone/zone", params=params)).json()
    await freeze(client, "2026-10-25T01:00:00Z")
    after = (await client.get("/api/v1/timezone/zone", params=params)).json()
    assert before["dst_active"] is True
    assert before["local_time"] == "2026-10-25T02:59:59.000000+02:00"
    assert after["dst_active"] is False
    assert after["local_time"] == "2026-10-25T02:00:00.000000+01:00"
    assert after["dst_from"] == "2027-03-28T01:00:00.000000+00:00"
    assert after["dst_utc_offset_seconds"] == 7200


async def test_unix_endpoints_follow_the_clock(client: httpx2.AsyncClient) -> None:
    await freeze(client, "2026-10-11T10:00:00.123456789Z")
    seconds = (await client.get("/api/v1/time/current/unix")).json()["unix_timestamp"]
    nanos = (await client.get("/api/v1/time/current/unix_ns")).json()["unix_timestamp"]
    utc = (await client.get("/api/v1/time/current/utc")).json()["utc_time"]
    assert seconds == 1791712800
    assert 1791712800123456000 <= nanos <= 1791712800123456900  # 7th digit filled in
    assert nanos % 100 == 0  # .NET tick resolution
    assert utc.startswith("2026-10-11T10:00:00.123456")
    await freeze(client, "2026-10-11T10:00:00Z")
    whole = (await client.get("/api/v1/time/current/unix_ns")).json()["unix_timestamp"]
    assert whole == 1791712800000000000  # frozen whole seconds stay exact


# -- errors and edge inputs ------------------------------------------------------------------------


async def test_routing_quirks(client: httpx2.AsyncClient) -> None:
    unknown = await client.get("/api/v1/nothing")
    assert unknown.status_code == 404
    assert unknown.content == b""
    get_on_post = await client.get("/api/conversion/converttimezone")
    assert (get_on_post.status_code, get_on_post.headers["allow"]) == (405, "POST")
    head = await client.head("/API/Health/Check/")
    assert (head.status_code, head.headers["allow"]) == (405, "GET")
    health = await client.get("/API/Health/Check/")
    assert health.status_code == 200
    assert health.content == b""
    assert "content-type" not in health.headers


async def test_coordinate_validation(client: httpx2.AsyncClient) -> None:
    both = await client.get(
        "/api/v1/time/current/coordinate", params={"latitude": "x", "longitude": ""}
    )
    assert both.json()["errors"] == {
        "latitude": ["The value 'x' is not valid."],
        "longitude": ["The value '' is invalid."],
    }
    assert both.headers["content-type"] == "application/problem+json; charset=utf-8"
    legacy = await client.get("/api/time/current/coordinate", params={"latitude": "Infinity"})
    assert legacy.json() == "Latitude has to between 90 and -90"
    nan = await client.get("/api/v1/time/current/coordinate", params={"latitude": "NaN"})
    assert nan.json()["timezone"] == "Antarctica/McMurdo"


async def test_ip_validation(client: httpx2.AsyncClient) -> None:
    padded = await client.get("/api/v1/time/current/ip", params={"ipAddress": " 1.1.1.1"})
    assert (padded.status_code, padded.text) == (400, "Invalid IP address")
    legacy = await client.get("/api/timezone/ip", params={"ipAddress": "1.2.3.4.5"})
    assert legacy.json() == "Invalid ip address"
    missing = await client.get("/api/timezone/ip")
    assert missing.json()["errors"] == {"ipAddress": ["The ipAddress field is required."]}
    assert missing.headers["content-type"] == "application/json; charset=utf-8"


async def test_unknown_zone_on_legacy_zone_info_is_a_404_problem(
    client: httpx2.AsyncClient,
) -> None:
    response = await client.get("/api/timezone/zone", params={"timeZone": "Nowhere/Land"})
    assert response.status_code == 404
    body = response.json()
    assert (body["title"], body["status"]) == ("Not Found", 404)


async def test_day_of_the_year_and_week(client: httpx2.AsyncClient) -> None:
    assert (await client.get("/api/conversion/dayoftheyear/2024-12-31")).json() == {"day": 366}
    week = await client.get("/api/conversion/dayoftheweek/1999-12-31")
    assert week.json() == {"dayOfWeek": "Friday"}
    bad = await client.get("/api/conversion/dayoftheweek/0000-01-01")
    assert (bad.status_code, bad.json()) == (400, "Invalid date")


# -- POST bodies -----------------------------------------------------------------------------------

CONVERT = "/api/conversion/converttimezone"


async def test_body_binding_errors(client: httpx2.AsyncClient) -> None:
    wrong_type = await client.post(CONVERT, content=b"x", headers={"content-type": "text/plain"})
    assert wrong_type.status_code == 415
    empty = await client.post(CONVERT, content=b"", headers={"content-type": "application/json"})
    assert empty.json()["errors"] == {"": ["A non-empty request body is required."]}
    broken = await client.post(CONVERT, content=b"{", headers={"content-type": "application/json"})
    assert "request" in broken.json()["errors"]
    array = await client.post(CONVERT, json=[1])
    assert broken.json()["errors"].keys() == array.json()["errors"].keys()
    nulls = await client.post(
        CONVERT,
        json={"fromTimeZone": None, "dateTime": 5, "toTimeZone": "UTC", "dstAmbiguity": None},
    )
    assert list(nulls.json()["errors"]) == ["DateTime", "DstAmbiguity", "FromTimeZone"]


async def test_conversion_ambiguity_and_case_insensitive_keys(client: httpx2.AsyncClient) -> None:
    body = {
        "FROMTIMEZONE": "europe/amsterdam",
        "DateTime": "2025-10-26 02:30:00",
        "toTimeZone": "UTC",
    }
    later = await client.post(CONVERT, json={**body, "dstAmbiguity": "Later"})
    assert later.json()["conversionResult"]["dateTime"] == "2025-10-26T01:30:00"
    assert later.json()["fromTimezone"] == "Europe/Amsterdam"
    bad = await client.post(CONVERT, json={**body, "dstAmbiguity": "x"})
    assert bad.json() == "Failed parsing DstAmbiguity."
    bad_date = await client.post(
        CONVERT, json={**body, "DateTime": "2025-10-26", "dstAmbiguity": ""}
    )
    assert bad_date.json() == "Invalid DateTime format."
    bad_to = await client.post(CONVERT, json={**body, "toTimeZone": "x", "dstAmbiguity": ""})
    assert bad_to.json() == "Invalid to timezone"


async def test_translate(client: httpx2.AsyncClient) -> None:
    ok = await client.post(
        "/api/conversion/translate", json={"dateTime": "2021-03-14 17:45:00", "languageCode": "it"}
    )
    assert ok.json()["friendlyDate"] == "domenica 14 marzo 2021"
    korean = await client.post(
        "/api/conversion/translate", json={"dateTime": "2026-05-05 17:05:05", "languageCode": "ko"}
    )
    assert korean.json()["friendlyTime"] == "오후 5:05:05"
    english = await client.post(
        "/api/conversion/translate", json={"dateTime": "2026-05-05 17:05:05", "languageCode": "en"}
    )
    assert english.json()["friendlyTime"] == "5:05:05\u202fPM"
    for code in ("zz", "en-US", "e1"):
        bad = await client.post(
            "/api/conversion/translate",
            json={"dateTime": "2021-03-14 17:45:00", "languageCode": code},
        )
        assert bad.json() == "Couldn't find a language with that code"
    date = await client.post(
        "/api/conversion/translate", json={"dateTime": "2021-03-14", "languageCode": "de"}
    )
    assert date.json() == "Invalid DateTime format."
    missing = await client.post("/api/conversion/translate", json={})
    assert list(missing.json()["errors"]) == ["DateTime", "LanguageCode"]


async def test_calculations(client: httpx2.AsyncClient) -> None:
    await freeze(client, "2026-10-11T10:00:00Z")
    current = await client.post(
        "/api/calculation/current/decrement",
        json={"timeZone": "Asia/Tokyo", "timeSpan": "0:01:00:00"},
    )
    assert current.json()["calculationResult"]["dateTime"] == "2026-10-11T18:00:00"
    bad_zone = await client.post(
        "/api/calculation/current/increment", json={"timeZone": "x", "timeSpan": "0:01:00:00"}
    )
    assert bad_zone.json() == "Invalid timezone"
    custom = {"timeZone": "UTC", "dateTime": "2021-02-30 00:00:00", "timeSpan": "1:00:00:00"}
    inc = await client.post(
        "/api/calculation/custom/increment", json={**custom, "dstAmbiguity": ""}
    )
    dec = await client.post(
        "/api/calculation/custom/decrement", json={**custom, "dstAmbiguity": ""}
    )
    assert inc.json() == "Invalid DateTime or incorrect format used."
    assert dec.json() == "Invalid DateTime format."
    ambiguous = {**custom, "timeZone": "Europe/Amsterdam", "dateTime": "2025-10-26 02:30:00"}
    error = await client.post(
        "/api/calculation/custom/increment", json={**ambiguous, "dstAmbiguity": ""}
    )
    assert error.json().startswith("Time given is ambiguous.")
    span = await client.post(
        "/api/calculation/custom/increment",
        json={**ambiguous, "timeSpan": "0:24:00:00", "dstAmbiguity": "earlier"},
    )
    assert span.json() == "Invalid TimeSpan or incorrect format."


# -- building blocks -------------------------------------------------------------------------------


def test_dotnet_parsers() -> None:
    assert dotnet.parse_float("1,234.5") == 1234.5
    assert dotnet.parse_float("-Infinity") == -math.inf
    assert dotnet.parse_float("1.2,3") is None
    assert dotnet.to_single(1e300) == math.inf
    assert dotnet.json_double(2.0) == 2
    assert dotnet.json_double(2.5) == 2.5
    assert str(dotnet.parse_ip("fe80::1%eth0")) == "fe80::1"
    assert dotnet.parse_ip("gg::1") is None
    assert dotnet.parse_local("2021-13-01 00:00:00") is None
    assert dotnet.parse_local("2021-01-01 24:00:00") is None
    assert dotnet.parse_span("0:00:60:00") is None
    assert str(dotnet.parse_span("3:04:05:06.120")) == "3:04:05:06.12"
    assert dotnet.civil_from_days(dotnet.days_from_civil(-44, 3, 15)) == (-44, 3, 15)
    assert dotnet.LocalDateTime(2021, 3, 14).day_of_year == 73


def test_geo() -> None:
    assert geo.nautical_zone(-7.4) == "UTC"
    assert geo.nautical_zone(97.6) == "Etc/GMT-7"
    assert geo.zone_at(math.nan, math.nan) == "Antarctica/McMurdo"
    assert geo.zone_at(52.37, 4.89) == "Europe/Amsterdam"
    neighbour = dotnet.parse_ip("185.121.177.1")
    assert neighbour is not None
    assert geo.zone_of_ip(neighbour) == "Pacific/Auckland"  # same /24 as a recorded address
    for text, zone in [
        ("10.0.0.1", "UTC"),
        ("::ffff:8.8.8.8", "America/Los_Angeles"),
        ("2a00::1", "UTC"),
        ("100.64.0.1", "UTC"),
        ("7.7.7.7", "UTC"),
    ]:
        address = dotnet.parse_ip(text)
        assert address is not None
        assert geo.zone_of_ip(address) == zone


def test_tzengine_rules() -> None:
    assert tzengine.canonical_zone("factory") is None
    assert tzengine.offset_at("Asia/Kolkata", 0) == 19800
    year = 2030  # beyond the explicit transitions of most zones: footer rules
    start, _ = tzengine.interval_at("America/New_York", 1_900_000_000)
    assert start.name in {"EDT", "EST"}
    assert tzengine._rule_day("J60", year) == (tzengine._days_from_civil(year, 3, 1), 7200)
    assert tzengine._rule_day("59/3", year) == (tzengine._days_from_civil(year, 3, 1), 10800)
    assert tzengine._parse_footer("") is None
    assert tzengine._parse_footer("???") is None
    assert tzengine._parse_footer("<+03>-3") == 10800
    rule = tzengine._parse_footer("AAA3BBB,M3.2.0,M11.1.0")
    assert isinstance(rule, tzengine._Rule)
    assert rule.dst_offset == rule.std_offset + 3600
    with pytest.raises(ValueError, match="TZif"):
        tzengine._read_tzif(b"nope")
    gap = dotnet.parse_local("2026-03-29 02:30:00")
    assert gap is not None
    _, used = views.resolve("Europe/Amsterdam", gap, "")
    assert (used.hour, used.minute) == (3, 30)
    assert tzengine.gap_offset("UTC", 0) == 0


async def test_review_fixes(client: httpx2.AsyncClient) -> None:
    blank = await client.get("/api/v1/time/current/zone", params={"timezone": "  "})
    assert blank.json()["errors"] == {"timezone": ["The timezone field is required."]}
    lat = await client.get("/api/v1/time/current/coordinate", params={"latitude": " "})
    assert lat.json()["errors"] == {"latitude": ["The value ' ' is invalid."]}
    digits = await client.get("/api/v1/time/current/coordinate", params={"latitude": "\u0665"})
    assert digits.status_code == 400
    overflow = await client.post(
        "/api/calculation/custom/increment",
        json={
            "timeZone": "UTC",
            "dateTime": "9999-12-31 00:00:00",
            "timeSpan": "2:00:00:00",
            "dstAmbiguity": "",
        },
    )
    assert overflow.status_code == 500
    assert overflow.headers["content-type"] == "text/html; charset=utf-8"
    huge = await client.post(
        "/api/calculation/current/increment",
        json={"timeZone": "UTC", "timeSpan": "99999999:00:00:00"},
    )
    assert huge.json() == "Invalid TimeSpan or incorrect format."
    no_type = await client.post(CONVERT, content=b"{}")
    assert no_type.status_code == 415
    for code, text in [("th", "พ.ศ. 2569"), ("fa", "1405 مهر 19, یکشنبه")]:
        answer = await client.post(
            "/api/conversion/translate",
            json={"dateTime": "2026-10-11 09:00:00", "languageCode": code},
        )
        assert text in answer.json()["friendlyDate"]


def test_ip_and_calendar_helpers() -> None:
    assert str(dotnet.parse_ip("010.0x10.1")) == "8.16.0.1"
    assert dotnet.parse_ip("1.2.3.256") is None
    assert dotnet.parse_ip("0x") is not None
    assert dotnet.parse_ip("[::1]") is not None
    assert dotnet.persian_date(2024, 3, 20) == (1403, 1, 1)
    assert dotnet.persian_date(2021, 3, 20) == (1399, 12, 30)
