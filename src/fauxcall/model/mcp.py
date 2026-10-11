"""MCP servers: transport behavior and the exact tools/resources/prompts they advertise."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from fauxcall.model.common import Model, RawJson
from fauxcall.model.realism import LatencyProfile
from fauxcall.model.semantic import SemanticModel


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
    default_protocol_version: str = "2025-06-18"
    sse_event_ids: bool = False
    content_type_json: str = "application/json"
    content_type_sse: str = "text/event-stream"


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

    def tool(self, name: str) -> McpToolModel | None:
        """Look up a tool by name."""
        return next((tool for tool in self.tools if tool.name == name), None)
