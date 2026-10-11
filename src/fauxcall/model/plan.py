"""Recording plans and the single up-front approval of mutating calls.

The agent writes plans (which real calls to make, why, and how to clean up). Read-only calls run
freely. Mutating calls only run if an approval covers them: either an endpoint-level approval with a
call budget (what users usually grant: "you may create and delete pets in the sandbox, up to 40
calls") or an exact-call approval pinned by fingerprint.
"""

from __future__ import annotations

import fnmatch
import hashlib
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from fauxcall import jsonfmt
from fauxcall.model.common import Model, Purpose, RawJson
from fauxcall.types import HttpMethod


class PlannedFile(Model):
    """A file part for a multipart request: from a path (relative to the fake) or inline."""

    field: str
    filename: str
    content_type: str = "application/octet-stream"
    path: str | None = None
    text: str | None = None
    base64: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> PlannedFile:
        sources = [s for s in (self.path, self.text, self.base64) if s is not None]
        if len(sources) != 1:
            raise ValueError("a planned file needs exactly one of path, text or base64")
        return self


class PlannedRequest(Model):
    """A request to send. Strings may reference captured values as ``{{capture_name}}``."""

    method: HttpMethod
    path: str = Field(description="Concrete path, or a template filled from path_params.")
    path_params: dict[str, str] = Field(default_factory=dict)
    query: list[tuple[str, str]] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: RawJson = None
    text_body: str | None = None
    form: dict[str, str] | None = None
    files: list[PlannedFile] = Field(default_factory=list)
    content_type: str | None = None
    auth: Literal["default", "none", "invalid"] = Field(
        default="default", description="'none'/'invalid' probe the real auth errors."
    )


class PollSpec(Model):
    """Poll a status endpoint until a terminal value appears (measures async job timings)."""

    request: PlannedRequest
    endpoint: str
    state_path: str = Field(description="JSON path of the state in the status response.")
    terminal: list[str] = Field(min_length=1)
    interval_ms: int = Field(default=500, ge=50)
    timeout_s: float = Field(default=120.0, gt=0)


class StreamSpec(Model):
    """Record a streaming response (SSE / WebSocket / chunked)."""

    kind: Literal["sse", "websocket", "chunked"]
    duration_s: float = Field(default=10.0, gt=0)
    max_events: int = Field(default=200, ge=1)
    send: list[RawJson] = Field(
        default_factory=list, description="WebSocket messages to send after connecting."
    )


class PlannedCall(Model):
    """One entry of a plan."""

    id: str
    endpoint: str
    purpose: Purpose = "sample"
    request: PlannedRequest
    repeat: int = Field(default=1, ge=1)
    interval_ms: int = Field(default=0, ge=0)
    capture: dict[str, str] = Field(
        default_factory=dict, description="name -> JSON path in the response body."
    )
    poll: PollSpec | None = None
    stream: StreamSpec | None = None
    cleanup: list[str] = Field(default_factory=list, description="Ids of calls that undo this.")
    mutating: bool | None = Field(default=None, description="Defaults to 'not a safe method'.")
    tags: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def is_mutating(self) -> bool:
        """Whether this call changes state on the real API."""
        if self.mutating is not None:
            return self.mutating
        return not self.request.method.is_safe

    def fingerprint(self) -> str:
        """Stable hash of what the call does (notes and tags excluded)."""
        payload = self.model_dump(mode="json", exclude={"note", "tags"})
        return hashlib.sha256(jsonfmt.canonical_dumps(payload).encode()).hexdigest()[:16]


class RecordingPlan(Model):
    """A named, ordered list of calls."""

    name: str
    created_at: AwareDatetime
    calls: list[PlannedCall] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> RecordingPlan:
        ids = [call.id for call in self.calls]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate call ids: {', '.join(duplicates)}")
        known = set(ids)
        for call in self.calls:
            missing = [c for c in call.cleanup if c not in known]
            if missing:
                raise ValueError(f"call {call.id!r} cleans up with unknown calls {missing}")
        return self

    def mutating_calls(self) -> list[PlannedCall]:
        """Calls that need approval."""
        return [call for call in self.calls if call.is_mutating]


class Approval(Model):
    """Permission to make mutating calls."""

    scope: Literal["endpoint", "call"]
    endpoint: str = Field(description="Endpoint id or glob (scope 'endpoint').")
    max_calls: int | None = Field(default=None, ge=1, description="Budget for scope 'endpoint'.")
    fingerprint: str | None = Field(default=None, description="Exact call (scope 'call').")
    reason: str | None = None
    approved_at: AwareDatetime
    approved_by: str = "user"

    @model_validator(mode="after")
    def _complete(self) -> Approval:
        if self.scope == "call" and not self.fingerprint:
            raise ValueError("call-scoped approvals need a fingerprint")
        return self

    def covers(self, call: PlannedCall) -> bool:
        """Whether this approval covers ``call`` (budget is checked separately)."""
        if self.scope == "call":
            return call.fingerprint() == self.fingerprint
        return fnmatch.fnmatchcase(call.endpoint, self.endpoint)


class Approvals(Model):
    """``data/approvals.json``: the up-front approval plus how much of each budget is used."""

    approvals: list[Approval] = Field(default_factory=list)
    used: dict[str, int] = Field(
        default_factory=dict, description="Endpoint-approval key -> mutating calls made so far."
    )

    @staticmethod
    def budget_key(approval: Approval) -> str:
        """Key under which an endpoint approval's usage is counted."""
        return f"{approval.scope}:{approval.endpoint}:{approval.fingerprint or ''}"

    def find(self, call: PlannedCall) -> Approval | None:
        """The first approval that covers ``call`` and still has budget."""
        for approval in self.approvals:
            if not approval.covers(call):
                continue
            if approval.max_calls is None:
                return approval
            if self.used.get(self.budget_key(approval), 0) < approval.max_calls:
                return approval
        return None

    def consume(self, approval: Approval) -> None:
        """Record one mutating call against ``approval``."""
        key = self.budget_key(approval)
        self.used[key] = self.used.get(key, 0) + 1
