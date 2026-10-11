"""Every recording, replayed with the fake's clock frozen at the moment the real server answered.

Stricter than ``test_recorded.py`` (status + shape): status, media type and **every value** must
match — computed zone data, DST bounds, error messages, validation problems, conversions. Values
that move with the clock (timestamps, epoch numbers) may differ by the second it takes the real
server to answer; random values (``traceId``) are compared by form.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx2
import pytest
from fauxcall import jsonfmt
from fauxcall.recording.store import RecordingStore

from fauxcall_timeapi.app import build_app
from fauxcall_timeapi.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

DATA = Settings().data_dir
TOLERANCE_S: Final = 1.0
TRACE_RE: Final = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-00$")
ISO_RE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
CLOCK_PARTS: Final = frozenset(
    {"year", "month", "day", "hour", "minute", "seconds", "milliSeconds"}
)
"""Legacy fields spelling out a moving timestamp next to its ``dateTime``."""
EPOCH_SCALE: Final = {"unix": 1, "unix_ms": 1e3, "unix_us": 1e6, "unix_ns": 1e9}


def _exchanges() -> Iterator[Exchange]:
    for _, exchange in RecordingStore(DATA / "recordings").iter_exchanges():
        if exchange.response is not None and exchange.error is None:
            yield exchange


EXCHANGES = list(_exchanges())


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app(Settings(fast=True, log_level="WARNING"))


def _instant(text: str) -> float | None:
    match = ISO_RE.match(text)
    if match is None:
        return None
    base = datetime.fromisoformat(match[1]).replace(tzinfo=UTC).timestamp()
    return base + float(match[2] or 0)


def _same_time(real: str, fake: str) -> bool:
    """Two timestamps with the same spelling of the offset, at most a second apart."""
    real_match, fake_match = ISO_RE.match(real), ISO_RE.match(fake)
    if real_match is None or fake_match is None or real_match[3] != fake_match[3]:
        return False
    real_at, fake_at = _instant(real), _instant(fake)
    return real_at is not None and fake_at is not None and abs(real_at - fake_at) <= TOLERANCE_S


def _skipped(record: dict[str, JsonValue], key: str, fake: JsonValue) -> bool:
    """Fields checked through a sibling (moving clock parts) or by form (trace ids)."""
    if key in CLOCK_PARTS and "dateTime" in record:
        return True
    if key == "traceId":
        return isinstance(fake, str) and TRACE_RE.match(fake) is not None
    return key == "time" and isinstance(record[key], str) and "date" in record


def _close(real: JsonValue, fake: JsonValue, path: str, scale: float) -> bool:
    """Equal, or the same moment within the tolerance."""
    if real == fake and type(real) is type(fake):
        return True
    if isinstance(real, str) and isinstance(fake, str):
        return _same_time(real, fake)
    epoch = path.endswith("unix_timestamp") and isinstance(real, int) and isinstance(fake, int)
    return epoch and abs(int(real) - int(fake)) <= TOLERANCE_S * scale  # type: ignore[arg-type]


def _differences(real: JsonValue, fake: JsonValue, path: str, scale: float) -> Iterator[str]:
    if isinstance(real, dict) and isinstance(fake, dict):
        if list(real) != list(fake):
            yield f"{path}: keys {list(real)} vs {list(fake)}"
            return
        for key, value in real.items():
            if not _skipped(real, key, fake[key]):
                yield from _differences(value, fake[key], f"{path}.{key}", scale)
    elif isinstance(real, list) and isinstance(fake, list) and len(real) == len(fake):
        for i, (a, b) in enumerate(zip(real, fake, strict=True)):
            yield from _differences(a, b, f"{path}[{i}]", scale)
    elif not _close(real, fake, path, scale):
        yield f"{path}: real {real!r} vs fake {fake!r}"


def _request(exchange: Exchange) -> tuple[list[tuple[str, str]], bytes | None]:
    request = exchange.request
    headers = [
        (k, v)
        for k, v in request.headers
        if k.lower() not in {"host", "content-length", "accept-encoding", "connection"}
    ]
    body = request.body
    if body.kind == "json":
        return headers, jsonfmt.dumps(body.json_value).encode()
    if body.text is not None:
        return headers, body.text.encode()
    return headers, None


@pytest.mark.parametrize(
    "exchange",
    EXCHANGES,
    ids=[f"{e.id}-{e.request.method.value} {e.request.path}" for e in EXCHANGES],
)
async def test_recorded_values(app: FastAPI, exchange: Exchange) -> None:
    recorded = exchange.response
    assert recorded is not None
    answered_ns = exchange.started_ns + int((exchange.ttfb_ms or 0) * 1e6 / 2)
    headers, content = _request(exchange)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        at = datetime.fromtimestamp(answered_ns / 1e9, tz=UTC).isoformat()
        clock = await client.post("/__fauxcall/clock", json={"op": "freeze", "at": at})
        assert clock.status_code == 200
        url = exchange.request.url.split("timeapi.io", 1)[-1]
        response = await client.request(
            exchange.request.method.value, url, headers=headers, content=content
        )
    assert response.status_code == recorded.status, response.text
    real_headers = {k.lower(): v for k, v in recorded.headers}
    real_type = real_headers.get("content-type")
    assert response.headers.get("content-type") == real_type
    assert response.headers.get("allow") == real_headers.get("allow")
    real_body: JsonValue = recorded.body.parsed() if recorded.body else None
    if real_body is None and recorded.body is not None and recorded.body.text:
        real_body = recorded.body.text
    fake_body: JsonValue = None
    if response.content:
        fake_body = (
            jsonfmt.loads(response.content) if "json" in (real_type or "") else response.text
        )
    leaf = exchange.request.path.rstrip("/").rsplit("/", 1)[-1].lower()
    problems = list(_differences(real_body, fake_body, "$", EPOCH_SCALE.get(leaf, 1)))
    assert not problems, "\n".join(problems)
