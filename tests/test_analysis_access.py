"""Which endpoints and tools the credentials may use: permission refusals in recordings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fauxcall.analysis.access import access_report, refusal
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

AT = datetime(2026, 10, 11, tzinfo=UTC)


def rest(
    endpoint: str,
    status: int,
    body: JsonValue = None,
    *,
    headers: list[tuple[str, str]] | None = None,
    tags: list[str] | None = None,
    path: str = "/v1/customers",
) -> Exchange:
    return Exchange(
        id="000001",
        endpoint=endpoint,
        started_at=AT,
        started_ns=0,
        request=CapturedRequest(method=HttpMethod.GET, url=f"https://api.test{path}", path=path),
        response=CapturedResponse(
            status=status,
            headers=headers or [],
            body=Body(kind="json", json_value=body) if body is not None else Body(kind="empty"),
        ),
        tags=tags or [],
    )


def tool(name: str, arguments: JsonValue, reply: JsonValue, status: int = 200) -> Exchange:
    sent: JsonValue = {"method": "tools/call", "params": {"name": name, "arguments": arguments}}
    exchange = rest(f"MCP tools/call:{name}", status, reply, path="/mcp")
    exchange.request.body = Body(kind="json", json_value=sent)
    return exchange


def failed_tool(text: str) -> JsonValue:
    return {"result": {"isError": True, "content": [{"type": "text", "text": text}]}}


def test_refusals_for_permission() -> None:
    scope = [("WWW-Authenticate", 'Bearer error="insufficient_scope"')]
    assert refusal(rest("GET /v1/customers", 403, headers=scope)) == "HTTP 403 insufficient_scope"
    assert refusal(rest("GET /v1/customers", 401)) == "HTTP 401"
    assert refusal(tool("read", {}, failed_tool("Permission denied for this key"))) == (
        "Permission denied for this key"
    )
    rpc_error: JsonValue = {"error": {"code": -32000, "message": "Access denied"}}
    assert refusal(tool("read", {}, rpc_error)) == "Access denied"
    assert refusal(rest("GET /v1/x", 400, {"message": "not authorized for x"})) == (
        "not authorized for x"
    )
    assert refusal(rest("GET /v1/x", 400, {"error": "forbidden region"})) == "forbidden region"
    long = "Permission denied: " + "x" * 200
    assert str(refusal(tool("read", {}, failed_tool(long)))).endswith("…")


def test_what_is_not_a_refusal() -> None:
    assert refusal(rest("GET /v1/x", 200, {"ok": True})) is None
    assert refusal(rest("GET /v1/x", 404, {"message": "No such customer"})) is None
    assert refusal(tool("read", {}, failed_tool("Invalid date"))) is None
    assert refusal(rest("GET /v1/x", 400, ["a list"])) is None
    assert refusal(rest("GET /v1/x", 400, {"detail": 3})) is None
    probe = rest("GET /v1/x", 401, tags=["auth:none"])  # sent without credentials on purpose
    assert refusal(probe) is None
    no_answer = rest("GET /v1/x", 200)
    no_answer.response = None
    assert refusal(no_answer) is None


def test_the_report_groups_by_endpoint_with_examples() -> None:
    denied = failed_tool("Permission denied")
    exchanges = [
        tool("read", {"op": "GetCustomers"}, denied),
        tool("read", {"op": "GetCustomers"}, denied),  # the same example once
        tool("read", {"op": "GetPrices"}, {"result": {"content": []}}),
        tool("read", {"op": "GetBalance"}, denied),
        tool("read", {"op": "GetRefunds"}, denied),
        tool("read", {"op": "GetPayouts"}, denied),  # three examples at most
        tool("account", {}, denied),  # no arguments: no example
        rest("GET /v1/customers", 401, tags=["auth:invalid"]),  # a probe: not counted
    ]
    no_answer = rest("GET /v1/x", 200)
    no_answer.response = None
    read, account = access_report([*exchanges, no_answer])
    assert (read.endpoint, read.calls, read.refused) == ("MCP tools/call:read", 6, 5)
    assert read.reasons == ["Permission denied"]
    assert read.examples == ['{"op":"GetCustomers"}', '{"op":"GetBalance"}', '{"op":"GetRefunds"}']
    assert not read.all_refused
    assert (account.calls, account.refused, account.examples) == (1, 1, [])
    assert account.all_refused
