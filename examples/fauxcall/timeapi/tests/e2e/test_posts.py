"""End-to-end scenarios for the POST endpoints (legacy conversions and calculations).

They compute and store nothing, but the user approved at most 20 real calls per endpoint for the
whole build; recording used most of them. Each scenario below is one call, chosen to exercise
what recordings could not show together. Total per parity run: converttimezone 1, translate 3,
current/increment 3, current/decrement 3, custom/increment 1, custom/decrement 2. Real calls need
``--allow-real-writes``; without it these skip on the real side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fauxcall.testing import ApiClient


async def test_convert_across_dst_rules(api: ApiClient) -> None:
    response = await api.post(
        "/api/conversion/converttimezone",
        json={
            "fromTimeZone": "America/Sao_Paulo",
            "dateTime": "2026-11-01 08:15:30.250000",
            "toTimeZone": "Australia/Lord_Howe",
            "dstAmbiguity": "",
        },
    )
    assert response.status_code == 200
    result = response.json()["conversionResult"]
    assert result["timeZone"] == "Australia/Lord_Howe"
    assert result["dstActive"] is True


@pytest.mark.parametrize(
    ("date_time", "code"),
    [("2026-12-24 18:30:00", "it"), ("2027-01-01 00:00:01", "pt"), ("2026-05-05 05:05:05", "ko")],
)
async def test_translate_languages(api: ApiClient, date_time: str, code: str) -> None:
    response = await api.post(
        "/api/conversion/translate", json={"dateTime": date_time, "languageCode": code}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["friendlyDateTime"] == f"{body['friendlyDate']} {body['friendlyTime']}"


@pytest.mark.parametrize(
    ("zone", "span"),
    [
        ("Pacific/Chatham", "0:12:00:00"),
        ("America/Santiago", "200:00:00:00.250"),
        ("UTC", "1:2:3:4"),
    ],
)
async def test_current_increment(api: ApiClient, zone: str, span: str) -> None:
    response = await api.post(
        "/api/calculation/current/increment", json={"timeZone": zone, "timeSpan": span}
    )
    assert response.status_code in {200, 400}


@pytest.mark.parametrize(
    ("zone", "span"),
    [("Europe/Dublin", "30:00:00:00"), ("asia/kathmandu", "0:00:00:01.999"), ("Etc/GMT-14", "")],
)
async def test_current_decrement(api: ApiClient, zone: str, span: str) -> None:
    response = await api.post(
        "/api/calculation/current/decrement", json={"timeZone": zone, "timeSpan": span}
    )
    assert response.status_code in {200, 400}


async def test_custom_increment_across_spring_forward(api: ApiClient) -> None:
    response = await api.post(
        "/api/calculation/custom/increment",
        json={
            "timeZone": "America/New_York",
            "dateTime": "2026-03-08 01:30:00",
            "timeSpan": "0:01:00:00",
            "dstAmbiguity": "",
        },
    )
    assert response.json()["calculationResult"]["dateTime"] == "2026-03-08T03:30:00"


@pytest.mark.parametrize(
    "body",
    [
        {
            "timeZone": "America/New_York",
            "dateTime": "2026-11-01 01:30:00",
            "timeSpan": "0:00:45:00",
            "dstAmbiguity": "earlier",
        },
        {
            "timeZone": "Europe/London",
            "dateTime": "2026-03-29 01:30:00",
            "timeSpan": "1:00:00:00",
            "dstAmbiguity": "later",
        },
    ],
)
async def test_custom_decrement_dst_edges(api: ApiClient, body: dict[str, str]) -> None:
    response = await api.post("/api/calculation/custom/decrement", json=body)
    assert response.status_code == 200
