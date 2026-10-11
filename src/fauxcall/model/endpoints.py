"""REST endpoints: signature, role, and everything the engine needs to answer like the real one."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fauxcall.model.common import Model, RawJson
from fauxcall.model.errors import ErrorCase, ErrorCondition
from fauxcall.model.fields import FieldPolicy
from fauxcall.model.realism import LatencyProfile
from fauxcall.model.semantic import SemanticModel
from fauxcall.types import HttpMethod

EndpointRole = Literal[
    "list",
    "get",
    "create",
    "replace",
    "update",
    "delete",
    "search",
    "job_submit",
    "job_status",
    "download",
    "upload",
    "sse",
    "websocket",
    "computed",
    "replay",
    "echo",
    "custom",
]
"""What the endpoint does.

Generic roles are handled by the engine; ``computed``/``custom`` endpoints are implemented in the
fake's own code using engine helpers.
"""

ParamLocation = Literal["path", "query", "header", "cookie"]
FilterOperator = Literal[
    "eq", "in", "contains", "icontains", "prefix", "gte", "lte", "gt", "lt", "search"
]


class ParamModel(Model):
    """A request parameter and how the real API treats it."""

    name: str
    location: ParamLocation
    required: bool = False
    param_schema: RawJson = Field(default=None, alias="schema", description="JSON schema (spec).")
    style: str | None = None
    explode: bool | None = None
    default: RawJson = None
    on_invalid: Literal["error", "ignore", "coerce", "unknown"] = Field(
        default="unknown", description="Observed reaction to a malformed value."
    )
    maps_to: str | None = Field(
        default=None, description="Record field this parameter filters on (list/search roles)."
    )
    operator: FilterOperator = "eq"
    separator: str | None = Field(
        default=None, description="For 'in' filters given as one delimited string, e.g. ','."
    )


class RequestBodyModel(Model):
    """The accepted request body."""

    content_types: list[str] = Field(default_factory=lambda: ["application/json"])
    body_schema: RawJson = Field(default=None, alias="schema")
    required: bool = False
    validate_schema: bool = Field(
        default=False, description="Reject bodies violating the schema (the real API does)."
    )


class HeaderRule(Model):
    """A contract header on a response: a fixed value or a ``{{placeholder}}`` template."""

    value: str


class ResponseVariant(Model):
    """One documented/observed response of an endpoint."""

    status: int = Field(ge=100, le=599)
    description: str | None = None
    content_type: str | None = Field(default=None, description="Exact Content-Type header value.")
    documented_schema: RawJson = None
    observed_schema: RawJson = None
    headers: dict[str, HeaderRule] = Field(default_factory=dict)
    examples: list[str] = Field(default_factory=list, description="Exchange ids.")


class PaginationModel(Model):
    """How a list endpoint pages through results."""

    style: Literal["offset", "page", "cursor", "link_header", "none"] = "none"
    limit_param: str | None = None
    offset_param: str | None = None
    page_param: str | None = None
    cursor_param: str | None = None
    default_limit: int | None = Field(default=None, ge=1)
    max_limit: int | None = Field(default=None, ge=1)
    first_page: int = 1
    total_path: str | None = Field(default=None, description="Body path of the total count.")
    next_path: str | None = Field(default=None, description="Body path of the next cursor/URL.")
    has_more_path: str | None = None
    cursor_format: Literal["opaque", "base64_json", "offset", "key"] = "opaque"


class SortModel(Model):
    """How a list endpoint sorts."""

    param: str | None = None
    order_param: str | None = None
    default: str | None = Field(default=None, description="Default sort field path.")
    descending_prefix: str | None = Field(default="-", description="e.g. '-created_at'.")


class ResponseShape(Model):
    """How the success body is built from records.

    ``record``: the record itself. ``list``: a bare JSON array of records. ``envelope``: a recorded
    body (``template``) whose ``items_path`` receives the records and whose pagination fields are
    recomputed. ``raw``: a recorded body returned as-is (replay).
    """

    kind: Literal["record", "list", "envelope", "raw"] = "record"
    items_path: str | None = None
    template: RawJson = None
    projection: list[str] | None = Field(
        default=None, description="Only these fields of each record (summary listings)."
    )


class WriteSemantics(Model):
    """What a write does in the real API — learned by writing and reading back."""

    persist: bool = Field(default=True, description="Writes are visible to later reads.")
    echo: Literal["record", "request", "empty", "message"] = "record"
    id_strategy: Literal["increment", "uuid4", "random_int", "client"] = "increment"
    id_start: int | None = None
    defaults: RawJson = Field(default=None, description="Fields the server fills in.")
    merge: bool = Field(default=False, description="Update merges into the record (PATCH-like).")
    upsert: bool = False
    status: int = 200
    message_template: RawJson = Field(
        default=None, description="Body for echo='message' (e.g. Petstore's DELETE)."
    )
    location_header: str | None = Field(
        default=None, description="Template for a Location header, e.g. '/pets/{{id}}'."
    )


class StreamModel(Model):
    """Streaming responses (SSE, WebSocket, chunked) replayed from recordings."""

    kind: Literal["sse", "websocket", "chunked"]
    exchanges: list[str] = Field(default_factory=list, description="Recordings to replay.")
    speed: float = Field(default=1.0, gt=0, description="2.0 replays twice as fast.")
    loop: bool = False
    respond_to: Literal["none", "match", "echo"] = Field(
        default="none", description="WebSocket: how client messages are answered."
    )


class EndpointModel(Model):
    """One REST endpoint."""

    id: str = Field(description="'<METHOD> <full path template>', e.g. 'GET /api/v3/pet/{petId}'.")
    method: HttpMethod
    path: str = Field(description="Full path template as served (including any base path).")
    spec_path: str | None = Field(default=None, description="The path as written in the spec.")
    operation_id: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    role: EndpointRole = "replay"
    collection: str | None = None
    key_param: str | None = None
    key_field: str | None = None
    params: list[ParamModel] = Field(default_factory=list)
    request_body: RequestBodyModel | None = None
    responses: list[ResponseVariant] = Field(default_factory=list)
    success_status: int = 200
    shape: ResponseShape = Field(default_factory=ResponseShape)
    pagination: PaginationModel | None = None
    sort: SortModel | None = None
    write: WriteSemantics | None = None
    fields: list[FieldPolicy] = Field(default_factory=list)
    errors: list[ErrorCase] = Field(default_factory=list)
    latency: LatencyProfile | None = None
    job: str | None = None
    stream: StreamModel | None = None
    semantic: SemanticModel | None = None
    handler: str | None = Field(
        default=None, description="Custom handler (computed/custom roles), for documentation."
    )
    auth_required: bool | None = Field(default=None, description="Overrides the API default.")
    deprecated: bool = False
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> EndpointModel:
        expected = f"{self.method.value} {self.path}"
        if self.id != expected:
            raise ValueError(f"endpoint id {self.id!r} must be {expected!r}")
        collection_roles = {"list", "get", "create", "replace", "update", "delete", "search"}
        if self.role in collection_roles and self.collection is None:
            raise ValueError(f"role {self.role!r} needs a collection")
        if self.role in {"job_submit", "job_status"} and self.job is None:
            raise ValueError(f"role {self.role!r} needs a job")
        if self.role in {"sse", "websocket"} and self.stream is None:
            raise ValueError(f"role {self.role!r} needs a stream")
        return self

    def param(self, name: str, location: ParamLocation | None = None) -> ParamModel | None:
        """Look up a parameter by name (and optionally location)."""
        return next(
            (
                p
                for p in self.params
                if p.name == name and (location is None or p.location == location)
            ),
            None,
        )

    def error_case(self, condition: ErrorCondition, param: str | None = None) -> ErrorCase | None:
        """The error case for a condition, preferring a parameter-specific one."""
        cases = [case for case in self.errors if case.when == condition]
        specific = next((case for case in cases if param and case.param == param), None)
        return specific or next((case for case in cases if case.param is None), None)
