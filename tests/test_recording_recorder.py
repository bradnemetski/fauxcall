from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from httpx2.websockets import ASGIWebSocketTransport, WebSocketDisconnect
from starlette.datastructures import UploadFile
from starlette.websockets import WebSocketDisconnect as StarletteDisconnect

from fauxcall import jsonfmt
from fauxcall.mcpwire import protocol
from fauxcall.model import Approval, Approvals, Exchange, PlannedCall, RecordingPlan
from fauxcall.recording import NoPrivacy, RealTarget, Recorder, RecordingStore, classify_body
from fauxcall.recording.recorder import (
    INVALID_CREDENTIAL,
    _keep_scheme,
    _ws_url,
    credential_values,
    host_of,
    scrub_exchange,
)
from fauxcall.storage import read_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from pathlib import Path

    from fauxcall.types import JsonValue

BASE = "http://api.test"
TOKEN = "sk-live-0123456789"
QUERY_KEY = "qk-9876543210"
EPOCH_NS = 1_791_633_600_000_000_000  # 2026-10-10T12:00:00Z
PETS = b'[{"id":1,"name":"Rex","price":105.50}]'
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(16))


def j(value: object) -> Any:
    """View decoded JSON loosely."""
    return value


class FakeClock:
    """Monotonic time that only moves when the recorder sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def clock_ns(self) -> int:
        return EPOCH_NS + int((self.now - 1000.0) * 1e9)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def _mark(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return f"m:{value}"
    if isinstance(value, list):
        return [_mark(item) for item in value]
    if isinstance(value, dict):
        return {key: _mark(item) for key, item in value.items()}
    return value


class Marking:
    """A privacy filter that marks everything it touched, and logs the order of its hooks."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(
        self,
        *,
        url: str,
        path: str,
        path_params: Mapping[str, str],
        query: Sequence[tuple[str, str]],
        headers: Sequence[tuple[str, str]],
        json_body: JsonValue,
    ) -> tuple[
        str, str, dict[str, str], list[tuple[str, str]], list[tuple[str, str]], JsonValue, list[str]
    ]:
        self.calls.append("request")
        return (
            url + "#m",
            path + "#m",
            {name: f"m:{value}" for name, value in path_params.items()},
            [(name, f"m:{value}") for name, value in query],
            [*headers, ("x-marked", "request")],
            _mark(json_body),
            ["request:note"],
        )

    def response_headers(
        self, headers: Sequence[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        self.calls.append("response_headers")
        return [*headers, ("x-marked", "response")], ["x-marked"]

    def json(self, value: JsonValue) -> tuple[JsonValue, list[str], list[str]]:
        self.calls.append("json")
        return _mark(value), ["$.r"], ["$.p"]

    def text(self, value: str) -> str:
        self.calls.append("text")
        return f"m:{value}"


# -- the in-process API ----------------------------------------------------------------------------


class Api:
    """A small in-process API that logs what it received (:attr:`seen`)."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self.seen: list[dict[str, Any]] = []
        self.polls: dict[str, int] = {}
        self._read_routes()
        self._write_routes()
        self._job_routes()
        self._websocket_routes()

    async def log(self, request: Request) -> dict[str, Any]:
        entry = {
            "method": request.method,
            "path": request.url.path,
            "raw_path": request.scope["raw_path"],
            "query": list(request.query_params.multi_items()),
            "headers": [(k.decode(), v.decode()) for k, v in request.scope["headers"]],
            "body": await request.body(),
        }
        self.seen.append(entry)
        return entry

    def _read_routes(self) -> None:
        app, log = self.app, self.log

        @app.get("/pets")
        async def pets(request: Request) -> Response:
            await log(request)
            return Response(PETS, media_type="application/json")

        @app.get("/pets/{pet_id}")
        async def pet(request: Request, pet_id: str) -> Response:
            await log(request)
            if pet_id == "42":
                return JSONResponse({"id": 42, "name": "Rex"})
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/pretty")
        async def pretty(request: Request) -> Response:
            await log(request)
            return Response(b'{ "token": "abc", "n": 1 }', media_type="application/json")

        @app.get("/text")
        async def text(request: Request) -> Response:
            await log(request)
            return PlainTextResponse("hello")

        @app.get("/image")
        async def image(request: Request) -> Response:
            await log(request)
            return Response(PNG, media_type="image/png")

        @app.get("/empty")
        async def empty(request: Request) -> Response:
            await log(request)
            return Response(status_code=204)

        @app.get("/redirect")
        async def redirect(request: Request) -> Response:
            await log(request)
            return Response(status_code=302, headers={"location": "/pets"})

        @app.get("/echo")
        async def echo(request: Request) -> Response:
            entry = await log(request)
            headers = dict(entry["headers"])
            return JSONResponse(
                {"authorization": headers.get("authorization"), "query": dict(entry["query"])},
                headers={"x-echo-auth": headers.get("authorization", "")},
            )

    def _write_routes(self) -> None:
        app, log = self.app, self.log

        @app.post("/pets")
        async def create(request: Request) -> Response:
            entry = await log(request)
            body = j(jsonfmt.loads(entry["body"]))
            created = {"id": 42, **body, "flag": True, "price": jsonfmt.loads("1.50")}
            return Response(jsonfmt.dumps(created), status_code=201, media_type="application/json")

        @app.delete("/pets/{pet_id}")
        async def delete(request: Request, pet_id: str) -> Response:
            del pet_id
            await log(request)
            return Response(status_code=204)

        @app.post("/upload")
        async def upload(request: Request) -> Response:
            await log(request)
            fields: dict[str, Any] = {}
            for name, value in (await request.form()).multi_items():
                if isinstance(value, UploadFile):
                    content = await value.read()
                    fields[name] = [value.filename, value.content_type, len(content)]
                else:
                    fields[name] = value
            return JSONResponse(fields)

        @app.post("/form")
        async def form(request: Request) -> Response:
            await log(request)
            return JSONResponse(dict((await request.form()).multi_items()))

        @app.post("/text")
        async def text_body(request: Request) -> Response:
            entry = await log(request)
            return PlainTextResponse(entry["body"].decode())

    def _job_routes(self) -> None:
        app, log, polls = self.app, self.log, self.polls

        @app.post("/jobs")
        async def submit(request: Request) -> Response:
            await log(request)
            return JSONResponse({"id": "job-1", "state": "queued"}, status_code=202)

        @app.get("/jobs/{job_id}")
        async def status(request: Request, job_id: str) -> Response:
            await log(request)
            polls[job_id] = polls.get(job_id, 0) + 1
            states = ["queued", "running", "done"] if job_id == "job-1" else ["running"]
            state = states[min(polls[job_id], len(states)) - 1]
            return JSONResponse({"id": job_id, "state": state})

    def _websocket_routes(self) -> None:
        app = self.app

        @app.websocket("/ws")
        async def chat(websocket: WebSocket) -> None:
            await websocket.accept()
            message = await websocket.receive_text()
            await websocket.send_text(f"echo:{message}")
            await websocket.send_text('{"user":"jane","token":"abc"}')
            await websocket.send_bytes(b"\x00\x01binary")
            await websocket.close(code=4001, reason="bye")

        @app.websocket("/ws/multi")
        async def multi(websocket: WebSocket) -> None:
            await websocket.accept()
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await websocket.send_text(f"bytes:{len(message['bytes'])}")
                elif message.get("text") == "bye":
                    await websocket.close(code=1000)
                    return
                else:
                    await websocket.send_text(f"text:{message.get('text')}")

        @app.websocket("/ws/quiet")
        async def quiet(websocket: WebSocket) -> None:
            await websocket.accept()
            with contextlib.suppress(StarletteDisconnect):
                await websocket.receive_text()  # says nothing; ends when the client hangs up

        @app.websocket("/ws/refuse")
        async def refuse(websocket: WebSocket) -> None:
            await websocket.close(code=1008)


def api() -> tuple[FastAPI, list[dict[str, Any]]]:
    server = Api()
    return server.app, server.seen


def plan(*calls: dict[str, Any]) -> RecordingPlan:
    return RecordingPlan.model_validate(
        {"name": "test", "created_at": "2026-10-10T12:00:00Z", "calls": list(calls)}
    )


def get(call_id: str, path: str, **extra: Any) -> dict[str, Any]:
    endpoint = extra.pop("endpoint", f"GET {path}")
    request = {"method": "GET", "path": path, **extra.pop("request", {})}
    return {"id": call_id, "endpoint": endpoint, "request": request, **extra}


def approval(endpoint: str, max_calls: int | None = None) -> Approval:
    return Approval(
        scope="endpoint",
        endpoint=endpoint,
        max_calls=max_calls,
        approved_at=datetime(2026, 10, 10, tzinfo=UTC),
    )


def recorder(
    tmp_path: Path,
    http: httpx2.AsyncClient,
    clock: FakeClock,
    *,
    approvals: Approvals | None = None,
    privacy: Any = None,
    target: RealTarget | None = None,
    save_approvals: bool = True,
    **options: Any,
) -> Recorder:
    return Recorder(
        RecordingStore(tmp_path / "recordings"),
        target or RealTarget(base_url=BASE, user_agent="fauxcall-test/1.0"),
        http,
        approvals or Approvals(),
        approvals_path=tmp_path / "approvals.json" if save_approvals else None,
        privacy=privacy,
        files_root=tmp_path,
        sleep=clock.sleep,
        clock_ns=clock.clock_ns,
        monotonic=clock.monotonic,
        **options,
    )


def asgi(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))


def stored(tmp_path: Path) -> dict[str, Exchange]:
    store = RecordingStore(tmp_path / "recordings")
    return {exchange.id: exchange for _, exchange in store.iter_exchanges()}


def disk_bytes(tmp_path: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file())


# -- bodies ----------------------------------------------------------------------------------------


async def test_read_calls_and_body_kinds(tmp_path: Path) -> None:
    app, seen = api()
    clock = FakeClock()
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, clock)
        report = await rec.run(
            plan(
                get("list", "/pets"),
                get("pretty", "/pretty"),
                get("text", "/text"),
                get("image", "/image"),
                get("empty", "/empty"),
                get("redirect", "/redirect"),
            )
        )
    assert report.calls_made == 6
    assert [o.statuses for o in report.outcomes] == [[200], [200], [200], [200], [204], [302]]
    assert len(seen) == 6
    found = stored(tmp_path)
    assert list(found) == ["000001", "000002", "000003", "000004", "000005", "000006"]
    pets = found["000001"]
    assert pets.response is not None
    body = pets.response.body
    assert body.kind == "json"
    assert body.text is None  # the style reproduces the bytes exactly …
    assert jsonfmt.dumps(body.json_value, body.style).encode() == PETS  # … including 105.50
    assert body.raw_text() == PETS.decode()
    assert pets.started_ns == EPOCH_NS
    assert pets.started_at == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert pets.ttfb_ms is not None
    assert pets.ttfb_ms <= pets.elapsed_ms
    assert ("user-agent", "fauxcall-test/1.0") in pets.request.headers
    assert pets.response.reason == "OK"
    assert pets.response.http_version == "HTTP/1.1"
    pretty = found["000002"].response
    assert pretty is not None
    assert pretty.body.text == '{ "token": "abc", "n": 1 }'  # not reproducible: kept verbatim
    text = found["000003"].response
    assert text is not None
    assert (text.body.kind, text.body.text) == ("text", "hello")
    image = found["000004"].response
    assert image is not None
    assert image.body.kind == "binary"
    assert image.body.sha256 == hashlib.sha256(PNG).hexdigest()
    assert image.body.file == "000004.response.bin"
    assert (tmp_path / "recordings" / "get-image" / "000004.response.bin").read_bytes() == PNG
    empty = found["000005"].response
    assert empty is not None
    assert empty.body.kind == "empty"
    redirect = found["000006"].response
    assert redirect is not None
    assert redirect.header("location") == "/pets"  # redirects are recorded, never followed
    assert "| list | GET /pets | 200 | 000001..000001 |  |" in report.summary()


@pytest.mark.parametrize(
    ("content_type", "raw", "kind"),
    [
        ("application/json", b"", "empty"),
        (None, b' {"a":1}', "json"),
        (None, b"[1,2]", "json"),
        ("application/problem+json", b'{"detail":"x"}', "json"),
        ("application/json", b"{not json", "text"),
        ("application/x-ndjson", b'{"a":1}\n{"a":2}\n', "text"),
        ("application/json", b'{"a":"\xff"}', "binary"),
        ("text/csv; charset=utf-8", b"a,b\n1,2\n", "text"),
        ("application/xml", b"<a/>", "text"),
        ("text/plain", b"\xff\xfe", "binary"),
        ("application/octet-stream", b"{}", "binary"),
        (None, b"plain words", "binary"),
    ],
)
def test_classify_body(content_type: str | None, raw: bytes, kind: str) -> None:
    body, sidecar = classify_body(content_type, raw)
    assert body.kind == kind
    assert body.content_type == content_type
    assert (sidecar is not None) is (kind == "binary")
    if kind != "empty":
        assert body.size == len(raw)


# -- requests: templating, captures, building ------------------------------------------------------


async def test_captures_flow_between_calls_in_json_spelling(tmp_path: Path) -> None:
    app, seen = api()
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock(), approvals=Approvals(approvals=[approval("*")]))
        report = await rec.run(
            plan(
                {
                    "id": "create",
                    "endpoint": "POST /pets",
                    "request": {"method": "POST", "path": "/pets", "json_body": {"name": "Rex"}},
                    "capture": {
                        "pet_id": "$.id",
                        "flag": "$.flag",
                        "price": "$.price",
                        "no": "$.x",
                    },
                },
                get(
                    "read",
                    "/pets/{pet_id}",
                    request={
                        "path_params": {"pet_id": "{{pet_id}}"},
                        "query": [["flag", "{{flag}}"], ["price", "{{price}}"], ["no", "{{no}}"]],
                        "headers": {"X-Pet": "pet-{{pet_id}}", "X-Flag": "{{flag}}"},
                    },
                ),
            )
        )
    assert [o.statuses for o in report.outcomes] == [[201], [200]]
    read = seen[1]
    assert read["path"] == "/pets/42"
    # placeholders render in JSON spelling: true (not True), 1.50 (not 1.5); unknown ones stay
    assert read["query"] == [("flag", "true"), ("price", "1.50"), ("no", "{{no}}")]
    headers = dict(read["headers"])
    assert (headers["x-pet"], headers["x-flag"]) == ("pet-42", "true")
    created = stored(tmp_path)["000001"]
    assert created.request.body.kind == "json"
    assert created.request.body.json_value == {"name": "Rex"}
    assert dict(created.request.headers)["content-type"] == "application/json"
    assert stored(tmp_path)["000002"].request.path_params == {"pet_id": "42"}


async def test_url_building(tmp_path: Path) -> None:
    app, seen = api()
    target = RealTarget(base_url=BASE + "/", user_agent="ua/1")
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock(), target=target)
        await rec.run(
            plan(
                get(
                    "odd",
                    "/pets/{pet_id}",
                    request={
                        "path_params": {"pet_id": "a b?#"},
                        "headers": {"User-Agent": "planned/2"},
                    },
                ),
                get(
                    "relative",
                    "pets?limit=2&tag=a+b",
                    endpoint="GET /pets",
                    request={"query": [["page", "3"]]},
                ),
            )
        )
    assert seen[0]["raw_path"] == b"/pets/a%20b%3F%23"  # spaces, ? and # can't break the URL
    assert stored(tmp_path)["000001"].request.path_params == {"pet_id": "a b?#"}
    assert dict(seen[0]["headers"])["user-agent"] == "planned/2"  # the plan's header wins
    assert [k for k, _ in seen[0]["headers"]].count("user-agent") == 1
    assert seen[1]["path"] == "/pets"
    assert seen[1]["query"] == [("limit", "2"), ("tag", "a b"), ("page", "3")]
    recorded = stored(tmp_path)["000002"].request
    assert recorded.path == "/pets"
    assert recorded.query == [("limit", "2"), ("tag", "a b"), ("page", "3")]
    assert recorded.url == f"{BASE}/pets?limit=2&tag=a+b&page=3"


# -- approvals, budgets, pacing --------------------------------------------------------------------


def _create(call_id: str = "create", **extra: Any) -> dict[str, Any]:
    request = {"method": "POST", "path": "/pets", "json_body": {"name": "Rex"}}
    return {"id": call_id, "endpoint": "POST /pets", "request": request, **extra}


async def test_mutating_calls_need_an_approval(tmp_path: Path) -> None:
    app, seen = api()
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(
            plan(_create(), get("read", "/pets"))
        )
    assert report.outcomes[0].skipped == (
        "needs approval (mutating call not covered by approvals.json)"
    )
    assert report.outcomes[0].exchanges == []
    assert [entry["method"] for entry in seen] == ["GET"]  # the POST never reached the API
    assert report.outcomes[1].statuses == [200]
    assert read_model(tmp_path / "approvals.json", Approvals) == Approvals()


async def test_endpoint_approval_budget_is_spent_and_saved(tmp_path: Path) -> None:
    app, seen = api()
    approvals = Approvals(approvals=[approval("POST /pets", max_calls=2)])
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, FakeClock(), approvals=approvals).run(
            plan(_create(repeat=3), _create("again"))
        )
    first, second = report.outcomes
    assert len(first.exchanges) == 2
    assert first.skipped == "approval budget used up after 2 call(s)"
    assert second.skipped == "needs approval (mutating call not covered by approvals.json)"
    assert len(seen) == 2
    saved = read_model(tmp_path / "approvals.json", Approvals)
    assert saved.used == {"endpoint:POST /pets:": 2}
    async with asgi(app) as http:  # a later run sees the spent budget
        later = await recorder(tmp_path, http, FakeClock(), approvals=saved).run(plan(_create()))
    assert later.outcomes[0].skipped is not None
    assert len(seen) == 2


async def test_exact_call_approval_and_explicit_mutating_flag(tmp_path: Path) -> None:
    app, seen = api()
    create = PlannedCall.model_validate(_create())
    exact = Approval(
        scope="call",
        endpoint="POST /pets",
        fingerprint=create.fingerprint(),
        approved_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    other = _create("other")
    other["request"] = {**other["request"], "json_body": {"name": "Other"}}
    async with asgi(app) as http:
        report = await recorder(
            tmp_path, http, FakeClock(), approvals=Approvals(approvals=[exact])
        ).run(
            plan(
                _create(),
                other,
                get("safe-but-flagged", "/pets", mutating=True),
                _create("declared-safe", mutating=False),
            )
        )
    assert report.outcomes[0].statuses == [201]
    assert report.outcomes[1].skipped is not None  # a different body: not the approved call
    assert report.outcomes[2].skipped is not None  # mutating: true on a GET needs approval
    assert report.outcomes[3].statuses == [201]  # mutating: false runs freely
    assert [entry["method"] for entry in seen] == ["POST", "POST"]


async def test_repeat_interval_and_pacing(tmp_path: Path) -> None:
    app, seen = api()
    clock = FakeClock()
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, clock, rps=2.0).run(
            plan(get("series", "/pets", repeat=3, interval_ms=200), get("once", "/text"))
        )
    assert len(seen) == 4
    # between repeats: the 0.2 s interval, then 0.3 s more to keep 0.5 s between calls (2 rps)
    assert clock.sleeps == [0.2, 0.3, 0.2, 0.3, 0.5]
    assert report.calls_made == 4
    times = [e.started_ns for e in stored(tmp_path).values()]
    assert [round((t - times[0]) / 1e9, 3) for t in times] == [0.0, 0.5, 1.0, 1.5]


async def test_long_intervals_need_no_extra_pacing(tmp_path: Path) -> None:
    app, _ = api()
    clock = FakeClock()
    async with asgi(app) as http:
        await recorder(tmp_path, http, clock, rps=0).run(
            plan(get("series", "/pets", repeat=2, interval_ms=1500))
        )
    assert clock.sleeps == [1.5]


async def test_recording_budget_exhaustion(tmp_path: Path) -> None:
    app, seen = api()
    approvals = Approvals(approvals=[approval("POST /pets", max_calls=5)])
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, FakeClock(), approvals=approvals, max_calls=2).run(
            plan(get("a", "/pets"), _create(repeat=3), get("b", "/text"), get("c", "/empty"))
        )
    assert report.budget_exhausted
    assert report.calls_made == 2
    assert [o.skipped for o in report.outcomes] == [
        None,
        "budget exhausted",
        "budget exhausted",
        "budget exhausted",
    ]
    assert len(report.outcomes[1].exchanges) == 1
    assert len(seen) == 2
    # the approval budget only counts calls that were made
    assert read_model(tmp_path / "approvals.json", Approvals).used == {"endpoint:POST /pets:": 1}
    assert "**budget exhausted**" in report.summary()


async def test_approvals_are_saved_even_when_a_run_crashes(tmp_path: Path) -> None:
    approvals = Approvals(approvals=[approval("POST /pets")])

    def explode(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("unexpected")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(explode)) as http:
        rec = recorder(tmp_path, http, FakeClock(), approvals=approvals)
        with pytest.raises(RuntimeError, match="unexpected"):
            await rec.run(plan(_create()))
    assert read_model(tmp_path / "approvals.json", Approvals).used == {"endpoint:POST /pets:": 1}


async def test_transport_errors_are_reported_per_call(tmp_path: Path) -> None:
    def flaky(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/down":
            raise httpx2.ConnectError("connection refused", request=request)
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(flaky)) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(
            plan(get("down", "/down"), get("up", "/up"))
        )
    assert report.outcomes[0].error == "ConnectError: connection refused"
    assert report.outcomes[1].statuses == [200]
    assert "connection refused" in report.summary()


# -- auth ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "authorization", "api_key"),
    [
        ("default", f"Bearer {TOKEN}", QUERY_KEY),
        ("none", None, None),
        ("invalid", f"Bearer {INVALID_CREDENTIAL}", INVALID_CREDENTIAL),
    ],
)
async def test_auth_modes(
    tmp_path: Path, mode: str, authorization: str | None, api_key: str | None
) -> None:
    app, seen = api()
    target = RealTarget(
        base_url=BASE,
        headers={"Authorization": f"Bearer {TOKEN}", "X-Api-Key": QUERY_KEY},
        query={"api_key": QUERY_KEY},
    )
    async with asgi(app) as http:
        await recorder(tmp_path, http, FakeClock(), target=target).run(
            plan(
                get(
                    "echo",
                    "/echo",
                    request={"auth": mode, "headers": {"authorization": "Bearer planned"}},
                )
            )
        )
    headers = seen[0]["headers"]
    sent = [value for name, value in headers if name == "authorization"]
    if authorization is None:
        assert sent == ["Bearer planned"]  # auth "none": only what the plan sends
    else:
        assert sent == [authorization]  # the configured credential replaces the planned one
    assert dict(seen[0]["query"]).get("api_key") == api_key
    if mode == "invalid":
        assert dict(headers)["x-api-key"] == INVALID_CREDENTIAL  # no scheme to keep
    tags = stored(tmp_path)["000001"].tags
    assert tags == ([] if mode == "default" else [f"auth:{mode}"])  # probes stay recognizable
    disk = disk_bytes(tmp_path)
    assert TOKEN.encode() not in disk  # never on disk, even with a pass-through privacy filter
    assert QUERY_KEY.encode() not in disk
    if mode == "default":
        echoed = stored(tmp_path)["000001"]
        assert echoed.response is not None
        assert echoed.response.body.json_value == {
            "authorization": "Bearer [REDACTED]",
            "query": {"api_key": "[REDACTED]"},
        }
        assert echoed.response.header("x-echo-auth") == "Bearer [REDACTED]"
        assert echoed.request.url.endswith("api_key=[REDACTED]")


@pytest.mark.parametrize(
    ("original", "header", "expected"),
    [
        ("Bearer abc", "Authorization", "Bearer NEW"),
        ("basic dXNlcjpwYXNz", "X-Anything", "basic NEW"),
        ("Bot abc", "Authorization", "Bot NEW"),
        ("Bot abc", "X-Custom", "Bot NEW"),
        ("Foo abc", "X-Custom", "NEW"),
        ("Foo abc", "Authorization", "Foo NEW"),
        ("9Foo abc", "Authorization", "NEW"),
        ("GenieKey abc", "Proxy-Authorization", "GenieKey NEW"),
        ("abc", "Authorization", "NEW"),
        ("Bearer ", "Authorization", "NEW"),
        ("Bearer abc", "Authorization", "Bearer abc"),
    ],
)
def test_keep_scheme(original: str, header: str, expected: str) -> None:
    replacement = original if expected == original else "NEW"
    assert _keep_scheme(original, replacement, header=header) == expected


def test_credential_values_and_scrubbing() -> None:
    values = credential_values(
        {
            "Authorization": "Bearer abc123",
            "X-Token": "Token def456",
            "X-Key": "abc 123 xyz",
            "B": "short",
            "C": "x y",
            "D": "Bearer ab",
        },
        {"k": "qk-1234", "dup": "abc123"},
    )
    assert values == ["abc123", "def456", "abc 123 xyz", "short", "Bearer ab", "qk-1234"]
    assert credential_values({}) == []
    exchange = Exchange.model_validate(
        {
            "id": "000001",
            "endpoint": "GET /x",
            "started_at": "2026-10-10T12:00:00Z",
            "started_ns": EPOCH_NS,
            "request": {"method": "GET", "url": "https://x/?k=abc123", "path": "/x"},
            "response": {
                "status": 200,
                "body": {"kind": "json", "json": jsonfmt.loads('{"abc123": 1.50}')},
            },
        }
    )
    cleaned = scrub_exchange(exchange, values)
    assert cleaned.request.url == "https://x/?k=[REDACTED]"
    assert cleaned.response is not None
    assert jsonfmt.dumps(cleaned.response.body.json_value) == '{"[REDACTED]":1.50}'
    assert scrub_exchange(exchange, []) is exchange
    assert scrub_exchange(exchange, ["nothing-like-it"]) is exchange
    assert host_of("https://api.example.com:8443/v1") == "api.example.com:8443"
    assert _ws_url("https://a.test/x") == "wss://a.test/x"
    assert _ws_url("http://a.test/x") == "ws://a.test/x"
    assert _ws_url("ws://a.test/x") == "ws://a.test/x"


# -- request bodies --------------------------------------------------------------------------------


async def test_multipart_uploads(tmp_path: Path) -> None:
    app, seen = api()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "photo.png").write_bytes(PNG)
    upload = {
        "id": "upload",
        "endpoint": "POST /upload",
        "mutating": False,
        "request": {
            "method": "POST",
            "path": "/upload",
            "form": {"title": "{{title}}", "password": "hunter2"},
            "files": [
                {
                    "field": "photo",
                    "filename": "photo.png",
                    "content_type": "image/png",
                    "path": "files/photo.png",
                },
                {
                    "field": "notes",
                    "filename": "notes.txt",
                    "content_type": "text/plain",
                    "text": "hello",
                },
                {
                    "field": "blob",
                    "filename": "blob.bin",
                    "base64": base64.b64encode(b"\x00\x01").decode(),
                },
            ],
        },
    }
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock())
        report = await rec.run(plan(upload))
    assert report.outcomes[0].statuses == [200]
    assert seen[0]["body"].count(b"Content-Disposition") == 5
    exchange = stored(tmp_path)["000001"]
    assert exchange.response is not None
    assert exchange.response.body.json_value == {
        "title": "{{title}}",
        "password": "hunter2",
        "photo": ["photo.png", "image/png", len(PNG)],
        "notes": ["notes.txt", "text/plain", 5],
        "blob": ["blob.bin", "application/octet-stream", 2],
    }
    body = exchange.request.body
    assert body.kind == "multipart"
    assert body.content_type is not None
    assert body.content_type.startswith("multipart/form-data; boundary=")
    assert body.size == len(seen[0]["body"])
    parts = {part.name: part for part in body.parts}
    assert parts["title"].text == "{{title}}"
    assert parts["notes"].text == "hello"
    assert parts["photo"].file == "000001.part0.bin"
    assert parts["photo"].size == len(PNG)
    assert parts["blob"].file == "000001.part2.bin"
    directory = tmp_path / "recordings" / "post-upload"
    assert (directory / "000001.part0.bin").read_bytes() == PNG
    assert (directory / "000001.part2.bin").read_bytes() == b"\x00\x01"


@pytest.mark.parametrize(
    ("planned", "files_root", "error"),
    [
        ({"path": "../outside.bin"}, True, "is outside"),
        ({"path": "missing.bin"}, True, "FileNotFoundError"),
        ({"path": "x.bin"}, False, "need files_root"),
        ({"base64": "not base64!"}, True, "invalid base64"),
    ],
)
async def test_planned_file_problems(
    tmp_path: Path, planned: dict[str, str], files_root: bool, error: str
) -> None:
    (tmp_path.parent / "outside.bin").write_bytes(b"secret")
    app, seen = api()
    files = [{"field": "f", "filename": "f.bin", **planned}]
    call = {
        "id": "up",
        "endpoint": "POST /upload",
        "mutating": False,
        "request": {"method": "POST", "path": "/upload", "files": files},
    }
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock())
        if not files_root:
            rec.files_root = None
        report = await rec.run(plan(call))
    assert report.outcomes[0].error is not None
    assert error in report.outcomes[0].error
    assert seen == []


async def test_form_and_text_bodies(tmp_path: Path) -> None:
    app, seen = api()
    calls = [
        {
            "id": "form",
            "endpoint": "POST /form",
            "mutating": False,
            "request": {"method": "POST", "path": "/form", "form": {"q": "a b", "n": "{{n}}"}},
        },
        {
            "id": "text",
            "endpoint": "POST /text",
            "mutating": False,
            "request": {
                "method": "POST",
                "path": "/text",
                "text_body": "<q>{{n}}</q>",
                "content_type": "application/xml",
            },
        },
        {
            "id": "patch",
            "endpoint": "POST /text",
            "mutating": False,
            "request": {
                "method": "POST",
                "path": "/text",
                "json_body": {"op": "x"},
                "content_type": "application/merge-patch+json",
                "headers": {"Content-Type": "application/json-patch+json"},
            },
        },
    ]
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock())
        await rec.run(plan(*calls))
    assert seen[0]["body"] == b"q=a+b&n=%7B%7Bn%7D%7D"
    assert seen[1]["body"] == b"<q>{{n}}</q>"
    assert dict(seen[1]["headers"])["content-type"] == "application/xml"
    assert dict(seen[2]["headers"])["content-type"] == "application/json-patch+json"
    found = stored(tmp_path)
    form = found["000001"].request.body
    assert (form.kind, form.text) == ("form", "q=a+b&n=%7B%7Bn%7D%7D")
    assert form.content_type == "application/x-www-form-urlencoded"
    text = found["000002"].request.body
    assert (text.kind, text.text, text.content_type) == ("text", "<q>{{n}}</q>", "application/xml")
    patch = found["000003"].request.body
    assert (patch.kind, patch.content_type) == ("json", "application/json-patch+json")


# -- polling ---------------------------------------------------------------------------------------


async def test_polling_until_terminal(tmp_path: Path) -> None:
    app, seen = api()
    clock = FakeClock()
    submit = {
        "id": "job",
        "endpoint": "POST /jobs",
        "mutating": False,
        "tags": ["jobs"],
        "request": {"method": "POST", "path": "/jobs"},
        "capture": {"job": "$.id"},
        "poll": {
            "endpoint": "GET /jobs/{id}",
            "request": {"method": "GET", "path": "/jobs/{{job}}"},
            "state_path": "$.state",
            "terminal": ["done", "failed"],
            "interval_ms": 1000,
            "timeout_s": 60,
        },
    }
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, clock).run(plan(submit))
    outcome = report.outcomes[0]
    assert outcome.statuses == [202, 200, 200, 200]
    assert outcome.error is None
    assert [entry["path"] for entry in seen] == [
        "/jobs",
        "/jobs/job-1",
        "/jobs/job-1",
        "/jobs/job-1",
    ]
    polls = [e for e in stored(tmp_path).values() if e.purpose == "job_poll"]
    assert [p.endpoint for p in polls] == ["GET /jobs/{id}"] * 3
    assert all(p.tags == ["jobs", "poll:job"] and p.plan_call == "job" for p in polls)
    assert clock.sleeps == [0.5, 1.0, 1.0]  # pacing before the first poll, then the interval


async def test_polling_times_out(tmp_path: Path) -> None:
    app, _ = api()
    clock = FakeClock()
    stuck = get(
        "watch",
        "/jobs/stuck",
        poll={
            "endpoint": "GET /jobs/{id}",
            "request": {"method": "GET", "path": "/jobs/stuck"},
            "state_path": "$.state",
            "terminal": ["done"],
            "interval_ms": 2000,
            "timeout_s": 5,
        },
    )
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, clock).run(plan(stuck))
    outcome = report.outcomes[0]
    assert outcome.error == "poll timed out after 5.0s (last state 'running')"
    assert len(outcome.exchanges) == 5  # the call itself + 4 polls (0 s, 2 s, 4 s, 6 s)


async def test_polled_states_compare_in_json_spelling(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"ready": True, "progress": 100})

    watch = get(
        "watch",
        "/status",
        poll={
            "endpoint": "GET /status",
            "request": {"method": "GET", "path": "/status"},
            "state_path": "$.ready",
            "terminal": ["true"],
        },
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(plan(watch))
    assert report.outcomes[0].error is None
    assert len(report.outcomes[0].exchanges) == 2


# -- streams ---------------------------------------------------------------------------------------


class Feed(httpx2.AsyncByteStream):
    """A streamed body: the given chunks, then (optionally) silence until the client hangs up."""

    def __init__(self, chunks: Sequence[bytes], *, stay_open: bool = False) -> None:
        self.chunks = list(chunks)
        self.stay_open = stay_open
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)
        if self.stay_open:
            await asyncio.sleep(3600)

    async def aclose(self) -> None:
        self.closed = True


def streaming(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def stream_call(kind: str, **spec: Any) -> dict[str, Any]:
    return get("live", "/live", stream={"kind": kind, **spec})


SSE_CHUNKS = [
    b"retry: 3000\n\n: comment\n",
    b'id: 1\nevent: tick\ndata: {"n":1,"price":105.50}\r',
    b"\n\r\nid: 2\ndata: line one\ndata: line two\n\n",
    b'data: {"email":"jane@example.org","token":"abc"}\n\n',
    b"data: cut off",
]


async def test_sse_capture(tmp_path: Path) -> None:
    received: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        received.append(request)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Feed(SSE_CHUNKS)
        )

    call = stream_call("sse", duration_s=5)
    call["request"].update(method="POST", json_body={"stream": True})
    call["mutating"] = False
    async with streaming(handler) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(plan(call))
    assert report.outcomes[0].statuses == [200]
    assert received[0].method == "POST"
    assert jsonfmt.loads(received[0].content) == {"stream": True}
    exchange = stored(tmp_path)["000001"]
    assert exchange.request.method.value == "POST"
    assert exchange.request.body.json_value == {"stream": True}
    events = exchange.stream
    assert [(e.kind, e.event, e.id, e.retry) for e in events] == [
        ("sse", "tick", "1", 3000),
        ("sse", "message", "2", None),
        ("sse", "message", None, None),
    ]
    assert events[0].data == '{"n":1,"price":105.50}'
    assert events[1].data == "line one\nline two"
    assert events[2].data == '{"email":"jane@example.org","token":"abc"}'  # NoPrivacy
    assert all(e.t_ms >= 0 for e in events)
    assert [e.t_ms for e in events] == sorted(e.t_ms for e in events)
    assert exchange.response is not None
    assert exchange.response.header("content-type") == "text/event-stream"


async def test_stream_duration_and_event_limits(tmp_path: Path) -> None:
    feeds: list[Feed] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        chunks = [b"data: a\n\ndata: b\n\ndata: c\n\n"] if request.url.path == "/many" else []
        feeds.append(Feed(chunks, stay_open=True))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=feeds[-1])

    calls = [
        get("many", "/many", stream={"kind": "sse", "max_events": 2, "duration_s": 5}),
        get("quiet", "/quiet", stream={"kind": "sse", "duration_s": 0.05}),
    ]
    async with streaming(handler) as http:
        report = await asyncio.wait_for(
            recorder(tmp_path, http, FakeClock()).run(plan(*calls)), timeout=5
        )
    many, quiet = stored(tmp_path).values()
    assert [e.data for e in many.stream] == ["a", "b"]  # cut at max_events, mid-chunk
    assert quiet.stream == []
    assert quiet.elapsed_ms >= 50
    assert all(feed.closed for feed in feeds)
    assert [o.statuses for o in report.outcomes] == [[200], [200]]


async def test_chunked_capture_and_stream_errors(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/denied":
            return httpx2.Response(401, json={"error": "invalid_token"})
        return httpx2.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=Feed([b'{"n":1}\n', b"", b'{"n":2}\n']),
        )

    calls = [
        {**stream_call("chunked", max_events=10), "capture": {"never": "$.n"}},
        get("denied", "/denied", stream={"kind": "sse"}, capture={"error": "$.error"}),
    ]
    async with streaming(handler) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(plan(*calls))
    chunked, denied = stored(tmp_path).values()
    assert [(e.kind, e.data) for e in chunked.stream] == [
        ("chunk", '{"n":1}\n'),
        ("chunk", '{"n":2}\n'),
    ]
    assert denied.stream == []
    assert denied.response is not None
    assert denied.response.status == 401
    assert denied.response.body.json_value == {"error": "invalid_token"}  # kept as a body
    assert [o.statuses for o in report.outcomes] == [[200], [401]]


async def test_websocket_capture(tmp_path: Path) -> None:
    app, _ = api()
    call = get(
        "ws", "/ws", stream={"kind": "websocket", "send": [{"hello": "world"}], "duration_s": 5}
    )
    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(app)) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(plan(call))
    assert report.outcomes[0].statuses == [101]
    exchange = stored(tmp_path)["000001"]
    assert exchange.request.url == "ws://api.test/ws"
    assert ("sec-websocket-version", "13") in exchange.request.headers
    kinds = [(e.kind, e.data, e.binary_file, e.code) for e in exchange.stream]
    assert kinds == [
        ("ws_send", '{"hello":"world"}', None, None),
        ("ws_recv", 'echo:{"hello":"world"}', None, None),
        ("ws_recv", '{"user":"jane","token":"abc"}', None, None),
        ("ws_recv", None, "000001.ws3.bin", None),
        ("ws_close", "bye", None, 4001),
    ]
    assert (tmp_path / "recordings" / "get-ws" / "000001.ws3.bin").read_bytes() == b"\x00\x01binary"
    assert exchange.response is not None
    assert exchange.response.status == 101


async def test_websocket_frames_go_out_on_schedule_while_receiving(tmp_path: Path) -> None:
    app, _ = api()
    frames = [
        {"text": "hi"},
        {"at_ms": 40, "base64": base64.b64encode(b"\x00\x01").decode()},
        {"at_ms": 80, "json": {"a": 1}},
        {"at_ms": 120, "text": "bye"},
        {"at_ms": 1200, "text": "too late"},  # the server has closed by then
    ]
    call = get(
        "multi", "/ws/multi", stream={"kind": "websocket", "frames": frames, "duration_s": 2}
    )
    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(app)) as http:
        report = await asyncio.wait_for(
            recorder(tmp_path, http, FakeClock()).run(plan(call)), timeout=10
        )
    assert report.outcomes[0].statuses == [101]
    exchange = stored(tmp_path)["000001"]
    assert [(e.kind, e.data, e.binary_file) for e in exchange.stream] == [
        ("ws_send", "hi", None),
        ("ws_recv", "text:hi", None),
        ("ws_send", None, "000001.ws2-sent.bin"),
        ("ws_recv", "bytes:2", None),
        ("ws_send", '{"a":1}', None),
        ("ws_recv", 'text:{"a":1}', None),
        ("ws_send", "bye", None),
        ("ws_close", None, None),
    ]
    sent_at = [e.t_ms for e in exchange.stream if e.kind == "ws_send"]
    assert sent_at[1:] >= [40, 80, 120]  # never early
    assert sent_at == sorted(sent_at)
    sidecar = tmp_path / "recordings" / "get-ws-multi" / "000001.ws2-sent.bin"
    assert sidecar.read_bytes() == b"\x00\x01"
    assert exchange.note == "1 of 5 frames not sent (connection ended)"


async def test_websocket_frames_all_sent_leave_no_note(tmp_path: Path) -> None:
    app, _ = api()
    frames = [{"at_ms": 10, "text": "a"}, {"at_ms": 30, "text": "b"}]
    call = get(
        "multi", "/ws/multi", stream={"kind": "websocket", "frames": frames, "duration_s": 0.3}
    )
    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(app)) as http:
        await asyncio.wait_for(recorder(tmp_path, http, FakeClock()).run(plan(call)), timeout=10)
    exchange = stored(tmp_path)["000001"]
    assert [(e.kind, e.data) for e in exchange.stream] == [
        ("ws_send", "a"),
        ("ws_recv", "text:a"),
        ("ws_send", "b"),
        ("ws_recv", "text:b"),
    ]
    assert exchange.note is None


async def test_websocket_silence_limits_and_refusals(tmp_path: Path) -> None:
    app, _ = api()
    calls = [
        get("quiet", "/ws/quiet", stream={"kind": "websocket", "duration_s": 0.05}),
        get("few", "/ws", stream={"kind": "websocket", "send": ["hi"], "max_events": 2}),
        get("refused", "/ws/refuse", stream={"kind": "websocket"}),
    ]
    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(app)) as http:
        report = await asyncio.wait_for(
            recorder(tmp_path, http, FakeClock()).run(plan(*calls)), timeout=10
        )
    quiet, few, refused = stored(tmp_path).values()
    assert quiet.stream == []
    assert [e.kind for e in few.stream] == ["ws_send", "ws_recv"]
    assert refused.note is not None
    assert refused.note.startswith("websocket error: WebSocketDisconnect")
    assert [o.error for o in report.outcomes] == [None, None, None]


async def test_websocket_upgrade_refused_with_a_status(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.scheme == "wss"
        return httpx2.Response(403, json={"error": "forbidden"})

    target = RealTarget(base_url="https://api.test")
    async with streaming(handler) as http:
        report = await recorder(tmp_path, http, FakeClock(), target=target).run(
            plan(get("ws", "/ws", stream={"kind": "websocket"}))
        )
    assert report.outcomes[0].statuses == [403]
    exchange = stored(tmp_path)["000001"]
    assert exchange.request.url == "wss://api.test/ws"
    assert exchange.response is not None
    assert exchange.response.body.json_value == {"error": "forbidden"}


async def test_websocket_network_errors_are_noted(tmp_path: Path) -> None:
    class Broken:
        response = None

        async def send_text(self, text: str) -> None:
            del text
            from httpx2.websockets import WebSocketNetworkError  # noqa: PLC0415

            raise WebSocketNetworkError

    class Opener:
        def websocket(self, url: str, **kwargs: Any) -> Any:
            del url, kwargs
            return _Ctx(Broken())

    class _Ctx:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def __aenter__(self) -> Any:
            return self.session

        async def __aexit__(self, *args: object) -> None:
            return None

    rec = recorder(tmp_path, Opener(), FakeClock())  # type: ignore[arg-type]
    report = await rec.run(plan(get("ws", "/ws", stream={"kind": "websocket", "send": ["x"]})))
    exchange = stored(tmp_path)["000001"]
    assert exchange.note == "websocket error: WebSocketNetworkError: "
    assert report.outcomes[0].statuses == [101]
    assert WebSocketDisconnect(1000).code == 1000


# -- privacy hooks ---------------------------------------------------------------------------------


async def test_privacy_hooks_run_in_order_and_secrets_never_reach_disk(tmp_path: Path) -> None:
    app, _ = api()
    privacy = Marking()
    target = RealTarget(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"})
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock(), privacy=privacy, target=target)
        await rec.run(
            plan(
                get("echo", "/echo", request={"path_params": {"x": "1"}, "query": [["q", "v"]]}),
                {
                    "id": "text",
                    "endpoint": "POST /text",
                    "mutating": False,
                    "request": {"method": "POST", "path": "/text", "text_body": "plain"},
                },
                {
                    "id": "form",
                    "endpoint": "POST /upload",
                    "mutating": False,
                    "request": {
                        "method": "POST",
                        "path": "/upload",
                        "form": {"note": "n"},
                        "files": [{"field": "f", "filename": "f.txt", "text": "file text"}],
                    },
                },
            )
        )
    # the response body is made safe before the request (personal values become known first)
    assert privacy.calls[:3] == ["json", "request", "response_headers"]
    echo, text, upload = stored(tmp_path).values()
    assert echo.response is not None
    assert echo.response.body.json_value == {
        "authorization": "m:Bearer [REDACTED]",
        "query": {"q": "m:v"},
    }
    assert echo.request.url.endswith("#m")
    assert echo.request.path == "/echo#m"
    assert echo.request.path_params == {"x": "m:1"}
    assert echo.request.query == [("q", "m:v")]
    assert ("x-marked", "request") in echo.request.headers
    assert ("x-marked", "response") in echo.response.headers
    assert echo.redacted == ["request:note", "response:$.r", "response.header:x-marked"]
    assert echo.pseudonymized == ["response:$.p"]
    assert text.request.body.text == "m:plain"
    assert text.response is not None
    assert text.response.body.text == "m:plain"
    parts = {part.name: part.text for part in upload.request.body.parts}
    assert parts == {"note": "m:n", "f": "m:file text"}
    assert "request.form:$.r" in upload.redacted
    assert TOKEN.encode() not in disk_bytes(tmp_path)


async def test_stream_payloads_get_json_privacy(tmp_path: Path) -> None:
    privacy = Marking()

    def handler(request: httpx2.Request) -> httpx2.Response:
        chunks = [
            b'data: {"user":"jane"}\n\n',
            b'data: [1, "x"]\n\n',
            b"data: plain\n\n",
            b"data: {broken\n\n",
        ]
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Feed(chunks)
        )

    async with streaming(handler) as http:
        await recorder(tmp_path, http, FakeClock(), privacy=privacy).run(plan(stream_call("sse")))
    exchange = stored(tmp_path)["000001"]
    assert [e.data for e in exchange.stream] == [
        '{"user":"m:jane"}',
        '[1, "m:x"]',  # re-spelled in the payload's own style
        "m:plain",
        "m:{broken",
    ]
    assert exchange.redacted[-3:] == ["stream[0]:$.r", "stream[1]:$.r", "response.header:x-marked"]


async def test_unchanged_json_keeps_its_spelling(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Feed([b'data: {"n": 1.50}\n\n']),
        )

    async with streaming(handler) as http:
        await recorder(tmp_path, http, FakeClock(), privacy=NoPrivacy()).run(
            plan(stream_call("sse"))
        )
    assert stored(tmp_path)["000001"].stream[0].data == '{"n": 1.50}'


async def test_non_reproducible_json_text_is_dropped_once_values_change(tmp_path: Path) -> None:
    app, _ = api()
    async with asgi(app) as http:
        await recorder(tmp_path, http, FakeClock(), privacy=Marking()).run(
            plan(get("pretty", "/pretty"))
        )
    response = stored(tmp_path)["000001"].response
    assert response is not None
    assert response.body.json_value == {"token": "m:abc", "n": 1}
    assert response.body.text is None  # the raw text would still hold the original values


def test_protocol_module_is_shared() -> None:
    # The recorder parses SSE with the same decoder the MCP client uses.
    assert protocol.SseDecoder().feed("data: x\n\n")[0].data == "x"


async def test_without_an_approvals_file_nothing_is_written(tmp_path: Path) -> None:
    app, seen = api()
    approvals = Approvals(approvals=[approval("POST /pets")])
    async with asgi(app) as http:
        rec = recorder(tmp_path, http, FakeClock(), approvals=approvals, save_approvals=False)
        await rec.run(plan(_create()))
    assert len(seen) == 1
    assert approvals.used == {"endpoint:POST /pets:": 1}
    assert not (tmp_path / "approvals.json").exists()


async def test_files_only_upload(tmp_path: Path) -> None:
    app, seen = api()
    call = {
        "id": "up",
        "endpoint": "POST /upload",
        "mutating": False,
        "request": {
            "method": "POST",
            "path": "/upload",
            "files": [{"field": "f", "filename": "f.txt", "text": "only a file"}],
        },
    }
    async with asgi(app) as http:
        await recorder(tmp_path, http, FakeClock()).run(plan(call))
    assert len(seen) == 1
    body = stored(tmp_path)["000001"].request.body
    assert [(part.name, part.text, part.size) for part in body.parts] == [("f", "only a file", 11)]


async def test_websocket_window_can_close_before_the_first_receive(tmp_path: Path) -> None:
    app, _ = api()
    call = get("ws", "/ws/quiet", stream={"kind": "websocket", "send": ["x"], "duration_s": 1e-6})
    async with httpx2.AsyncClient(transport=ASGIWebSocketTransport(app)) as http:
        await recorder(tmp_path, http, FakeClock()).run(plan(call))
    assert [e.kind for e in stored(tmp_path)["000001"].stream] == ["ws_send"]


async def test_auth_probe_tags_keep_the_plan_tags(tmp_path: Path) -> None:
    app, _ = api()
    probes = [
        get("bad", "/echo", tags=["auth", "auth:invalid"], request={"auth": "invalid"}),
        get("anon", "/echo", tags=["auth"], request={"auth": "none"}, purpose="auth"),
    ]
    async with asgi(app) as http:
        await recorder(tmp_path, http, FakeClock()).run(plan(*probes))
    bad, anon = stored(tmp_path).values()
    assert bad.tags == ["auth", "auth:invalid"]  # not added twice
    assert (anon.tags, anon.purpose) == (["auth", "auth:none"], "auth")


async def test_mutating_poll_requests_are_refused(tmp_path: Path) -> None:
    app, seen = api()
    watch = get(
        "watch",
        "/pets",
        poll={
            "endpoint": "POST /jobs",
            "request": {"method": "POST", "path": "/jobs"},
            "state_path": "$.state",
            "terminal": ["done"],
        },
    )
    async with asgi(app) as http:
        report = await recorder(tmp_path, http, FakeClock()).run(plan(watch))
    assert report.outcomes[0].error == "poll requests must be read-only; POST is not polled"
    assert [entry["method"] for entry in seen] == ["GET"]  # the call itself, no POST polls
