"""MCP servers: transport behavior and the exact tools/resources/prompts they advertise."""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, SerializerFunctionWrapHandler, model_serializer

from fauxcall.model.common import Model, RawJson
from fauxcall.model.realism import LatencyProfile
from fauxcall.model.semantic import SemanticModel

FALLBACK_PROTOCOL_VERSION: Final = "2025-06-18"
"""What a fake answers an unsupported protocol version when nothing better was recorded."""

PROBE_TOOL: Final = "__fauxcall_probe_unknown_tool__"
PROBE_RESOURCE: Final = "fauxcall://probe/unknown-resource"
PROBE_PROMPT: Final = "__fauxcall_probe_unknown_prompt__"
"""Names introspection asks for so the real "unknown …" errors get recorded; a fake puts the
caller's name where the real error repeats one of these."""


_UUID: Final = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
IdFormat = Literal["hex", "uuid", "other"]
"""How ids look: 32 hex digits, a dashed UUID, or something else (served as hex)."""


def id_format(value: str | None) -> IdFormat:
    """The format of an id a server issued."""
    text = value or ""
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text
    ):
        return "uuid"
    return "hex" if re.fullmatch(r"[0-9a-fA-F]+", text) else "other"


class McpTransportModel(Model):
    """How the real server speaks MCP over HTTP — reproduced exactly so clients can't tell."""

    kind: Literal["streamable_http", "sse"] = "streamable_http"
    path: str = Field(default="/mcp", description="Streamable endpoint, or the SSE stream path.")
    messages_path: str | None = Field(default=None, description="SSE transport: POST path.")
    response_mode: Literal["json", "sse"] = Field(
        default="sse", description="How POSTed requests are answered."
    )
    session_header: str = "Mcp-Session-Id"
    issues_session: bool = True
    requires_session: bool = True
    missing_session_status: int = 400
    get_stream: Literal["sse", "status"] = Field(
        default="status",
        description="GET on the endpoint: 'sse' opens a server-to-client stream; 'status' answers "
        "with get_status (most servers: 405).",
    )
    get_status: int = Field(default=405, ge=400, le=599, description="Status of a refused GET.")
    delete_status: int = 200
    protocol_versions: dict[str, str] = Field(
        default_factory=dict, description="Protocol version requested -> version answered."
    )
    default_protocol_version: str | None = Field(
        default=None,
        description=(
            "Version answered when the requested one is not in protocol_versions (what the real "
            "server answers an unsupported version); unset: the recorded initialize result's, "
            "else " + FALLBACK_PROTOCOL_VERSION + "."
        ),
    )
    sse_event_ids: bool = False
    member_order: list[str] = Field(
        default_factory=list,
        description="A response's members in the order the server writes them (result, id …).",
    )
    session_id_format: IdFormat = "hex"
    event_id_format: IdFormat = "hex"
    priming_versions: list[str] = Field(
        default_factory=list,
        description="Protocol versions whose SSE answers open with an empty priming event.",
    )
    refusals: dict[str, RawJson] = Field(
        default_factory=dict,
        description=(
            "How the server refuses a request at the HTTP level, by situation (POSTs: accept, "
            "content_type, protocol_header, unknown_session; get_unknown_session, "
            "get_second_stream, delete_unknown_session): {status, content_type, body}."
        ),
    )
    sse_field_order: list[str] = Field(
        default_factory=lambda: ["id", "event", "data"],
        description="The order of an SSE event's fields as the server writes them.",
    )
    sse_event_name: str | None = Field(
        default="message", description="The event name of its SSE messages (null: none)."
    )
    sse_streamed: bool = Field(
        default=False,
        description="SSE answers are sent without a Content-Length (chunked), even when complete.",
    )
    priming_since: str | None = Field(
        default=None,
        description=(
            "SSE answers open with a priming event when the protocol version the client asked "
            "for (the handshake's, else the request's MCP-Protocol-Version header) sorts at or "
            "after this one; null: priming_versions decides by the negotiated version."
        ),
    )
    message_member_order: list[str] = Field(
        default_factory=list,
        description="Member order of messages the server sends on its own (method, params …).",
    )
    refusal_order: list[str] = Field(
        default_factory=list,
        description=(
            "The order in which the server checks a POST (the refusals' situations): the first "
            "that applies answers. Empty: accept, content_type, protocol_header, "
            "unknown_session, invalid_json, not_jsonrpc."
        ),
    )
    cors: RawJson = Field(
        default=None,
        description=(
            "CORS as the server answers it: {preflight: {status, headers}, headers, "
            "headers_without_origin}; header values may hold {{origin}} and "
            "{{request_headers}} (null: no CORS)."
        ),
    )
    resumption: Literal["stream", "session"] = Field(
        default="stream",
        description=(
            "A GET with Last-Event-ID: 'stream' replays the rest of that event's stream and "
            "stays the client's stream; 'session' replays every later event of the session "
            "(priming events included), is never refused as a second stream and gets nothing "
            "more (the TypeScript SDK's in-memory event store)."
        ),
    )
    timeout_cancel_reason: str | None = Field(
        default=None,
        description=(
            "When a request the server sent the client times out, it tells the client with "
            "notifications/cancelled carrying this reason (the TypeScript SDK: 'McpError: MCP "
            "error -32001: Request timed out'); null: it sends nothing."
        ),
    )
    keep_alive: str = Field(
        default=": keep-alive",
        description="The comment line the server writes to keep an idle SSE stream open.",
    )
    keep_alive_seconds: float = Field(
        default=15.0, gt=0, description="How long an SSE stream stays idle before a keep-alive."
    )
    reinitialize_status: int | None = Field(
        default=None, description="HTTP status of a refused second initialize in a session."
    )
    content_type_json: str = "application/json"
    content_type_sse: str = "text/event-stream"


def _alternatives(value: object) -> object:
    """``["sampling", "roots"]`` (any one of them) -> ``[["sampling"], ["roots"]]``."""
    if isinstance(value, list):
        return [[item] if isinstance(item, str) else item for item in value]
    return value


def has_capability(declared: object, path: str) -> bool:
    """Whether client capabilities hold ``path`` (``"elicitation.url"``)."""
    node = declared
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


class McpToolModel(Model):
    """One tool: its exact definition and how the fake answers calls."""

    definition: RawJson = Field(description="The tool object exactly as tools/list returned it.")
    behavior: Literal["replay", "match", "computed", "error"] = "match"
    computed: str | None = Field(default=None, description="Custom handler name for 'computed'.")
    invalid_args: Literal["jsonrpc_error", "tool_error", "accept"] = "tool_error"
    invalid_template: RawJson = Field(
        default=None, description="Recorded error (JSON-RPC error or isError result) for bad args."
    )
    latency: LatencyProfile | None = None
    semantic: SemanticModel | None = None
    requires: Annotated[list[list[str]], BeforeValidator(_alternatives)] = Field(
        default_factory=list,
        description=(
            "Client capabilities the server lists this tool only to: alternatives, each a set of "
            "capability paths that must all be declared (``[['sampling'], ['tasks.requests', "
            "'sampling']]``). Empty: every client sees it."
        ),
    )
    varies: bool = Field(
        default=False,
        description=(
            "The real tool answers the same arguments differently from one call to the next "
            "(search ranking, generated text): replays and parity compare the shape of its "
            "answers only, not their values or lengths."
        ),
    )

    @model_serializer(mode="wrap")
    def _keep_behavior(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Always write ``behavior`` (after the definition), even when it is the default."""
        data: dict[str, object] = handler(self)
        if "behavior" in data:
            return data
        items = list(data.items())
        position = 1 if items and items[0][0] == "definition" else 0
        items.insert(position, ("behavior", self.behavior))
        return dict(items)

    def offered_to(self, declared: object) -> bool:
        """Whether the server lists this tool to a client that declared these capabilities."""
        return not self.requires or any(
            all(has_capability(declared, path) for path in alternative)
            for alternative in self.requires
        )

    @property
    def name(self) -> str:
        """The tool's name."""
        definition = self.definition
        return str(definition.get("name", "")) if isinstance(definition, dict) else ""


class McpModel(Model):
    """Everything about an MCP server except per-call recordings (those live in recordings/)."""

    transport: McpTransportModel = Field(default_factory=McpTransportModel)
    initialize_result: RawJson = Field(
        default=None,
        description="The initialize result: protocolVersion, capabilities, serverInfo.",
    )
    tools: list[McpToolModel] = Field(default_factory=list)
    tools_page_size: int | None = Field(default=None, ge=1)
    resources: list[RawJson] = Field(default_factory=list)
    resource_templates: list[RawJson] = Field(default_factory=list)
    prompts: list[RawJson] = Field(default_factory=list)
    errors: dict[str, RawJson] = Field(
        default_factory=dict,
        description="JSON-RPC error objects by situation: method_not_found, unknown_tool, …",
    )
    capability_tools_after_initialized: bool = Field(
        default=False,
        description=(
            "Tools with requires appear only once the client sent notifications/initialized "
            "(servers that register them then; learned by introspection)."
        ),
    )
    arguments_probe_tool: str | None = Field(
        default=None,
        description="The tool given non-object arguments by introspection (arguments_not_object).",
    )
    listing_extras: dict[str, RawJson] = Field(
        default_factory=dict,
        description=(
            "Per listing method (tools/list …), the recorded result's members in their order, "
            "when it has more than the list and nextCursor (resultType, ttlMs, _meta …); the "
            "list itself is a null placeholder."
        ),
    )

    def tool(self, name: str) -> McpToolModel | None:
        """Look up a tool by name."""
        return next((tool for tool in self.tools if tool.name == name), None)
