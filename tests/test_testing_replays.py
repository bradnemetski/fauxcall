"""Scenario calls and recordings get the same key when the call repeats the recorded request."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx2

from fauxcall.model.recording import Body, CapturedRequest, Exchange
from fauxcall.recording.store import RecordingStore
from fauxcall.testing.client import ApiClient
from fauxcall.testing.replays import (
    body_text,
    exchange_key,
    http_key,
    mcp_key,
    recorded_keys,
    request_body,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue


def recorded(
    method: str,
    path: str,
    query: list[tuple[str, str]] | None = None,
    body: Body | None = None,
    endpoint: str | None = None,
) -> Exchange:
    return Exchange(
        id="000001",
        endpoint=endpoint or f"{method} {path}",
        started_at=datetime(2026, 10, 11, tzinfo=UTC),
        started_ns=0,
        request=CapturedRequest(
            method=HttpMethod(method),
            url=f"https://api.test{path}",
            path=path,
            query=query or [],
            body=body or Body(kind="empty"),
        ),
    )


def test_http_keys_ignore_order_credentials_and_redactions() -> None:
    plain = http_key("get", "/v1/rates?to=USD", [("from", "EUR")], "")
    assert plain == http_key("GET", "/v1/rates", [("from", "EUR"), ("to", "USD")], "")
    with_key = http_key(
        "GET",
        "/v1/rates",
        [("to", "USD"), ("from", "EUR"), ("API_KEY", "s3cret")],
        "",
        credential_names=["api_key"],
    )
    assert with_key == plain
    redacted = http_key("GET", "/v1/rates?apikey=[REDACTED]&from=EUR&to=USD", [], "")
    assert redacted == plain
    assert http_key("POST", "/v1/items", [], '{"a":1}') != http_key("POST", "/v1/items", [], "")


def test_recordings_and_calls_key_alike() -> None:
    json_body = Body(kind="json", json_value={"b": 2, "a": 1})
    assert exchange_key(recorded("POST", "/v1/items", body=json_body)) == http_key(
        "POST", "/v1/items", [], request_body({"json": {"a": 1, "b": 2}})
    )
    text = Body(kind="text", text="hello")
    assert exchange_key(recorded("POST", "/echo", body=text)) == http_key(
        "POST", "/echo", [], request_body({"content": b"hello"})
    )
    form = Body(kind="form", text="a=1")
    assert exchange_key(recorded("POST", "/form", body=form)).endswith(" a=1")  # type: ignore[union-attr]
    binary = Body(kind="binary", size=3)
    assert exchange_key(recorded("PUT", "/file", body=binary)) == http_key("PUT", "/file", [], "")
    assert body_text(content="raw") == "raw"
    assert request_body({"content": 5}) == ""


def test_mcp_keys() -> None:
    params: JsonValue = {"name": "add", "arguments": {"a": 2}}
    sent = Body(
        kind="json",
        json_value={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params},
    )
    exchange = recorded("POST", "/mcp", body=sent, endpoint="MCP tools/call:add")
    assert exchange_key(exchange) == mcp_key("tools/call", params)
    assert mcp_key("tools/list", None) == mcp_key("tools/list", {})
    odd = recorded("POST", "/mcp", body=Body(kind="json", json_value=["batch"]), endpoint="MCP x")
    assert exchange_key(odd) is None


def test_recorded_keys(tmp_path: Path) -> None:
    assert recorded_keys(None) == frozenset()
    assert recorded_keys(tmp_path / "missing") == frozenset()
    store = RecordingStore(tmp_path)
    store.save(recorded("GET", "/v1/a", [("key", "s3cret")]))
    store.save(
        recorded(
            "POST", "/mcp", body=Body(kind="json", json_value="odd"), endpoint="MCP odd"
        ).model_copy(update={"id": "000002"})
    )
    assert recorded_keys(tmp_path, ["key"]) == {http_key("GET", "/v1/a", [], "")}


async def test_api_clients_log_the_key_of_each_call() -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(answer), base_url="https://api.test"
    ) as http:
        client = ApiClient("fake", http, allow_writes=False, query={"api_key": "s3cret"})
        await client.get("/v1/a", params={"x": "1"})
        await client.post("/v1/b", json={"n": 1})
    first, second = client.log.calls
    assert first.replay_key == http_key("GET", "/v1/a", [("x", "1")], "")
    assert second.replay_key == http_key("POST", "/v1/b", [], '{"n":1}')
