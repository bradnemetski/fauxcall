"""The ``/__fauxcall`` admin API: health, reset, clock control, journal, faults, world, snapshots.

Agent evaluation harnesses use it to put a fake in a known state before a run and to inspect what
the agent did afterwards. Every operation can target one session (``X-Fauxcall-Session`` header or a
``session`` field); otherwise it targets the default session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field

from fauxcall import __version__
from fauxcall.clock import datetime_to_ns
from fauxcall.config import FaultRule, IdentityConfig  # noqa: TC001 - FastAPI reads annotations
from fauxcall.runtime.journal import JournalEntry
from fauxcall.storage import model_to_json

if TYPE_CHECKING:
    from fauxcall.runtime.app import FakeRuntime
    from fauxcall.types import JsonValue


class ClockOperation(BaseModel):
    """A change to the clock."""

    op: Literal["freeze", "advance", "offset", "travel", "scale", "real"]
    at: AwareDatetime | None = None
    seconds: float | None = None
    rate: float | None = None
    session: str | None = None


class ResetRequest(BaseModel):
    """What to reset."""

    session: str | None = Field(default=None, description="Omit to reset every session.")
    journal: bool = False


class RestoreRequest(BaseModel):
    """A snapshot to restore."""

    session: str = "default"
    snapshot: dict[str, object]


def _runtime(request: Request) -> FakeRuntime:
    runtime: FakeRuntime = request.app.state.fauxcall
    return runtime


def _session(request: Request, explicit: str | None = None) -> str:
    runtime = _runtime(request)
    return explicit or request.headers.get(runtime.settings.session_header) or "default"


async def _require_token(
    request: Request, token: Annotated[str | None, Header(alias="X-Fauxcall-Admin-Token")] = None
) -> None:
    expected = _runtime(request).settings.admin_token
    if expected is not None and token != expected.get_secret_value():
        raise HTTPException(status_code=401, detail="admin token required")


def admin_router() -> APIRouter:  # noqa: C901, PLR0915 - a flat list of small endpoints
    """Build the admin router (mounted under the configured prefix)."""
    router = APIRouter(dependencies=[Depends(_require_token)], tags=["fauxcall admin"])

    @router.get("/health")
    async def health(request: Request) -> dict[str, object]:
        runtime = _runtime(request)
        clock = runtime.clocks.for_session(_session(request)).state()
        return {
            "status": "ok",
            "fake": runtime.model.name,
            "title": runtime.model.title,
            "fauxcall_version": __version__,
            "sessions": runtime.world.sessions(),
            "clock": clock.model_dump(mode="json"),
            "jev_active": runtime.jev.active,
            "jev_cached_answers": runtime.jev.cached_answers,
        }

    @router.post("/reset")
    async def reset(request: Request, body: ResetRequest | None = None) -> dict[str, object]:
        runtime = _runtime(request)
        target = body.session if body else None
        runtime.reset(target, journal=bool(body and body.journal))
        return {"reset": target or "all"}

    @router.get("/sessions")
    async def sessions(request: Request) -> dict[str, object]:
        return {"sessions": _runtime(request).world.sessions()}

    @router.get("/clock")
    async def get_clock(request: Request, session: str | None = None) -> dict[str, object]:
        runtime = _runtime(request)
        state = runtime.clocks.for_session(_session(request, session)).state()
        return state.model_dump(mode="json")

    @router.post("/clock")
    async def set_clock(request: Request, operation: ClockOperation) -> dict[str, object]:
        runtime = _runtime(request)
        target = operation.session or request.headers.get(runtime.settings.session_header)
        clock = runtime.clocks.override(target) if target else runtime.clocks.default
        at_ns = datetime_to_ns(operation.at) if operation.at else None
        try:
            match operation.op:
                case "freeze":
                    clock.freeze(at_ns)
                case "advance":
                    clock.advance(_need(operation.seconds, "seconds"))
                case "offset":
                    clock.set_offset(_need(operation.seconds, "seconds"))
                case "travel":
                    clock.travel_to(_need(at_ns, "at"))
                case "scale":
                    clock.scale(_need(operation.rate, "rate"), at_ns)
                case "real":
                    clock.resume_real()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return clock.state().model_dump(mode="json")

    @router.get("/journal")
    async def journal(
        request: Request,
        session: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        path_prefix: str | None = None,
        since: Annotated[
            int | None, Query(description="Entries after this sequence number")
        ] = None,
        limit: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, object]:
        entries = _runtime(request).journal.query(
            session=session,
            endpoint=endpoint,
            method=method,
            path_prefix=path_prefix,
            since_seq=since,
            limit=limit,
        )
        return {"entries": [model_to_json(e, exclude_defaults=False) for e in entries]}

    @router.delete("/journal")
    async def clear_journal(request: Request, session: str | None = None) -> dict[str, object]:
        return {"removed": _runtime(request).journal.clear(session)}

    @router.get("/faults")
    async def get_faults(request: Request) -> dict[str, object]:
        return {"faults": [rule.model_dump(mode="json") for rule in _runtime(request).faults.rules]}

    @router.put("/faults")
    async def put_faults(request: Request, rules: list[FaultRule]) -> dict[str, object]:
        _runtime(request).faults.replace(rules)
        return {"faults": [rule.model_dump(mode="json") for rule in rules]}

    @router.delete("/faults")
    async def clear_faults(request: Request) -> dict[str, object]:
        _runtime(request).faults.replace([])
        return {"faults": []}

    @router.put("/auth/users")
    async def put_users(request: Request, users: list[IdentityConfig]) -> dict[str, object]:
        _runtime(request).auth.update_users(users)
        return {"users": [user.name for user in users]}

    @router.get("/world/{collection}")
    async def world(
        request: Request, collection: str, session: str | None = None
    ) -> dict[str, object]:
        runtime = _runtime(request)
        try:
            records = runtime.world.session(_session(request, session)).list(collection)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"collection": collection, "records": [r.record for r in records]}

    @router.get("/snapshot")
    async def snapshot(request: Request, session: str | None = None) -> dict[str, object]:
        target = _session(request, session)
        state: JsonValue = _runtime(request).world.session(target).snapshot()
        return {"session": target, "snapshot": state}

    @router.post("/restore")
    async def restore(request: Request, body: RestoreRequest) -> dict[str, object]:
        _runtime(request).world.session(body.session).restore(body.snapshot)  # type: ignore[arg-type]
        return {"restored": body.session}

    @router.get("/model")
    async def model(request: Request) -> dict[str, object]:
        dumped = model_to_json(_runtime(request).model)
        assert isinstance(dumped, dict)  # noqa: S101
        return dict(dumped)

    @router.get("/journal/entry-schema", include_in_schema=False)
    async def entry_schema() -> dict[str, object]:
        return JournalEntry.model_json_schema()

    return router


def _need[T](value: T | None, name: str) -> T:
    if value is None:
        raise ValueError(f"'{name}' is required for this operation")
    return value
