from __future__ import annotations

import random
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError

from fauxcall.errors import ModelError
from fauxcall.jsonfmt import JsonFloat, loads
from fauxcall.model import (
    ApiModel,
    Approval,
    Approvals,
    AuthModel,
    AuthScheme,
    Body,
    CalendarModel,
    CalendarPolicy,
    CapturedRequest,
    CapturedResponse,
    CollectionModel,
    DurationModel,
    EndpointModel,
    ErrorCase,
    ErrorTemplate,
    Exchange,
    FieldPolicy,
    InvariantModel,
    JobModel,
    JobOutcome,
    JobStateModel,
    LatencyProfile,
    McpModel,
    McpToolModel,
    PlannedCall,
    PlannedRequest,
    Progress,
    RateLimitModel,
    RealTarget,
    RecordingPlan,
    RelationModel,
    SeriesPolicy,
    SessionWindow,
    SourceInfo,
)
from fauxcall.model.plan import PlannedFile, StreamSpec, WsFrame
from fauxcall.model.progress import PHASES, GateAnswers, PhaseState
from fauxcall.storage import read_model, write_model
from fauxcall.timefmt import TimeFormat
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)


def _api(**updates: object) -> ApiModel:
    fields: dict[str, object] = {
        "name": "petstore",
        "title": "Swagger Petstore",
        "source": SourceInfo(kind="openapi", url="https://x/openapi.json", fetched_at=NOW),
        "real": RealTarget(base_url="https://petstore3.swagger.io", base_path="/api/v3"),
    }
    return ApiModel.model_validate({**fields, **updates})


def _endpoint(method: str, path: str, **extra: object) -> EndpointModel:
    return EndpointModel.model_validate(
        {"id": f"{method} {path}", "method": method, "path": path, **extra}
    )


def test_field_policies_are_discriminated() -> None:
    adapter = TypeAdapter[FieldPolicy](FieldPolicy)
    policy = adapter.validate_python(
        {
            "kind": "calendar",
            "path": "time",
            "calendar": "nyse",
            "format": {"kind": "iso", "fraction_digits": 9},
        }
    )
    assert isinstance(policy, CalendarPolicy)
    assert policy.format == TimeFormat(kind="iso", fraction_digits=9)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "telepathy", "path": "x"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "now", "path": "x"})  # needs a format


def test_endpoint_validation_rules() -> None:
    endpoint = _endpoint(
        "GET",
        "/api/v3/pet/{petId}",
        role="get",
        collection="pets",
        key_param="petId",
        params=[
            {"name": "petId", "location": "path", "required": True, "schema": {"type": "integer"}}
        ],
        errors=[
            {"when": "not_found", "template": "pet_not_found"},
            {"when": "invalid_param", "template": "bad_id", "param": "petId"},
            {"when": "invalid_param", "template": "bad_generic"},
        ],
    )
    assert endpoint.method is HttpMethod.GET
    param = endpoint.param("petId")
    assert param is not None
    assert param.param_schema == {"type": "integer"}
    assert endpoint.param("petId", "query") is None
    case = endpoint.error_case("invalid_param", "petId")
    assert case is not None
    assert case.template == "bad_id"
    generic = endpoint.error_case("invalid_param", "other")
    assert generic is not None
    assert generic.template == "bad_generic"
    assert endpoint.error_case("conflict") is None

    with pytest.raises(ValidationError, match="must be"):
        EndpointModel(id="GET /x", method=HttpMethod.POST, path="/x")
    with pytest.raises(ValidationError, match="needs a collection"):
        _endpoint("GET", "/pets", role="list")
    with pytest.raises(ValidationError, match="needs a job"):
        _endpoint("POST", "/uploads", role="job_submit")
    with pytest.raises(ValidationError, match="needs a stream"):
        _endpoint("GET", "/events", role="sse")


def test_api_model_lookups_and_upsert() -> None:
    api = _api(
        endpoints=[_endpoint("GET", "/api/v3/pet/{petId}", role="get", collection="pets")],
        collections=[CollectionModel(name="pets")],
        errors=[ErrorTemplate(name="nf", status=404, body_text="Pet not found")],
        jobs=[_job()],
        calendars=[CalendarModel(name="nyse", zone="America/New_York")],
        rate_limits=[
            RateLimitModel(name="global", limit=10, window_seconds=1, endpoints=["GET *"])
        ],
    )
    assert api.endpoint("GET /api/v3/pet/{petId}").collection == "pets"
    assert api.find_endpoint("GET /nope") is None
    assert api.collection("pets").path == "world/pets.jsonl"
    assert api.error_template("nf").status == 404
    assert api.job("upload").collection == "uploads"
    assert api.calendar("nyse").weekdays == [0, 1, 2, 3, 4]
    assert [r.name for r in api.rate_limits_for("GET /api/v3/pet/{petId}")] == ["global"]
    assert api.rate_limits_for("POST /api/v3/pet") == []
    for lookup, name in [
        (api.endpoint, "GET /missing"),
        (api.collection, "missing"),
        (api.error_template, "missing"),
        (api.job, "missing"),
        (api.calendar, "missing"),
    ]:
        with pytest.raises(ModelError, match="unknown"):
            lookup(name)
    replacement = _endpoint("GET", "/api/v3/pet/{petId}", role="replay", summary="new")
    api.upsert_endpoint(replacement)
    api.upsert_endpoint(_endpoint("DELETE", "/api/v3/pet/{petId}"))
    assert [e.id for e in api.endpoints] == [
        "GET /api/v3/pet/{petId}",
        "DELETE /api/v3/pet/{petId}",
    ]
    assert api.endpoints[0].summary == "new"


def _job() -> JobModel:
    return JobModel(
        name="upload",
        collection="uploads",
        states=[
            JobStateModel(name="queued", duration=DurationModel(kind="fixed", seconds=1)),
            JobStateModel(name="processing", duration=DurationModel(kind="uniform", low=5, high=9)),
            JobStateModel(name="done", terminal=True),
            JobStateModel(name="failed", terminal=True),
        ],
        outcomes=[
            JobOutcome(state="done", probability=0.97),
            JobOutcome(state="failed", probability=0.03),
        ],
    )


def test_api_model_reports_every_dangling_reference() -> None:
    api = _api(
        kind="mcp",
        endpoints=[
            _endpoint(
                "GET",
                "/a",
                role="get",
                collection="ghost",
                errors=[ErrorCase(when="not_found", template="ghost_template")],
                fields=[
                    {
                        "kind": "calendar",
                        "path": "t",
                        "calendar": "ghost_cal",
                        "format": {"kind": "date"},
                    },
                    SeriesPolicy(path="price", series=""),
                ],
            ),
            _endpoint("GET", "/a", role="replay"),
            _endpoint("POST", "/jobs", role="job_submit", job="ghost_job"),
        ],
        relations=[
            RelationModel(name="r", from_collection="a", from_field="x", to_collection="b"),
        ],
        invariants=[InvariantModel(name="u", kind="unique", collection="c", description="d")],
        jobs=[_job()],
        rate_limits=[RateLimitModel(name="rl", limit=1, window_seconds=1, error_template="t429")],
        auth=AuthModel(missing_template="t401"),
        routing={"unknown_route_template": "t404"},
    )
    problems = api.problems()
    expected_fragments = [
        "duplicate endpoint 'GET /a'",
        "unknown collection 'ghost'",
        "unknown error template 'ghost_template'",
        "unknown calendar 'ghost_cal'",
        "series policy without a series name",
        "unknown job 'ghost_job'",
        "relation 'r': unknown collection 'a'",
        "relation 'r': unknown collection 'b'",
        "job 'upload': unknown collection 'uploads'",
        "invariant 'u': unknown collection 'c'",
        "rate limit 'rl': unknown error template 't429'",
        "auth: unknown error template 't401'",
        "routing: unknown error template 't404'",
        "kind is 'mcp' but the model has no 'mcp' section",
    ]
    for fragment in expected_fragments:
        assert any(fragment in problem for problem in problems), fragment
    with pytest.raises(ModelError, match="inconsistent"):
        api.check()
    _api().check()


def test_model_round_trip_preserves_raw_json_spellings(tmp_path: Path) -> None:
    template = loads('{"title": "Not Found", "status": 404, "ratio": 1.50}')
    api = _api(
        errors=[ErrorTemplate(name="nf", status=404, body_json=template)],
        mcp=McpModel(
            tools=[McpToolModel(definition={"name": "ask", "inputSchema": {"type": "object"}})]
        ),
        calendars=[
            CalendarModel(
                name="ecb",
                zone="Europe/Berlin",
                publications=[time(16, 0)],
                holidays=[date(2026, 12, 25)],
                early_closes={date(2026, 12, 24): time(13, 0)},
                sessions=[SessionWindow(open=time(9), close=time(17))],
            )
        ],
    )
    path = tmp_path / "model.json"
    write_model(path, api)
    assert "1.50" in path.read_text()
    assert '"format_version"' not in path.read_text()  # defaults are omitted
    loaded = read_model(path, ApiModel)
    assert loaded == api
    body = loaded.errors[0].body_json
    assert isinstance(body, dict)
    ratio = body["ratio"]
    assert isinstance(ratio, JsonFloat)
    assert ratio.lexeme == "1.50"
    assert loaded.mcp is not None
    tool = loaded.mcp.tool("ask")
    assert tool is not None
    assert tool.name == "ask"
    assert loaded.mcp.tool("nope") is None
    assert McpToolModel(definition=[1]).name == ""


def test_sub_model_validation() -> None:
    with pytest.raises(ValidationError, match="close must be after open"):
        SessionWindow(open=time(16), close=time(9))
    with pytest.raises(ValidationError, match="weekdays"):
        CalendarModel(name="x", zone="UTC", weekdays=[7])
    assert CalendarModel(name="x", zone="UTC", weekdays=[4, 0, 0]).weekdays == [0, 4]
    with pytest.raises(ValidationError, match="needs seconds"):
        DurationModel(kind="fixed")
    with pytest.raises(ValidationError, match="needs samples"):
        DurationModel(kind="empirical")
    with pytest.raises(ValidationError, match="low <= high"):
        DurationModel(kind="uniform", low=5, high=1)
    with pytest.raises(ValidationError, match="not a declared state"):
        JobModel(
            name="j",
            collection="c",
            states=[JobStateModel(name="a")],
            outcomes=[JobOutcome(state="b", probability=1)],
        )
    with pytest.raises(ValidationError, match="sum to 1"):
        JobModel(
            name="j",
            collection="c",
            states=[JobStateModel(name="a")],
            outcomes=[JobOutcome(state="a", probability=0.5)],
        )
    with pytest.raises(ValidationError, match="either body_json or body_text"):
        ErrorTemplate(name="e", status=400, body_json={"a": 1}, body_text="x")
    with pytest.raises(ValidationError, match="location and a name"):
        AuthScheme(kind="api_key")
    with pytest.raises(ValidationError, match="at least one endpoint"):
        RateLimitModel(name="r", limit=1, window_seconds=1, endpoints=[])
    with pytest.raises(ValidationError):
        CollectionModel.model_validate({"name": "x", "unknown_field": 1})


def test_latency_profile_from_samples() -> None:
    assert LatencyProfile.from_samples([]) == LatencyProfile()
    single = LatencyProfile.from_samples([42.0, -1.0])
    assert single.p50_ms == single.p99_ms == 42.0
    profile = LatencyProfile.from_samples([float(n) for n in range(1, 501)])
    assert len(profile.samples_ms) == 200
    assert profile.p50_ms < profile.p90_ms < profile.p99_ms


def test_latency_of_answers_and_errors() -> None:
    rng = random.Random(7)
    split = LatencyProfile.from_samples([15000.0, 12000.0], errors=[250.0, -1.0])
    assert (split.samples_ms, split.error_samples_ms) == ([12000.0, 15000.0], [250.0])
    assert split.p50_ms == 13500.0  # percentiles describe answers
    assert split.pick_ms(rng, error=True) == 250.0
    assert split.pick_ms(rng) in {12000.0, 15000.0}
    answers_only = LatencyProfile.from_samples([900.0])
    assert answers_only.pick_ms(rng, error=True) == 900.0  # errors take answers' times
    errors_only = LatencyProfile.from_samples([], errors=[300.0])
    assert (errors_only.pick_ms(rng), errors_only.p50_ms) == (300.0, 0.0)
    assert LatencyProfile().pick_ms(rng) is None


def test_exchange_and_body_helpers() -> None:
    body = Body(kind="json", json_value=loads('{"price": 105.50}'), content_type="application/json")
    assert body.raw_text() == '{"price":105.50}'
    assert body.parsed() == {"price": 105.5}
    assert Body(kind="text", text="Invalid Timezone").raw_text() == "Invalid Timezone"
    assert Body(kind="text", text="x").parsed() is None
    request = CapturedRequest(
        method=HttpMethod.GET,
        url="https://x/a?tz=UTC&tz=CET",
        path="/a",
        query=[("tz", "UTC"), ("tz", "CET")],
    )
    assert request.query_value("tz") == "UTC"
    assert request.query_value("nope") is None
    response = CapturedResponse(status=200, headers=[("Content-Type", "application/json")])
    assert response.header("content-type") == "application/json"
    assert response.header("x-missing") is None
    exchange = Exchange(
        id="000001",
        endpoint="GET /a",
        started_at=NOW,
        started_ns=0,
        request=request,
        response=response,
    )
    assert Exchange.model_validate(exchange.model_dump(by_alias=True)) == exchange


def test_websocket_frames() -> None:
    spec = StreamSpec.model_validate(
        {
            "kind": "websocket",
            "duration_s": 1,
            "frames": [
                {"text": "ping"},
                {"at_ms": 250, "json": {"action": "subscribe"}},
                {"at_ms": 500, "base64": "AAE="},
            ],
        }
    )
    assert [frame.payload() for frame in spec.frames] == [
        "ping",
        '{"action":"subscribe"}',
        b"\x00\x01",
    ]
    problems: list[tuple[dict[str, object], str]] = [
        ({"kind": "websocket", "frames": [{"text": "a", "base64": "AAE="}]}, "exactly one of"),
        ({"kind": "websocket", "frames": [{"at_ms": 5}]}, "exactly one of"),
        ({"kind": "websocket", "frames": [{"base64": "not base64!"}]}, "not valid base64"),
        (
            {"kind": "websocket", "frames": [{"at_ms": 9, "text": "a"}, {"at_ms": 1, "text": "b"}]},
            "at_ms order",
        ),
        (
            {"kind": "websocket", "duration_s": 1, "frames": [{"at_ms": 1000, "text": "late"}]},
            "within duration_s",
        ),
        ({"kind": "sse", "frames": [{"text": "a"}]}, "kind 'websocket'"),
    ]
    for problem, message in problems:
        with pytest.raises(ValidationError, match=message):
            StreamSpec.model_validate(problem)


def test_frames_leave_older_fingerprints_alone() -> None:
    stream = StreamSpec(kind="sse", send=["hi"])
    call = PlannedCall(
        id="b",
        endpoint="POST /ws",
        request=PlannedRequest(method=HttpMethod.POST, path="/ws"),
        stream=stream,
    )
    assert call.fingerprint() == "fb67115c9eaaba81"  # computed before frames existed
    timed = StreamSpec(kind="websocket", send=["hi"], frames=[WsFrame(text="x")])
    assert call.model_copy(update={"stream": timed}).fingerprint() != call.fingerprint()


def test_plans_fingerprints_and_approvals() -> None:
    create = PlannedCall(
        id="create",
        endpoint="POST /api/v3/pet",
        request=PlannedRequest(method=HttpMethod.POST, path="/api/v3/pet", json_body={"name": "x"}),
        capture={"pet_id": "$.id"},
        cleanup=["delete"],
        note="first",
    )
    delete = PlannedCall(
        id="delete",
        endpoint="DELETE /api/v3/pet/{petId}",
        request=PlannedRequest(method=HttpMethod.DELETE, path="/api/v3/pet/{{pet_id}}"),
    )
    read = PlannedCall(
        id="read",
        endpoint="GET /api/v3/pet/{petId}",
        request=PlannedRequest(method=HttpMethod.GET, path="/api/v3/pet/1"),
    )
    forced = PlannedCall(
        id="search",
        endpoint="POST /search",
        request=PlannedRequest(method=HttpMethod.POST, path="/search"),
        mutating=False,
    )
    plan = RecordingPlan(name="pets", created_at=NOW, calls=[create, delete, read, forced])
    assert [c.id for c in plan.mutating_calls()] == ["create", "delete"]
    assert create.fingerprint() == create.model_copy(update={"note": "changed"}).fingerprint()
    assert create.fingerprint() != create.model_copy(update={"repeat": 2}).fingerprint()

    approvals = Approvals(
        approvals=[
            Approval(scope="endpoint", endpoint="POST /api/v3/*", max_calls=1, approved_at=NOW),
            Approval(
                scope="call",
                endpoint=delete.endpoint,
                fingerprint=delete.fingerprint(),
                approved_at=NOW,
            ),
        ]
    )
    approval = approvals.find(create)
    assert approval is not None
    approvals.consume(approval)
    assert approvals.find(create) is None  # budget exhausted
    exact = approvals.find(delete)
    assert exact is not None
    assert exact.scope == "call"
    approvals.consume(exact)
    assert approvals.find(delete) is not None  # exact approvals have no budget
    assert approvals.find(read) is None
    with pytest.raises(ValidationError, match="fingerprint"):
        Approval(scope="call", endpoint="x", approved_at=NOW)

    with pytest.raises(ValidationError, match="duplicate call ids"):
        RecordingPlan(name="p", created_at=NOW, calls=[read, read])
    with pytest.raises(ValidationError, match="unknown calls"):
        RecordingPlan(name="p", created_at=NOW, calls=[create])
    with pytest.raises(ValidationError, match="exactly one"):
        PlannedFile(field="file", filename="a.txt")
    PlannedFile(field="file", filename="a.txt", text="hello")


def test_progress_tracks_phases_and_endpoints() -> None:
    progress = Progress(fake="petstore", source="https://x", started_at=NOW, updated_at=NOW)
    assert progress.next_action() == "discover: pending"
    progress.phases["discover"] = PhaseState(status="done")
    progress.phases["gate"] = PhaseState(status="blocked", notes=["waiting for API key"])
    assert progress.next_action() == "gate: blocked (waiting for API key)"
    progress.phases["gate"] = PhaseState(status="blocked")
    assert progress.next_action() == "gate: blocked (see notes)"
    progress.phases["gate"] = PhaseState(status="done")
    entry = progress.endpoint("GET /a")
    assert progress.endpoint("GET /a") is entry
    assert progress.next_action() == "record: endpoint 'GET /a' -> recorded"
    entry.steps["recorded"] = "done"
    entry.steps["analyzed"] = "skipped"
    assert progress.next_action() == "record: endpoint 'GET /a' -> modeled"
    for step in ("modeled", "implemented", "tested"):
        entry.steps[step] = "done"
    assert entry.next_step() is None
    assert progress.next_action() == "record: pending"
    for phase in PHASES:
        progress.phases[phase] = PhaseState(status="done")
    progress.phases["review"] = PhaseState(status="in_progress", started_at=NOW)
    assert progress.next_action() == (
        f"review: in progress since {NOW:%Y-%m-%d %H:%M} UTC; if no reviewer is running now, "
        "it was interrupted: run it again (in the foreground)"
    )
    progress.phases["review"] = PhaseState(status="in_progress")
    assert progress.next_action().startswith("review: in progress; if no reviewer")
    progress.phases["review"] = PhaseState(status="done")
    assert progress.next_action() == "done"
    progress.gate = GateAnswers(jev_key=True, mutations="approved")
    assert Progress.model_validate(progress.model_dump()) == progress
