"""End-to-end scenarios for the read-only (GET) side of timeapi.io.

Each runs against the fake (default), the real API (``--target=real``) or both with a compliance
report (``--target=parity``). Assertions hold on both sides: they check behavior (the time is now,
the zone is consistent across endpoints), never recorded values.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient

NOW_TOLERANCE_S: Final = 30.0
"""How far "now" may be from this machine's clock (network, clock skew)."""

ZONES: Final = [
    "Europe/Amsterdam",
    "America/New_York",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "Australia/Lord_Howe",
    "Pacific/Chatham",
    "Europe/Dublin",
    "Africa/Casablanca",
    "Antarctica/Troll",
    "America/Santiago",
    "Asia/Tehran",
    "Pacific/Kiritimati",
    "Etc/GMT+12",
    "UTC",
    "EST",
    "US/Pacific",
    "America/Edmonton",  # canary: changes rules in tzdata 2026c (drift of the server's tz database)
]
COORDINATES: Final = [
    ("38.9", "-77.03"),
    ("52.3667", "4.8945"),
    ("-33.8688", "151.2093"),
    ("27.7172", "85.324"),
    ("-43.95", "-176.55"),
    ("64.1466", "-21.9426"),
    ("0", "0"),
    ("90", "180"),
    ("-90", "-180"),
    ("30", "-40"),
    ("-10", "-140"),
    ("1.3521", "103.8198"),
    ("40.4168", "-3.7038"),
    ("-34.6037", "-58.3816"),
]
IPS: Final = ["8.8.8.8", "1.1.1.1", "77.88.8.8", "180.76.76.76", "200.221.11.100", "81.2.69.142"]


def _iso_seconds(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def assert_now(text: str) -> None:
    """An ISO timestamp with offset close to now."""
    assert abs(_iso_seconds(text) - time.time()) < NOW_TOLERANCE_S, text


# -- v1: current time ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("leaf", "scale"), [("unix", 1), ("unix_ms", 1e3), ("unix_us", 1e6), ("unix_ns", 1e9)]
)
async def test_unix_timestamps_are_now(api: ApiClient, leaf: str, scale: float) -> None:
    response = await api.get(f"/api/v1/time/current/{leaf}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    value = response.json()["unix_timestamp"]
    assert isinstance(value, int)
    assert abs(value / scale - time.time()) < NOW_TOLERANCE_S


async def test_utc_time_is_now(api: ApiClient) -> None:
    body = (await api.get("/api/v1/time/current/utc")).json()
    assert body["utc_time"].endswith("Z")
    assert len(body["utc_time"].split(".")[1]) <= 8  # up to 7 digits (zeros trimmed) + Z
    assert_now(body["utc_time"][:26] + "Z")


@pytest.mark.parametrize("zone", ZONES)
async def test_current_time_in_zone(api: ApiClient, zone: str) -> None:
    response = await api.get("/api/v1/time/current/zone", params={"timezone": zone})
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == zone
    assert_now(body["date_time"])
    assert body["date"] == body["date_time"][:10]
    assert body["time"] == body["date_time"][11:26]


async def test_current_time_zone_name_is_case_insensitive(api: ApiClient) -> None:
    lower = (await api.get("/api/v1/time/current/zone", params={"timezone": "asia/tokyo"})).json()
    upper = (await api.get("/api/v1/time/current/zone", params={"TimeZone": "ASIA/TOKYO"})).json()
    assert lower["timezone"] == upper["timezone"] == "Asia/Tokyo"


async def test_current_time_first_of_repeated_values_wins(api: ApiClient) -> None:
    response = await api.get("/api/v1/time/current/zone?timezone=Asia/Tokyo&timezone=Europe/Paris")
    assert response.json()["timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize("value", ["Mars/Phobos", "Amsterdam", " UTC", "Factory", "GMT+2"])
async def test_current_time_invalid_zone(api: ApiClient, value: str) -> None:
    response = await api.get("/api/v1/time/current/zone", params={"timezone": value})
    assert response.status_code == 400
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == "Invalid Timezone"


@pytest.mark.parametrize("query", ["", "?timezone="])
async def test_current_time_zone_required(api: ApiClient, query: str) -> None:
    response = await api.get(f"/api/v1/time/current/zone{query}")
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json; charset=utf-8"
    assert response.json()["errors"] == {"timezone": ["The timezone field is required."]}


@pytest.mark.parametrize(("latitude", "longitude"), COORDINATES)
async def test_current_time_at_coordinate(api: ApiClient, latitude: str, longitude: str) -> None:
    response = await api.get(
        "/api/v1/time/current/coordinate", params={"latitude": latitude, "longitude": longitude}
    )
    assert response.status_code == 200
    assert_now(response.json()["date_time"])


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ({"latitude": "90.5", "longitude": "0"}, 400),
        ({"latitude": "0", "longitude": "-181"}, 400),
        ({"latitude": "91", "longitude": "181"}, 400),
        ({"latitude": "abc", "longitude": "1"}, 400),
        ({"latitude": "", "longitude": "x"}, 400),
        ({"latitude": "1,5", "longitude": "2"}, 200),
        ({"latitude": "52.3"}, 200),
        ({}, 200),
        ({"LATITUDE": " -33.9 ", "Longitude": "18.4"}, 200),
        ({"latitude": "5e1", "longitude": "1E1"}, 200),
    ],
)
async def test_coordinate_binding(api: ApiClient, query: dict[str, str], status: int) -> None:
    response = await api.get("/api/v1/time/current/coordinate", params=query)
    assert response.status_code == status


@pytest.mark.parametrize("address", [*IPS, "8.8.8", "134744072", "::ffff:1.1.1.1", "127.0.0.1"])
async def test_current_time_by_ip(api: ApiClient, address: str) -> None:
    response = await api.get("/api/v1/time/current/ip", params={"ipAddress": address})
    assert response.status_code == 200
    assert_now(response.json()["date_time"])


@pytest.mark.parametrize("address", ["999.1.1.1", "not-an-ip", " 8.8.8.8", "1.2.3.4.5"])
async def test_current_time_by_invalid_ip(api: ApiClient, address: str) -> None:
    response = await api.get("/api/v1/time/current/ip", params={"ipAddress": address})
    assert (response.status_code, response.text) == (400, "Invalid IP address")


async def test_current_time_ip_required(api: ApiClient) -> None:
    response = await api.get("/api/v1/time/current/ip")
    assert response.json()["errors"] == {"ipAddress": ["The ipAddress field is required."]}


# -- v1: zone info -------------------------------------------------------------------------------


@pytest.mark.parametrize("zone", ZONES)
async def test_zone_info(api: ApiClient, zone: str) -> None:
    response = await api.get("/api/v1/timezone/zone", params={"timeZone": zone})
    assert response.status_code == 200
    body = response.json()
    assert_now(body["local_time"])
    assert_now(body["utc_time"])
    assert body["current_utc_offset_seconds"] == body["standard_utc_offset_seconds"] + (
        body["dst_offset_seconds"] if body["dst_active"] else 0
    )
    if body["has_dst"]:
        assert body["dst_from"] < body["dst_until"]
    else:
        assert body["dst_from"] is None


@pytest.mark.parametrize("value", ["Mars/Phobos", "", "EUROPE/Nowhere"])
async def test_zone_info_unknown(api: ApiClient, value: str) -> None:
    response = await api.get("/api/v1/timezone/zone", params={"timeZone": value})
    assert response.status_code == 400


@pytest.mark.parametrize(("latitude", "longitude"), COORDINATES)
async def test_zone_info_at_coordinate(api: ApiClient, latitude: str, longitude: str) -> None:
    response = await api.get(
        "/api/v1/timezone/coordinate", params={"latitude": latitude, "longitude": longitude}
    )
    assert response.status_code == 200


async def test_zone_info_coordinate_errors(api: ApiClient) -> None:
    assert (
        await api.get("/api/v1/timezone/coordinate", params={"latitude": "-91"})
    ).status_code == 400
    response = await api.get("/api/v1/timezone/coordinate", params={"longitude": "y"})
    assert response.json()["errors"] == {"longitude": ["The value 'y' is not valid."]}


async def test_available_zones(api: ApiClient) -> None:
    zones = (await api.get("/api/v1/timezone/availabletimezones")).json()
    assert "Europe/Amsterdam" in zones
    assert "Factory" not in zones


# -- cross-endpoint consistency ------------------------------------------------------------------


async def test_listed_zones_are_accepted_everywhere(api: ApiClient) -> None:
    zones = (await api.get("/api/v1/timezone/availabletimezones")).json()
    for zone in zones[::60]:
        info = await api.get("/api/v1/timezone/zone", params={"timeZone": zone})
        legacy = await api.get("/api/timezone/zone", params={"timeZone": zone})
        assert info.status_code == legacy.status_code == 200
        assert (
            info.json()["current_utc_offset_seconds"]
            == legacy.json()["currentUtcOffset"]["seconds"]
        )


async def test_coordinate_zone_matches_zone_info(api: ApiClient) -> None:
    query = {"latitude": "-31.55", "longitude": "159.08"}
    current = (await api.get("/api/v1/time/current/coordinate", params=query)).json()
    info = (await api.get("/api/v1/timezone/coordinate", params=query)).json()
    by_name = (await api.get("/api/v1/timezone/zone", params={"timeZone": info["timezone"]})).json()
    assert current["timezone"] == info["timezone"] == by_name["timezone"]
    assert current["utc_offset_seconds"] == by_name["current_utc_offset_seconds"]


@pytest.mark.parametrize("address", IPS)
async def test_ip_zone_is_the_same_on_both_apis(api: ApiClient, address: str) -> None:
    v1 = (await api.get("/api/v1/time/current/ip", params={"ipAddress": address})).json()
    legacy = (await api.get("/api/timezone/ip", params={"ipAddress": address})).json()
    assert v1["timezone"] == legacy["timeZone"]


async def test_unix_and_utc_agree(api: ApiClient) -> None:
    unix = (await api.get("/api/v1/time/current/unix")).json()["unix_timestamp"]
    utc = (await api.get("/api/v1/time/current/utc")).json()["utc_time"]
    assert abs(_iso_seconds(utc[:26] + "+00:00") - unix) < NOW_TOLERANCE_S


# -- legacy (v0.99) ------------------------------------------------------------------------------


@pytest.mark.parametrize("zone", ZONES[:8])
async def test_legacy_current_time(api: ApiClient, zone: str) -> None:
    response = await api.get("/api/time/current/zone", params={"timeZone": zone})
    body = response.json()
    assert body["timeZone"] == zone
    assert body["dateTime"].startswith(f"{body['year']:04d}-{body['month']:02d}-{body['day']:02d}")
    assert body["date"] == f"{body['month']:02d}/{body['day']:02d}/{body['year']:04d}"


async def test_legacy_mixed_case_path(api: ApiClient) -> None:
    response = await api.get("/api/Time/current/zone", params={"timeZone": "Asia/Tokyo"})
    assert response.json()["timeZone"] == "Asia/Tokyo"


@pytest.mark.parametrize("query", [{"timeZone": "Mars/Phobos"}, {"timeZone": ""}, {}])
async def test_legacy_current_time_errors(api: ApiClient, query: dict[str, str]) -> None:
    response = await api.get("/api/time/current/zone", params=query)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize("zone", ZONES)
async def test_legacy_zone_info(api: ApiClient, zone: str) -> None:
    body = (await api.get("/api/timezone/zone", params={"timeZone": zone})).json()
    assert body["hasDayLightSaving"] == (body["dstInterval"] is not None)


async def test_legacy_zone_info_unknown_is_404(api: ApiClient) -> None:
    response = await api.get("/api/timezone/zone", params={"timeZone": "Mars/Phobos"})
    assert response.status_code == 404
    assert response.json()["title"] == "Not Found"


@pytest.mark.parametrize(("latitude", "longitude"), COORDINATES[:6])
async def test_legacy_coordinate_endpoints(api: ApiClient, latitude: str, longitude: str) -> None:
    query = {"latitude": latitude, "longitude": longitude}
    current = (await api.get("/api/time/current/coordinate", params=query)).json()
    info = (await api.get("/api/timezone/coordinate", params=query)).json()
    assert current["timeZone"] == info["timeZone"]


async def test_legacy_coordinate_errors(api: ApiClient) -> None:
    lat = await api.get("/api/timezone/coordinate", params={"latitude": "95", "longitude": "0"})
    assert lat.json() == "Latitude has to between 90 and -90"
    junk = await api.get("/api/time/current/coordinate", params={"latitude": "z"})
    assert junk.headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize("address", [*IPS[:3], "10.1.2.3", "bad"])
async def test_legacy_ip_endpoints(api: ApiClient, address: str) -> None:
    current = await api.get("/api/time/current/ip", params={"ipAddress": address})
    info = await api.get("/api/timezone/ip", params={"ipAddress": address})
    assert current.status_code == info.status_code


async def test_legacy_available_zones_match_v1(api: ApiClient) -> None:
    legacy = (await api.get("/api/timezone/availabletimezones")).json()
    v1 = (await api.get("/api/v1/timezone/availabletimezones")).json()
    assert legacy == v1


@pytest.mark.parametrize(
    "date",
    [
        "2021-03-14",
        "2024-02-29",
        "1900-02-28",
        "2000-02-29",
        "0001-01-01",
        "9999-12-31",
        "2026-07-04",
    ],
)
async def test_day_of_week_and_year(api: ApiClient, date: str) -> None:
    week = await api.get(f"/api/conversion/dayoftheweek/{date}")
    year = await api.get(f"/api/conversion/dayoftheyear/{date}")
    assert week.status_code == year.status_code == 200
    assert 1 <= year.json()["day"] <= 366


@pytest.mark.parametrize(
    "date", ["2023-02-29", "1900-02-29", "2021-1-01", "today", "2021-03-14T00:00"]
)
async def test_day_conversions_reject_bad_dates(api: ApiClient, date: str) -> None:
    week = await api.get(f"/api/conversion/dayoftheweek/{date}")
    year = await api.get(f"/api/conversion/dayoftheyear/{date}")
    assert week.json() == year.json() == "Invalid date"


# -- routing -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/health/check", "/API/HEALTH/CHECK", "/api/health/check/", "/api/V1/Time/Current/Unix/"],
)
async def test_routing_is_lenient(api: ApiClient, path: str) -> None:
    assert (await api.get(path)).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/v2/time", "/api/v1/time/current/zone/x", "/api/conversion/dayoftheweek/"]
)
async def test_unknown_routes(api: ApiClient, path: str) -> None:
    response = await api.get(path)
    assert response.status_code == 404
    assert response.content == b""


async def test_wrong_method(api: ApiClient) -> None:
    head = await api.request("HEAD", "/api/v1/time/current/utc")
    assert (head.status_code, head.headers["allow"]) == (405, "GET")
    get = await api.get("/api/calculation/custom/increment")
    assert (get.status_code, get.headers["allow"]) == (405, "POST")


async def test_now_is_now_on_both_api_generations(api: ApiClient) -> None:
    v1 = (await api.get("/api/v1/timezone/zone", params={"timeZone": "UTC"})).json()
    legacy = (await api.get("/api/time/current/zone", params={"timeZone": "UTC"})).json()
    assert abs(_iso_seconds(legacy["dateTime"][:26] + "+00:00") - v1["unix_timestamp"]) < 5
    assert abs(datetime.now(UTC).timestamp() - v1["unix_timestamp"]) < NOW_TOLERANCE_S
