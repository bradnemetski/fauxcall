"""The fake's browserless OAuth server: metadata, registration, authorize, tokens, identity."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import pytest

from _engine import j
from _mcp import asgi_client, mcp_app, mcp_model
from fauxcall.config import AuthSettings, IdentityConfig
from fauxcall.mcpwire import protocol
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.model import AuthModel, AuthScheme, OAuthModel

if TYPE_CHECKING:
    from pathlib import Path

    import httpx2
    from fastapi import FastAPI

    from fauxcall.runtime import FakeRuntime

CALLBACK = "http://localhost/cb"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)


def _app(
    tmp_path: Path, *, oauth: OAuthModel | None = None, users: AuthSettings | None = None
) -> tuple[FastAPI, FakeRuntime]:
    auth = AuthModel(
        schemes=[AuthScheme(kind="oauth2", oauth=oauth or OAuthModel(scopes=["read", "write"]))],
        missing_template="unauthorized",
        forbidden_template="forbidden",
    )
    settings = users or AuthSettings(
        users=[
            IdentityConfig(name="alice", status="allowed"),
            IdentityConfig(name="mallory", status="denied"),
        ],
        default_user="alice",
    )
    return mcp_app(tmp_path, mcp_model(auth=auth), auth=settings)


async def _register(http: httpx2.AsyncClient, **metadata: Any) -> Any:
    body = {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", **metadata}
    response = await http.post("/register", json=body)
    assert response.status_code == 201
    return j(response.json())


def _authorize_params(client_id: str, **extra: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
        **extra,
    }


async def _code(http: httpx2.AsyncClient, client_id: str, **extra: str) -> dict[str, list[str]]:
    response = await http.get("/authorize", params=_authorize_params(client_id, **extra))
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)


async def _exchange(http: httpx2.AsyncClient, code: str, **form: str) -> httpx2.Response:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": CALLBACK,
        **form,
    }
    return await http.post("/token", data=data)


async def test_metadata_documents_point_at_the_fake(tmp_path: Path) -> None:
    oauth = OAuthModel(
        scopes=["read"],
        protected_resource_metadata={"resource_name": "DeepWiki", "scopes_supported": ["wiki"]},
        authorization_server_metadata={"service_documentation": "https://docs.example"},
    )
    app, _ = _app(tmp_path, oauth=oauth)
    async with asgi_client(app) as http:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            resource = (await http.get(path)).json()
            assert resource["resource"] == "http://fake.test/mcp"
            assert resource["authorization_servers"] == ["http://fake.test"]
            assert resource["resource_name"] == "DeepWiki"  # recorded fields are kept
            assert resource["scopes_supported"] == ["wiki"]  # recorded beats configured
        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            server = (await http.get(path)).json()
            assert server["issuer"] == "http://fake.test"
            assert server["service_documentation"] == "https://docs.example"
            assert server["scopes_supported"] == ["read"]
            assert server["code_challenge_methods_supported"] == ["S256"]
        challenge = await http.post("/mcp", json=protocol.request(1, "ping"))
        assert challenge.status_code == 401
        assert challenge.headers["www-authenticate"] == (
            'Bearer resource_metadata="http://fake.test/.well-known/oauth-protected-resource/mcp"'
        )


async def test_registration(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with asgi_client(app) as http:
        public = await _register(http)
        assert public["client_id"].startswith("fauxcall-")
        assert "client_secret" not in public
        confidential = await _register(http, token_endpoint_auth_method="client_secret_post")
        assert len(confidential["client_secret"]) >= 32
        not_json = await http.post(
            "/register", content=b"{", headers={"content-type": "application/json"}
        )
        assert (not_json.status_code, not_json.json()["error"]) == (400, "invalid_client_metadata")
        bodies: list[dict[str, object]] = [
            {},
            {"redirect_uris": []},
            {"redirect_uris": [""]},
            {"redirect_uris": "x"},
        ]
        for body in bodies:
            bad = await http.post("/register", json=body)
            assert (bad.status_code, bad.json()["error"]) == (400, "invalid_redirect_uri")


async def test_authorize_validates_the_request(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with asgi_client(app) as http:
        client_id = (await _register(http))["client_id"]
        # Never redirect to an unverified URI.
        for redirect in ("", "http://evil.example/cb"):
            refused = await http.get(
                "/authorize", params=_authorize_params(client_id, redirect_uri=redirect)
            )
            assert (refused.status_code, refused.json()["error"]) == (400, "invalid_request")
        wrong_type = await _code(http, client_id, response_type="token")
        assert wrong_type["error"] == ["unsupported_response_type"]
        plain = await _code(http, client_id, code_challenge_method="plain")
        assert plain["error"] == ["invalid_request"]
        # Unregistered (pre-configured) clients are accepted, and existing query strings kept.
        loose = await http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "static",
                "redirect_uri": f"{CALLBACK}?a=1",
            },
        )
        location = urlsplit(loose.headers["location"])
        query = parse_qs(location.query)
        assert query["a"] == ["1"]
        assert "code" in query
        assert "state" not in query


async def test_token_grants(tmp_path: Path) -> None:
    app, runtime = _app(tmp_path)
    async with asgi_client(app) as http:
        client_id = (await _register(http))["client_id"]
        granted = await _exchange(http, (await _code(http, client_id))["code"][0])
        assert granted.status_code == 200
        assert granted.headers["cache-control"] == "no-store"
        tokens = granted.json()
        assert (tokens["token_type"], tokens["expires_in"], tokens["scope"]) == (
            "Bearer",
            3600,
            "read write",
        )
        replay = await _exchange(
            http, (await _code(http, client_id))["code"][0], code_verifier="wrong"
        )
        assert replay.json() == {"error": "invalid_grant"}
        for form in ({"redirect_uri": "http://other/cb"}, {"client_id": "someone-else"}):
            code = (await _code(http, client_id))["code"][0]
            assert (await _exchange(http, code, **form)).json() == {"error": "invalid_grant"}
        code = (await _code(http, client_id))["code"][0]
        assert (await _exchange(http, code)).status_code == 200
        assert (await _exchange(http, code)).json() == {"error": "invalid_grant"}  # single use
        no_verifier = (await _code(http, client_id))["code"][0]
        missing = await http.post(
            "/token", data={"grant_type": "authorization_code", "code": no_verifier}
        )
        assert missing.json() == {"error": "invalid_grant"}
        scoped = await _code(http, client_id, scope="read")
        assert (await _exchange(http, scoped["code"][0])).json()["scope"] == "read"
        refreshed = await http.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 200
        reused = await http.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        )
        assert reused.json() == {"error": "invalid_grant"}  # refresh tokens rotate
        unsupported = await http.post("/token", data={"grant_type": "password"})
        assert unsupported.json() == {"error": "unsupported_grant_type"}
        bearer = {"authorization": f"Bearer {refreshed.json()['access_token']}"}
        wire = McpWireClient("http://fake.test/mcp", http, headers=bearer)
        await wire.initialize()
        assert (
            await http.post("/revoke", data={"token": refreshed.json()["access_token"]})
        ).status_code == 200
        revoked = await http.post("/mcp", json=protocol.request(5, "ping"), headers=bearer)
        assert revoked.status_code == 401
    assert runtime.journal.query(path_prefix="/mcp")[0].identity == "alice"


async def test_pkce_is_optional_and_refresh_tokens_can_be_off(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, oauth=OAuthModel(refresh_tokens=False, token_ttl_seconds=60))
    async with asgi_client(app) as http:
        client_id = (await _register(http))["client_id"]
        location = await http.get(
            "/authorize",
            params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK},
        )
        code = parse_qs(urlsplit(location.headers["location"]).query)["code"][0]
        response = await http.post(
            "/token", data={"grant_type": "authorization_code", "code": code}
        )
        assert response.status_code == 200
        assert "refresh_token" not in response.json()
        assert response.json()["expires_in"] == 60


@pytest.mark.parametrize(
    ("settings", "hint", "outcome"),
    [
        (AuthSettings(users=[IdentityConfig(name="bob")]), None, "bob"),  # first allowed user
        (AuthSettings(), "carol", "carol"),  # anyone may sign in by default
        (AuthSettings(), None, "anonymous"),
        (AuthSettings(mode="allowlist", users=[IdentityConfig(name="bob")]), "carol", "denied"),
        (
            AuthSettings(users=[IdentityConfig(name="eve", status="denied")], default_user="eve"),
            None,
            "denied",
        ),
    ],
)
async def test_who_gets_approved(
    tmp_path: Path, settings: AuthSettings, hint: str | None, outcome: str
) -> None:
    app, runtime = _app(tmp_path, users=settings)
    async with asgi_client(app) as http:
        client_id = (await _register(http))["client_id"]
        query = await _code(http, client_id, **({"login_hint": hint} if hint else {}))
        if outcome == "denied":
            assert query["error"] == ["access_denied"]
            return
        token = (await _exchange(http, query["code"][0])).json()["access_token"]
        wire = McpWireClient(
            "http://fake.test/mcp", http, headers={"authorization": f"Bearer {token}"}
        )
        await wire.initialize()
    assert runtime.journal.query(path_prefix="/mcp")[-1].identity == outcome
