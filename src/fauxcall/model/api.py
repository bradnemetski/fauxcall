"""The API model: everything a fake knows about the real API, in one validated document.

Stored as ``data/model.json``. Built incrementally during ``/fauxcall`` (endpoint by endpoint) and
read by the runtime engine, the scaffolder and the parity comparator.
"""

from __future__ import annotations

import fnmatch
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from fauxcall.errors import ModelError
from fauxcall.jsonfmt import COMPACT, JsonStyle
from fauxcall.model.auth import AuthModel
from fauxcall.model.calendar import CalendarModel
from fauxcall.model.common import Model
from fauxcall.model.endpoints import EndpointModel
from fauxcall.model.errors import ErrorTemplate
from fauxcall.model.fields import CalendarPolicy, SeriesPolicy
from fauxcall.model.jobs import JobModel
from fauxcall.model.mcp import McpModel
from fauxcall.model.realism import RateLimitModel
from fauxcall.model.world import (
    CollectionModel,
    InvariantModel,
    RelationModel,
)
from fauxcall.types import HttpMethod

MODEL_FORMAT_VERSION: Final = 1

SourceKind = Literal["openapi", "swagger_ui", "redoc", "html_docs", "mcp"]


_CHANGES: Final = frozenset({HttpMethod.POST, HttpMethod.PUT, HttpMethod.PATCH})
"""Methods that create or change records."""
_STRATEGY_KEY_TYPES: Final[dict[str, frozenset[str]]] = {
    "increment": frozenset({"int"}),
    "random_int": frozenset({"int", "str"}),
    "uuid4": frozenset({"uuid", "str"}),
}
"""Key types each server-side id strategy can produce (``client`` ids are checked per request)."""


class SourceInfo(Model):
    """Where the fake's knowledge came from."""

    kind: SourceKind
    url: str = Field(description="What the user passed to /fauxcall.")
    spec_url: str | None = Field(default=None, description="The machine-readable spec found.")
    spec_file: str | None = Field(default=None, description="Snapshot under data/source/.")
    spec_version: str | None = None
    title: str | None = None
    api_version: str | None = None
    fetched_at: AwareDatetime


class RealTarget(Model):
    """The real API's location."""

    base_url: str = Field(description="Scheme + host (+ port), no trailing slash.")
    base_path: str = Field(default="", description="Prefix every endpoint path starts with.")


class RoutingModel(Model):
    """Routing quirks of the real server (probed, not assumed)."""

    case_insensitive: bool = False
    trailing_slash: Literal["strict", "redirect", "ignore"] = "strict"
    unknown_route_template: str | None = None
    method_not_allowed_template: str | None = None
    spec_paths: list[str] = Field(
        default_factory=list, description="Paths where the real server serves its own spec."
    )


class ApiModel(Model):
    """Root of ``data/model.json``."""

    format_version: Literal[1] = MODEL_FORMAT_VERSION
    name: str
    title: str
    kind: Literal["rest", "mcp"] = "rest"
    description: str | None = None
    source: SourceInfo
    real: RealTarget
    json_style: JsonStyle = COMPACT
    routing: RoutingModel = Field(default_factory=RoutingModel)
    auth: AuthModel | None = None
    endpoints: list[EndpointModel] = Field(default_factory=list)
    collections: list[CollectionModel] = Field(default_factory=list)
    relations: list[RelationModel] = Field(default_factory=list)
    invariants: list[InvariantModel] = Field(default_factory=list)
    calendars: list[CalendarModel] = Field(default_factory=list)
    jobs: list[JobModel] = Field(default_factory=list)
    rate_limits: list[RateLimitModel] = Field(default_factory=list)
    errors: list[ErrorTemplate] = Field(default_factory=list)
    mcp: McpModel | None = None
    default_headers: dict[str, str] = Field(
        default_factory=dict, description="Contract headers on every response."
    )
    notes: list[str] = Field(default_factory=list)

    # -- lookups -------------------------------------------------------------------------------

    def endpoint(self, endpoint_id: str) -> EndpointModel:
        """The endpoint with this id (raises :class:`ModelError` if missing)."""
        for endpoint in self.endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        raise ModelError(f"unknown endpoint {endpoint_id!r}")

    def find_endpoint(self, endpoint_id: str) -> EndpointModel | None:
        """The endpoint with this id, or ``None``."""
        return next((e for e in self.endpoints if e.id == endpoint_id), None)

    def collection(self, name: str) -> CollectionModel:
        """The collection called ``name``."""
        for collection in self.collections:
            if collection.name == name:
                return collection
        raise ModelError(f"unknown collection {name!r}")

    def error_template(self, name: str) -> ErrorTemplate:
        """The error template called ``name``."""
        for template in self.errors:
            if template.name == name:
                return template
        raise ModelError(f"unknown error template {name!r}")

    def job(self, name: str) -> JobModel:
        """The job model called ``name``."""
        for job in self.jobs:
            if job.name == name:
                return job
        raise ModelError(f"unknown job {name!r}")

    def calendar(self, name: str) -> CalendarModel:
        """The calendar called ``name``."""
        for calendar in self.calendars:
            if calendar.name == name:
                return calendar
        raise ModelError(f"unknown calendar {name!r}")

    def upsert_endpoint(self, endpoint: EndpointModel) -> None:
        """Add an endpoint, replacing one with the same id (keeps declaration order)."""
        for index, existing in enumerate(self.endpoints):
            if existing.id == endpoint.id:
                self.endpoints[index] = endpoint
                return
        self.endpoints.append(endpoint)

    def rate_limits_for(self, endpoint_id: str) -> list[RateLimitModel]:
        """Rate limits whose endpoint globs match ``endpoint_id``."""
        return [
            limit
            for limit in self.rate_limits
            if any(fnmatch.fnmatchcase(endpoint_id, glob) for glob in limit.endpoints)
        ]

    # -- consistency ---------------------------------------------------------------------------

    def writes_to(self, collection: str) -> bool:
        """Whether some endpoint creates or changes records of ``collection``.

        Generic write roles, and custom or computed endpoints on it that POST, PUT or PATCH.
        """
        return any(
            e.collection == collection
            and (
                e.role in {"create", "replace", "update"}
                or (e.role in {"custom", "computed"} and e.method in _CHANGES)
            )
            for e in self.endpoints
        )

    def deletes_from(self, collection: str) -> bool:
        """Whether some endpoint deletes records of ``collection`` (generic, custom or computed)."""
        return any(
            e.collection == collection
            and (
                e.role == "delete"
                or (e.role in {"custom", "computed"} and e.method == HttpMethod.DELETE)
            )
            for e in self.endpoints
        )

    def undecided(self) -> list[str]:
        """Relation rules not decided yet (``null``): what to find out before the fake is done.

        ``on_missing`` matters only when some endpoint writes the referencing collection, and
        ``on_delete`` only when one deletes the target; the reverse minimum always does.
        """
        writes = {r.from_collection for r in self.relations if self.writes_to(r.from_collection)}
        deletes = {r.to_collection for r in self.relations if self.deletes_from(r.to_collection)}
        found: list[str] = []
        for relation in self.relations:
            where = (
                f"relation {relation.name!r} ({relation.from_collection}.{relation.from_field} "
                f"-> {relation.to_collection})"
            )
            if relation.on_missing is None and relation.from_collection in writes:
                found.append(f"{where}: on_missing (refuse or accept a missing target?)")
            if relation.on_delete is None and relation.to_collection in deletes:
                found.append(f"{where}: on_delete (deleting a referenced target does what?)")
            if relation.min_per_target is None:
                found.append(f"{where}: min_per_target (must every target be referenced?)")
        return found

    def problems(self) -> list[str]:
        """Cross-reference problems (dangling names, duplicates). Empty means consistent."""
        found: list[str] = []
        found += _duplicates("endpoint", [e.id for e in self.endpoints])
        found += _duplicates("collection", [c.name for c in self.collections])
        found += _duplicates("error template", [t.name for t in self.errors])
        found += _duplicates("job", [j.name for j in self.jobs])
        found += _duplicates("calendar", [c.name for c in self.calendars])
        found += _duplicates("rate limit", [r.name for r in self.rate_limits])
        found += self._reference_problems()
        found += self._key_problems()
        if self.kind == "mcp" and self.mcp is None:
            found.append("kind is 'mcp' but the model has no 'mcp' section")
        return found

    def _key_problems(self) -> list[str]:
        """Ids the fake assigns must fit the collection's key type."""
        key_types = {c.name: c.key_type for c in self.collections}
        job_collections = {j.name: j.collection for j in self.jobs}
        found: list[str] = []
        for endpoint in self.endpoints:
            if endpoint.role in {"create", "upload"}:
                collection, strategy = endpoint.collection, "increment"
            elif endpoint.role == "job_submit":
                collection, strategy = job_collections.get(endpoint.job or ""), "uuid4"
            else:
                continue
            if endpoint.write is not None:
                strategy = endpoint.write.id_strategy
            key_type = key_types.get(collection or "")
            fits = _STRATEGY_KEY_TYPES.get(strategy)
            if key_type is not None and fits is not None and key_type not in fits:
                found.append(
                    f"endpoint {endpoint.id!r}: id_strategy {strategy!r} can't make the"
                    f" {key_type} keys of collection {collection!r}"
                )
        return found

    def _reference_problems(self) -> list[str]:  # noqa: C901 - one flat checklist
        collections = {c.name for c in self.collections}
        templates = {t.name for t in self.errors}
        jobs = {j.name for j in self.jobs}
        calendars = {c.name for c in self.calendars}
        found: list[str] = []

        def need(kind: str, name: str | None, known: set[str], where: str) -> None:
            if name is not None and name not in known:
                found.append(f"{where}: unknown {kind} {name!r}")

        for endpoint in self.endpoints:
            where = f"endpoint {endpoint.id!r}"
            need("collection", endpoint.collection, collections, where)
            need("job", endpoint.job, jobs, where)
            for case in endpoint.errors:
                need("error template", case.template, templates, where)
            for policy in endpoint.fields:
                if isinstance(policy, CalendarPolicy):
                    need("calendar", policy.calendar, calendars, where)
                if isinstance(policy, SeriesPolicy) and not policy.series:
                    found.append(f"{where}: series policy without a series name")
        for relation in self.relations:
            where = f"relation {relation.name!r}"
            need("collection", relation.from_collection, collections, where)
            need("collection", relation.to_collection, collections, where)
        for job in self.jobs:
            need("collection", job.collection, collections, f"job {job.name!r}")
        for invariant in self.invariants:
            need("collection", invariant.collection, collections, f"invariant {invariant.name!r}")
        for limit in self.rate_limits:
            need("error template", limit.error_template, templates, f"rate limit {limit.name!r}")
        if self.auth is not None:
            for name in (
                self.auth.missing_template,
                self.auth.invalid_template,
                self.auth.forbidden_template,
                self.auth.expired_template,
                self.auth.insufficient_scope_template,
            ):
                need("error template", name, templates, "auth")
        for name in (
            self.routing.unknown_route_template,
            self.routing.method_not_allowed_template,
        ):
            need("error template", name, templates, "routing")
        return found

    def check(self) -> None:
        """Raise :class:`ModelError` listing every consistency problem."""
        problems = self.problems()
        if problems:
            raise ModelError("API model is inconsistent:\n- " + "\n- ".join(problems))


def _duplicates(kind: str, names: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for name in names:
        if name in seen and name not in duplicated:
            duplicated.append(name)
        seen.add(name)
    return [f"duplicate {kind} {name!r}" for name in duplicated]
