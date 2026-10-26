"""What a recording looks like on disk: one :class:`Exchange` per real request/response."""

from __future__ import annotations

from typing import Final, Literal

from pydantic import AwareDatetime, Field

from fauxcall import jsonfmt
from fauxcall.jsonfmt import COMPACT, JsonStyle
from fauxcall.model.common import Model, Purpose, RawJson
from fauxcall.types import HttpMethod, JsonValue

BodyKind = Literal["empty", "json", "text", "binary", "form", "multipart"]


class MultipartPart(Model):
    """One part of a multipart request body."""

    name: str
    filename: str | None = None
    content_type: str | None = None
    text: str | None = None
    file: str | None = Field(
        default=None, description="Sidecar file holding binary part content (relative)."
    )
    size: int = 0


class Body(Model):
    """A captured body.

    JSON bodies are stored parsed (readable in diffs) together with the spelling ``style`` needed to
    reproduce the original bytes. Binary content lives in a sidecar file next to the exchange.
    """

    kind: BodyKind = "empty"
    content_type: str | None = None
    json_value: RawJson = Field(default=None, alias="json")
    style: JsonStyle = COMPACT
    text: str | None = None
    file: str | None = Field(default=None, description="Sidecar file for binary content.")
    sha256: str | None = None
    size: int = 0
    parts: list[MultipartPart] = Field(default_factory=list)

    def raw_text(self) -> str | None:
        """The body as text, re-spelled exactly as received (``None`` for binary/empty bodies)."""
        if self.kind == "json":
            return jsonfmt.dumps(self.json_value, self.style)
        return self.text

    def parsed(self) -> JsonValue:
        """The JSON value for JSON bodies, ``None`` otherwise."""
        return self.json_value if self.kind == "json" else None


class CapturedRequest(Model):
    """The request exactly as sent (credentials redacted)."""

    method: HttpMethod
    url: str
    path: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query: list[tuple[str, str]] = Field(default_factory=list)
    headers: list[tuple[str, str]] = Field(default_factory=list)
    body: Body = Field(default_factory=Body)

    def query_value(self, name: str) -> str | None:
        """First value of a query parameter."""
        return next((value for key, value in self.query if key == name), None)


class CapturedResponse(Model):
    """The response exactly as received (``Set-Cookie`` values redacted)."""

    status: int
    reason: str | None = None
    http_version: str | None = None
    headers: list[tuple[str, str]] = Field(default_factory=list)
    body: Body = Field(default_factory=Body)

    def header(self, name: str) -> str | None:
        """First value of a header (case-insensitive)."""
        lowered = name.lower()
        return next((value for key, value in self.headers if key.lower() == lowered), None)


StreamEventKind = Literal["sse", "ws_recv", "ws_send", "chunk", "ws_close"]


class StreamEvent(Model):
    """One event of a streamed response (SSE event, WebSocket message, HTTP chunk)."""

    t_ms: float = Field(description="Milliseconds since the response started.")
    kind: StreamEventKind
    event: str | None = None
    id: str | None = None
    data: str | None = None
    binary_file: str | None = None
    retry: int | None = None
    code: int | None = Field(default=None, description="WebSocket close code.")


CANCELLED: Final = "cancelled"
"""The tag of an MCP call the client cancelled: evidence of what that does, never an answer."""


class Exchange(Model):
    """One real request/response, the unit of evidence everything else is derived from."""

    id: str
    endpoint: str = Field(description="Endpoint id, e.g. 'GET /api/v3/pet/{petId}'.")
    purpose: Purpose = "sample"
    plan_call: str | None = None
    started_at: AwareDatetime
    started_ns: int = Field(description="Send time in nanoseconds since the Unix epoch.")
    elapsed_ms: float = 0.0
    ttfb_ms: float | None = None
    request: CapturedRequest
    response: CapturedResponse | None = None
    stream: list[StreamEvent] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Transport error, when no response came.")
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    redacted: list[str] = Field(default_factory=list)
    pseudonymized: list[str] = Field(default_factory=list)
