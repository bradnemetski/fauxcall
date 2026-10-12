"""Which scenario calls only repeat a recorded request.

A fake answers a request it was recorded with by replaying the recording, so a scenario that sends
exactly that request mostly checks the recording against itself. Parity counts such calls apart
(the *independent* score leaves them out), so the score says how the fake does on requests it has
never seen. Calls are keyed the same way on both sides: method, path, query (credentials left out)
and body; MCP calls by method and params.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from urllib.parse import parse_qsl

from fauxcall import jsonfmt
from fauxcall.logs import REDACTED
from fauxcall.recording.store import RecordingStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

MCP_PREFIX: Final = "MCP "


def mcp_key(method: str, params: JsonValue) -> str:
    """The key of an MCP request: its method and params."""
    return f"{MCP_PREFIX}{method} {jsonfmt.canonical_dumps(params or {})}"


def http_key(
    method: str,
    path: str,
    query: Iterable[tuple[str, str]],
    body: str,
    *,
    credential_names: Iterable[str] = (),
) -> str:
    """The key of an HTTP request: method, path, sorted query without credentials, body."""
    bare, _, inline = path.partition("?")
    skip = {name.lower() for name in credential_names}
    pairs = sorted(
        (name, value)
        for name, value in [*parse_qsl(inline, keep_blank_values=True), *query]
        if name.lower() not in skip and REDACTED not in value
    )
    listed: JsonValue = [[name, value] for name, value in pairs]
    return f"{method.upper()} {bare} {jsonfmt.canonical_dumps(listed)} {body}"


def body_text(json_value: JsonValue = None, content: bytes | str | None = None) -> str:
    """A request body as the key sees it: canonical JSON, or the raw text."""
    if json_value is not None:
        return jsonfmt.canonical_dumps(json_value)
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    return content or ""


def exchange_key(exchange: Exchange, credential_names: Iterable[str] = ()) -> str | None:
    """The key of a recorded request (``None``: nothing a scenario could repeat)."""
    request = exchange.request
    if exchange.endpoint.startswith(MCP_PREFIX):
        sent = request.body.parsed()
        if not isinstance(sent, dict) or "method" not in sent:
            return None
        return mcp_key(str(sent["method"]), sent.get("params"))
    body = request.body
    text = (
        body_text(body.parsed())
        if body.kind == "json"
        else body.text or ""
        if body.kind in {"text", "form"}
        else ""
    )
    return http_key(
        request.method.value, request.path, request.query, text, credential_names=credential_names
    )


def recorded_keys(recordings: Path | None, credential_names: Iterable[str] = ()) -> frozenset[str]:
    """The keys of every recorded request under ``recordings``."""
    if recordings is None or not recordings.is_dir():
        return frozenset()
    names = list(credential_names)
    keys = (exchange_key(e, names) for _, e in RecordingStore(recordings).iter_exchanges())
    return frozenset(key for key in keys if key is not None)


def request_body(kwargs: Mapping[str, object]) -> str:
    """The body of an httpx2 request about to be sent, from its ``json``/``content`` arguments."""
    content = kwargs.get("content")
    return body_text(
        cast("JsonValue", kwargs.get("json")),
        content if isinstance(content, bytes | str) else None,
    )
