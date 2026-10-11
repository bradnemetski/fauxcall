"""A browserless OAuth 2.1 authorization server for fakes of OAuth-protected MCP servers.

MCP clients discover it through the same metadata documents the real server publishes (rewritten to
point at the fake), register dynamically, and complete authorization-code + PKCE without a browser:
``/authorize`` approves immediately as the configured user. Denied users get ``access_denied``;
expired/denied tokens get the real 401/403 behavior through the normal auth pipeline.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from fauxcall.model.auth import OAuthModel
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonObject


@dataclass(slots=True)
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str | None
    identity: str
    scopes: tuple[str, ...]


def _base(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


def _oauth_error(error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=400)


def _pkce_ok(verifier: str | None, challenge: str | None) -> bool:
    if challenge is None:
        return True
    if not verifier:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=") == challenge


def oauth_router(  # noqa: C901, PLR0915 - one closure per OAuth endpoint
    runtime: FakeRuntime, oauth: OAuthModel, resource_path: str
) -> APIRouter:
    """Routes for metadata discovery, registration, authorization, token and revocation."""
    router = APIRouter(include_in_schema=False)
    codes: dict[str, _Code] = {}
    refresh: dict[str, tuple[str, tuple[str, ...]]] = {}
    clients: dict[str, JsonObject] = {}

    def resource_metadata(request: Request) -> JsonObject:
        base = _base(request)
        recorded = (
            oauth.protected_resource_metadata
            if isinstance(oauth.protected_resource_metadata, dict)
            else {}
        )
        return {
            **recorded,
            "resource": base + resource_path,
            "authorization_servers": [base],
            "scopes_supported": recorded.get("scopes_supported", list(oauth.scopes)),
        }

    @router.get("/.well-known/oauth-protected-resource")
    @router.get("/.well-known/oauth-protected-resource" + resource_path)
    async def protected_resource(request: Request) -> JSONResponse:
        return JSONResponse(resource_metadata(request))

    @router.get("/.well-known/oauth-authorization-server")
    @router.get("/.well-known/openid-configuration")
    async def authorization_server(request: Request) -> JSONResponse:
        base = _base(request)
        recorded = (
            oauth.authorization_server_metadata
            if isinstance(oauth.authorization_server_metadata, dict)
            else {}
        )
        return JSONResponse(
            {
                **recorded,
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "revocation_endpoint": f"{base}/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "scopes_supported": recorded.get("scopes_supported", oauth.scopes),
                "authorization_response_iss_parameter_supported": True,
            }
        )

    @router.post("/register")
    async def register(request: Request) -> JSONResponse:
        try:
            metadata = jsonfmt.plain(jsonfmt.loads(await request.body()))
        except ValueError:
            metadata = None
        if not isinstance(metadata, dict):
            return _oauth_error("invalid_client_metadata", "The body must be a JSON object.")
        uris = metadata.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not uris
            or not all(isinstance(u, str) and u for u in uris)
        ):
            return _oauth_error("invalid_redirect_uri", "redirect_uris must list one or more URIs.")
        client_id = "fauxcall-" + secrets.token_hex(8)
        record: JsonObject = {**metadata, "client_id": client_id, "client_id_issued_at": 0}
        if metadata.get("token_endpoint_auth_method", "none") != "none":
            record["client_secret"] = secrets.token_urlsafe(24)
        clients[client_id] = record
        return JSONResponse(record, status_code=201)

    @router.get("/authorize")
    async def authorize(request: Request) -> Response:
        query = request.query_params
        redirect_uri = query.get("redirect_uri", "")
        client = clients.get(query.get("client_id", ""))
        registered = client.get("redirect_uris") if client else None
        if not redirect_uri or (isinstance(registered, list) and redirect_uri not in registered):
            # Never redirect to an unverified URI: answer the user agent directly.
            return _oauth_error("invalid_request", "redirect_uri is missing or not registered.")
        state = query.get("state")
        settings = runtime.auth.settings
        users = settings.users
        wanted = query.get("login_hint") or settings.default_user
        user = next((u for u in users if u.name == wanted), None) if wanted else None
        if user is None and users and not wanted:
            user = next((u for u in users if u.status == "allowed"), None)
        params: dict[str, str] = {"iss": _base(request)}
        if state:
            params["state"] = state
        challenge = query.get("code_challenge")
        if query.get("response_type") != "code":
            params.update(error="unsupported_response_type")
            return _redirect(redirect_uri, params)
        if challenge and query.get("code_challenge_method", "plain") != "S256":
            params.update(error="invalid_request", error_description="Only S256 PKCE is supported")
            return _redirect(redirect_uri, params)
        denied = (user is not None and user.status == "denied") or (
            settings.mode == "allowlist" and user is None
        )
        if denied:
            params.update(error="access_denied", error_description="The user denied access")
            return _redirect(redirect_uri, params)
        code = secrets.token_urlsafe(24)
        scopes = tuple(query.get("scope", "").split()) or tuple(oauth.scopes)
        codes[code] = _Code(
            client_id=query.get("client_id", ""),
            redirect_uri=redirect_uri,
            challenge=challenge,
            identity=user.name if user else (wanted or "anonymous"),
            scopes=scopes,
        )
        params["code"] = code
        return _redirect(redirect_uri, params)

    def tokens(identity: str, scopes: tuple[str, ...]) -> JsonObject:
        now = runtime.clocks.default.now_ns()
        access = runtime.auth.issue_token(
            identity, now_ns=now, ttl_seconds=oauth.token_ttl_seconds, scopes=scopes
        )
        body: JsonObject = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": oauth.token_ttl_seconds,
            "scope": " ".join(scopes),
        }
        if oauth.refresh_tokens:
            token = secrets.token_urlsafe(32)
            refresh[token] = (identity, scopes)
            body["refresh_token"] = token
        return body

    @router.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        grant = str(form.get("grant_type", ""))
        if grant == "authorization_code":
            code = codes.pop(str(form.get("code", "")), None)
            verifier = form.get("code_verifier")
            if code is None or not _pkce_ok(str(verifier) if verifier else None, code.challenge):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if (
                str(form.get("redirect_uri", code.redirect_uri)) != code.redirect_uri
                or str(form.get("client_id", code.client_id)) != code.client_id
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(
                tokens(code.identity, code.scopes), headers={"cache-control": "no-store"}
            )
        if grant == "refresh_token":
            found = refresh.pop(str(form.get("refresh_token", "")), None)
            if found is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(tokens(*found), headers={"cache-control": "no-store"})
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    @router.post("/revoke")
    async def revoke(request: Request) -> Response:
        form = await request.form()
        value = str(form.get("token", ""))
        runtime.auth.revoke_token(value)
        refresh.pop(value, None)
        return Response(status_code=200)

    return router


def www_authenticate(request: Request, resource_path: str) -> str:
    """The ``WWW-Authenticate`` value pointing clients at the resource metadata."""
    metadata = f"{_base(request)}/.well-known/oauth-protected-resource{resource_path}"
    return f'Bearer resource_metadata="{metadata}"'
