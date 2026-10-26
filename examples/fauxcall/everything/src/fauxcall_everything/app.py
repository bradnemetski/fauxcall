"""The fake's ASGI app."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fauxcall.runtime import create_app, register_method, register_tool
from fauxcall.runtime.app import runtime_of_app
from starlette.middleware import Middleware

from fauxcall_everything.handlers import METHOD_HANDLERS, TOOL_HANDLERS
from fauxcall_everything.routes import router
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.types import JsonValue
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REFUSALS = {
    "GET": "get_unknown_session",
    "HEAD": "get_unknown_session",
    "DELETE": "delete_unknown_session",
}
_ONLY_ONE_INITIALIZE = (
    b'{"jsonrpc":"2.0","error":{"code":-32600,'
    b'"message":"Invalid Request: Only one initialization request is allowed"},"id":null}'
)


def _members_ok(value: object, objects: tuple[str, ...], flags: tuple[str, ...] = ()) -> bool:
    """An object whose ``objects`` members, when present, are objects and ``flags`` booleans."""
    if not isinstance(value, dict):
        return False
    return all(isinstance(value[key], dict) for key in objects if key in value) and all(
        isinstance(value[key], bool) for key in flags if key in value
    )


def _objects_of(value: object) -> bool:
    """A record of objects (``experimental``, ``extensions``)."""
    return isinstance(value, dict) and all(isinstance(item, dict) for item in value.values())


def _elicitation_ok(value: object) -> bool:
    form = value.get("form", {}) if isinstance(value, dict) else None
    return _members_ok(value, ("form", "url")) and _members_ok(form, (), ("applyDefaults",))


def _capability_ok(key: str, value: object) -> bool:
    """One declared capability against ``ClientCapabilitiesSchema`` (unknown ones pass)."""
    match key:
        case "experimental" | "extensions":
            return _objects_of(value)
        case "sampling":
            return _members_ok(value, ("context", "tools"))
        case "elicitation":
            return _elicitation_ok(value)
        case "roots":
            return _members_ok(value, (), ("listChanged",))
        case "tasks":
            return _members_ok(value, ("list", "cancel", "requests"))
        case _:
            return True


def _capabilities_ok(capabilities: object) -> bool:
    """``ClientCapabilitiesSchema``: each declared capability has the shape the SDK expects."""
    return isinstance(capabilities, dict) and all(
        _capability_ok(key, value) for key, value in capabilities.items()
    )


def _is_initialize(message: object) -> bool:
    """The SDK's ``isInitializeRequest``: an initialize that matches its schema."""
    if not isinstance(message, dict) or message.get("method") != "initialize":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    client = params.get("clientInfo")
    return (
        message.get("jsonrpc") == "2.0"
        and isinstance(message.get("id"), str | int)
        and not isinstance(message.get("id"), bool)
        and isinstance(params.get("protocolVersion"), str)
        and _capabilities_ok(params.get("capabilities"))
        and isinstance(client, dict)
        and isinstance(client.get("name"), str)
        and isinstance(client.get("version"), str)
    )


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


class SessionRequired:
    """What the real server answers before any session exists, where FauxCall answers otherwise.

    - GET, HEAD and DELETE on the MCP path naming no session (or an empty one): the real 400.
      The express handlers refuse them before the transport looks at anything else (``Accept``
      included); recording 000024 shows the GET, the server's source the others.
    - A POST naming no session goes to a fresh transport (webStandardStreamableHttp.js): once
      ``Accept``, ``Content-Type`` and the JSON-RPC body pass, anything but one initialize that
      matches the SDK's schema is refused ("Server not initialized", or "Only one
      initialization request is allowed" in a batch), and the initialize's
      ``MCP-Protocol-Version`` header is not checked.
    FauxCall refuses only a session id it doesn't know; everything else passes on to it.
    """

    def __init__(  # noqa: PLR0913 - the transport's settings, as the model has them
        self,
        app: ASGIApp,
        *,
        path: str,
        header: str,
        refusals: dict[str, JsonValue],
        not_initialized: bytes,
        max_body: int | None,
    ) -> None:
        self.app = app
        self.path = path.rstrip("/").lower()
        self.header = header.lower().encode("latin-1")
        self.refusals = refusals
        self.not_initialized = not_initialized
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer session-less requests to the MCP path as the real server does."""
        on_path = str(scope.get("path", "")).rstrip("/").lower() == self.path
        if scope["type"] != "http" or not on_path or _header(scope, self.header):
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", ""))
        unnamed = {
            **scope,
            "headers": [(k, v) for k, v in scope["headers"] if k.lower() != self.header],
        }
        refusal = self.refusals.get(_REFUSALS.get(method, ""))
        if isinstance(refusal, dict):
            body = str(refusal.get("body") or "").encode()
            status = refusal.get("status")
            await _answer(
                send,
                status if isinstance(status, int) else 400,
                str(refusal.get("content_type") or "application/json"),
                body,
                head=method == "HEAD",
            )
            return
        if method != "POST":
            await self.app(unnamed, receive, send)
            return
        body = await _read_body(receive)
        verdict = self._verdict(unnamed, body)
        if isinstance(verdict, bytes):
            await _answer(send, 400, "application/json", verdict)
            return
        await self.app(verdict, _replaying(body, receive), send)

    def _verdict(self, scope: Scope, body: bytes) -> bytes | Scope:
        """The refusal body, or the scope to pass on (an initialize without its version header)."""
        accept = _header(scope, b"accept") or ""
        media = (_header(scope, b"content-type") or "").split(";")[0].strip().lower()
        if "application/json" not in accept or "text/event-stream" not in accept:
            return scope
        if media != "application/json" or (self.max_body is not None and len(body) > self.max_body):
            return scope
        try:
            parsed = json.loads(body)
        except ValueError:
            return scope
        messages = parsed if isinstance(parsed, list) else [parsed]
        if not messages or not all(
            isinstance(m, dict) and m.get("jsonrpc") == "2.0" for m in messages
        ):
            return scope
        if not any(_is_initialize(message) for message in messages):
            return self.not_initialized
        if len(messages) > 1:
            return _ONLY_ONE_INITIALIZE
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"mcp-protocol-version"]
        return {**scope, "headers": headers}


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if message["type"] != "http.request" or not message.get("more_body", False):
            return b"".join(chunks)


def _replaying(body: bytes, receive: Receive) -> Receive:
    """A ``receive`` that hands the buffered body over once, then the client's own events."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _answer(
    send: Send, status: int, content_type: str, body: bytes, *, head: bool = False
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("latin-1")),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (used by ``__main__`` and the tests)."""
    app = create_app(settings or Settings(), routers=[router])
    for name, handler in TOOL_HANDLERS.items():
        register_tool(app, name, handler)
    for method, method_handler in METHOD_HANDLERS.items():
        register_method(app, method, method_handler)
    mcp = runtime_of_app(app).model.mcp
    if mcp is not None:
        transport = mcp.transport
        missing = mcp.errors.get("missing_session")
        app.user_middleware.append(  # innermost: after the routing quirks, inside CORS
            Middleware(
                SessionRequired,
                path=transport.path,
                header=transport.session_header,
                refusals=dict(transport.refusals),
                not_initialized=json.dumps(missing, separators=(",", ":")).encode(),
                max_body=transport.max_body_bytes,
            )
        )
    return app
