"""MCP servers: transport behavior and the exact tools/resources/prompts they advertise."""

from __future__ import annotations

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
