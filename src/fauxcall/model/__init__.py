"""The declarative model of a faked API (``data/model.json``) and its companions.

* :mod:`~fauxcall.model.api` — the root :class:`ApiModel`.
* :mod:`~fauxcall.model.endpoints` — REST endpoints, parameters, responses, pagination, writes.
* :mod:`~fauxcall.model.fields` — per-field time/value policies.
* :mod:`~fauxcall.model.world` — collections, relations, invariants, world records.
* :mod:`~fauxcall.model.jobs` — async job state machines.
* :mod:`~fauxcall.model.calendar` — trading/publication calendars.
* :mod:`~fauxcall.model.errors` — error templates.
* :mod:`~fauxcall.model.realism` — latency and rate limits.
* :mod:`~fauxcall.model.auth` — authentication schemes.
* :mod:`~fauxcall.model.semantic` — Jev-backed runtime judgments.
* :mod:`~fauxcall.model.mcp` — MCP transport and capabilities.
* :mod:`~fauxcall.model.recording` — recorded exchanges (evidence).
* :mod:`~fauxcall.model.plan` — recording plans and approvals.
* :mod:`~fauxcall.model.progress` — build checkpoints.
"""

from fauxcall.model.api import ApiModel, RealTarget, RoutingModel, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme, OAuthModel
from fauxcall.model.calendar import CalendarModel, SessionWindow
from fauxcall.model.common import Model, RawJson
from fauxcall.model.endpoints import (
    EndpointModel,
    PaginationModel,
    ParamModel,
    RequestBodyModel,
    ResponseShape,
    ResponseVariant,
    SortModel,
    StreamModel,
    WriteSemantics,
)
from fauxcall.model.errors import ErrorCase, ErrorTemplate
from fauxcall.model.fields import (
    CalendarPolicy,
    ComputedPolicy,
    CounterPolicy,
    EchoPolicy,
    FieldPolicy,
    IgnorePolicy,
    NowPolicy,
    PeriodicPolicy,
    PseudonymPolicy,
    RandomPolicy,
    RelativePolicy,
    SeriesPolicy,
    StaticPolicy,
    VolatilePolicy,
)
from fauxcall.model.jobs import DurationModel, JobModel, JobOutcome, JobStateModel
from fauxcall.model.mcp import McpModel, McpToolModel, McpTransportModel
from fauxcall.model.plan import Approval, Approvals, PlannedCall, PlannedRequest, RecordingPlan
from fauxcall.model.progress import Progress
from fauxcall.model.realism import LatencyProfile, RateLimitModel
from fauxcall.model.recording import Body, CapturedRequest, CapturedResponse, Exchange, StreamEvent
from fauxcall.model.semantic import MatchSpec, OutcomeSpec, SemanticModel, SemanticOutput
from fauxcall.model.world import CollectionModel, InvariantModel, RelationModel, WorldRecord

__all__ = [
    "ApiModel",
    "Approval",
    "Approvals",
    "AuthModel",
    "AuthScheme",
    "Body",
    "CalendarModel",
    "CalendarPolicy",
    "CapturedRequest",
    "CapturedResponse",
    "CollectionModel",
    "ComputedPolicy",
    "CounterPolicy",
    "DurationModel",
    "EchoPolicy",
    "EndpointModel",
    "ErrorCase",
    "ErrorTemplate",
    "Exchange",
    "FieldPolicy",
    "IgnorePolicy",
    "InvariantModel",
    "JobModel",
    "JobOutcome",
    "JobStateModel",
    "LatencyProfile",
    "MatchSpec",
    "McpModel",
    "McpToolModel",
    "McpTransportModel",
    "Model",
    "NowPolicy",
    "OAuthModel",
    "OutcomeSpec",
    "PaginationModel",
    "ParamModel",
    "PeriodicPolicy",
    "PlannedCall",
    "PlannedRequest",
    "Progress",
    "PseudonymPolicy",
    "RandomPolicy",
    "RateLimitModel",
    "RawJson",
    "RealTarget",
    "RecordingPlan",
    "RelationModel",
    "RelativePolicy",
    "RequestBodyModel",
    "ResponseShape",
    "ResponseVariant",
    "RoutingModel",
    "SemanticModel",
    "SemanticOutput",
    "SeriesPolicy",
    "SessionWindow",
    "SortModel",
    "SourceInfo",
    "StaticPolicy",
    "StreamEvent",
    "StreamModel",
    "VolatilePolicy",
    "WorldRecord",
    "WriteSemantics",
]
