"""The fake MCP server end to end: the wire protocol, the official SDK client, OAuth, matching."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from _engine import j
from _mcp import RECORDED, asgi_client, live_server, mcp_app, mcp_model
from fauxcall.clock import datetime_to_ns
from fauxcall.config import AuthSettings, IdentityConfig
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import AuthModel, AuthScheme, McpTransportModel, OAuthModel
from fauxcall.model.semantic import MatchSpec, SemanticModel

if TYPE_CHECKING:
    from pathlib import Path


async def test_wire_level_session_tools_and_matching(tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path, mcp_model())
    async with asgi_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        init = await wire.initialize()
        assert init.content_type
        assert "event-stream" in init.content_type
        assert wire.session_id
        assert j(init.result)["serverInfo"] == {"name": "DeepWiki", "version": "2.14.3"}
        listed = j((await wire.call("tools/list")).result)["tools"]
        assert [t["name"] for t in listed] == ["ask_question", "read_wiki_structure", "now"]
        assert listed[0]["x-unknown-vendor-field"] == {"kept": True}  # unknown fields kept
        exact = await wire.call(
            "tools/call",
            {"name": "read_wiki_structure", "arguments": {"repoName": "facebook/react"}},
        )
        assert j(exact.result)["content"][0]["text"] == "1. Overview\n2. Fiber"
        assert j(exact.response)["id"] == j(exact.sent)["id"]
        close = await wire.call(
            "tools/call",
            {
                "name": "ask_question",
                "arguments": {"repoName": "facebook/react", "question": "what are react hooks"},
            },
        )
        assert j(close.result)["content"][0]["text"] == "Hooks let components use state."
        computed = await wire.call("tools/call", {"name": "now", "arguments": {}})
        assert j(computed.result)["content"][0]["text"] == str(datetime_to_ns(RECORDED))
        unknown = await wire.call("tools/call", {"name": "nope", "arguments": {}})
        assert unknown.error == {"code": -32602, "message": "Unknown tool"}
        invalid = await wire.call(
            "tools/call", {"name": "ask_question", "arguments": {"repoName": 5}}
        )
        assert j(invalid.result)["isError"] is True
        missing_method = await wire.call("does/not/exist")
        assert j(missing_method.error)["code"] == protocol.METHOD_NOT_FOUND
        pong = await wire.call("ping")
        assert pong.result == {}
        assert (await wire.get_stream_status())[0] == 405
        no_session = await wire.post(protocol.request(99, "tools/list"), include_session=False)
        assert no_session.status == 400
        assert await wire.delete_session() == 200


async def test_protocol_version_negotiation_and_json_mode(tmp_path: Path) -> None:
    transport = McpTransportModel(
        response_mode="json", issues_session=False, requires_session=False
    )
    app, _ = mcp_app(tmp_path, mcp_model(transport=transport))
    async with asgi_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http, protocol_version="2025-03-26")
        init = await wire.initialize(protocol_version="2025-03-26")
        assert init.content_type == "application/json"
        assert wire.negotiated_version == "2025-06-18"
        assert wire.session_id is None
        listed = await wire.call("tools/list")
        assert len(j(listed.result)["tools"]) == 3


async def test_official_sdk_client_works_against_the_fake(tmp_path: Path) -> None:
    app, _ = mcp_app(tmp_path, mcp_model())
    async with (
        asgi_client(app) as http,
        Client(streamable_http_client("http://fake.test/mcp", http_client=http)) as client,
    ):
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == [
            "ask_question",
            "read_wiki_structure",
            "now",
        ]
        result = await client.call_tool("read_wiki_structure", {"repoName": "facebook/react"})
        assert j(result.content[0]).text == "1. Overview\n2. Fiber"


async def test_legacy_sse_transport_over_real_sockets(tmp_path: Path) -> None:
    transport = McpTransportModel(
        kind="sse", path="/sse", messages_path="/messages/", response_mode="sse"
    )
    app, runtime = mcp_app(tmp_path, mcp_model(transport=transport))
    assert runtime.mcp is not None
    async with live_server(app) as base, httpx2.AsyncClient(base_url=base) as http:
        wire = McpWireClient(f"{base}/sse", http, transport="sse", timeout_s=10)
        try:
            init = await wire.initialize()
            assert wire.messages_url is not None
            assert "/messages/?session_id=" in wire.messages_url
            assert j(init.result)["serverInfo"]["name"] == "DeepWiki"
            answer = await wire.call(
                "tools/call",
                {"name": "read_wiki_structure", "arguments": {"repoName": "facebook/react"}},
            )
            assert j(answer.result)["content"][0]["text"] == "1. Overview\n2. Fiber"
            garbled = await http.post(
                wire.messages_url, content=b"{", headers={"content-type": "application/json"}
            )
            assert (garbled.status_code, garbled.text) == (400, "Could not parse message")
        finally:
            await wire.close()
        # The official SDK's legacy client agrees.
        async with Client(sse_client(f"{base}/sse")) as client:
            listed = await client.list_tools()
            assert len(listed.tools) == 3
        # Bad posts get the official SDK server's status codes and messages.
        ping = protocol.request(1, "ping")
        missing = await http.post("/messages/", json=ping)
        assert (missing.status_code, missing.text) == (400, "session_id is required")
        malformed = await http.post("/messages/?session_id=xyz", json=ping)
        assert (malformed.status_code, malformed.text) == (400, "Invalid session ID")
        unknown = await http.post(f"/messages/?session_id={'0' * 32}", json=ping)
        assert (unknown.status_code, unknown.text) == (404, "Could not find session")


def _pkce() -> tuple[str, str]:
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


async def _oauth_token(http: httpx2.AsyncClient, login_hint: str | None = None) -> httpx2.Response:
    verifier, challenge = _pkce()
    registration = await http.post(
        "/register",
        json={"redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none"},
    )
    client_id = registration.json()["client_id"]
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "http://localhost/cb",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    if login_hint:
        params["login_hint"] = login_hint
    authorize = await http.get("/authorize", params=params)
    assert authorize.status_code == 302
    query = parse_qs(urlsplit(authorize.headers["location"]).query)
    assert query["state"] == ["xyz"]
    assert query["iss"] == ["http://fake.test"]
    if "error" in query:
        return authorize
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": verifier,
            "redirect_uri": "http://localhost/cb",
        },
    )


async def test_browserless_oauth_with_allowed_and_denied_users(tmp_path: Path) -> None:
    auth = AuthModel(
        schemes=[
            AuthScheme(kind="oauth2", oauth=OAuthModel(scopes=["read"], token_ttl_seconds=60))
        ],
        missing_template="unauthorized",
        forbidden_template="forbidden",
    )
    users = AuthSettings(
        users=[
            IdentityConfig(name="alice", status="allowed"),
            IdentityConfig(name="mallory", status="denied"),
        ],
        default_user="alice",
    )
    app, runtime = mcp_app(tmp_path, mcp_model(auth=auth), auth=users)
    async with asgi_client(app) as http:
        anonymous = await http.post(
            "/mcp", json=protocol.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        )
        assert anonymous.status_code == 401
        assert "resource_metadata=" in anonymous.headers["www-authenticate"]
        metadata = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert metadata["resource"] == "http://fake.test/mcp"
        server = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert server["token_endpoint"] == "http://fake.test/token"
        token = await _oauth_token(http)
        assert token.status_code == 200
        tokens = token.json()
        bearer = {"authorization": f"Bearer {tokens['access_token']}"}
        wire = McpWireClient("http://fake.test/mcp", http, headers=bearer)
        await wire.initialize()
        assert len(j((await wire.call("tools/list")).result)["tools"]) == 3
        refreshed = await http.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 200
        denied = await _oauth_token(http, login_hint="mallory")
        assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
        bad_grant = await http.post(
            "/token",
            data={"grant_type": "authorization_code", "code": "nope", "code_verifier": "x"},
        )
        assert bad_grant.status_code == 400
        runtime.clocks.default.advance(120)  # past the 60 s token lifetime
        expired = await http.post("/mcp", json=protocol.request(2, "tools/list"), headers=bearer)
        assert expired.status_code == 401
        revoked = await http.post("/revoke", data={"token": tokens["access_token"]})
        assert revoked.status_code == 200


@pytest.mark.parametrize(
    ("repo", "answered"), [("facebook/react", True), ("vercel/next.js", False)]
)
async def test_exact_inputs_restrict_the_candidates(
    tmp_path: Path, repo: str, *, answered: bool
) -> None:
    model = mcp_model()
    assert model.mcp is not None
    model.mcp.tools[0].semantic = SemanticModel(
        match=MatchSpec(
            inputs=["args.repoName", "args.question"], exact=["args.repoName"], fallback="none"
        )
    )
    app, _ = mcp_app(tmp_path, model)
    async with asgi_client(app) as http:
        wire = McpWireClient("http://fake.test/mcp", http)
        await wire.initialize()
        reply = await wire.call(
            "tools/call",
            {"name": "ask_question", "arguments": {"repoName": repo, "question": "hooks?"}},
        )
        # Another repository has no recording: matching must not borrow facebook/react's answer.
        assert (reply.error is None) is answered
