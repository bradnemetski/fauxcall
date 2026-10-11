"""CLI: plans, the up-front approval, recording, and analysis of the evidence."""

from __future__ import annotations

import fnmatch
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path  # noqa: TC003 - Typer resolves the annotations at runtime
from typing import TYPE_CHECKING, Annotated, Final
from urllib.parse import urlencode

import typer

from fauxcall import jsonfmt
from fauxcall.cli import common
from fauxcall.cli.common import (
    clean_errors,
    cli_settings,
    echo,
    fail,
    fake_settings,
    layout,
    load_model,
    now,
    run,
    save_model,
    secret_values,
)
from fauxcall.jsonfmt import infer_style
from fauxcall.model.errors import ErrorTemplate
from fauxcall.model.plan import Approval, Approvals, PlannedCall, PlannedRequest, RecordingPlan
from fauxcall.model.realism import LatencyProfile
from fauxcall.recording.mcp import McpRecorder
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.recording.recorder import RealTarget, Recorder
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import read_model, write_model

if TYPE_CHECKING:
    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.model.mcp import McpToolModel
    from fauxcall.model.recording import Exchange
    from fauxcall.paths import FakeLayout
    from fauxcall.types import JsonValue

ERROR_STATUS: Final = 400
"""Responses from this status up are errors."""
SUCCESS: Final = range(200, 300)
"""Successful statuses."""

plan_app = typer.Typer(help="Recording plans and the single up-front approval of mutating calls.")


def _plans(fake: FakeLayout) -> list[tuple[Path, RecordingPlan]]:
    if not fake.plans.is_dir():
        return []
    return [(path, read_model(path, RecordingPlan)) for path in sorted(fake.plans.glob("*.json"))]


def _approvals(fake: FakeLayout) -> Approvals:
    return (
        read_model(fake.approvals_path, Approvals) if fake.approvals_path.exists() else Approvals()
    )


def _example(schema: JsonValue) -> str:
    if not isinstance(schema, dict):
        return "value"
    for key in ("example", "default"):
        if key in schema:
            return str(schema[key])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return str(enum[0])
    kind = schema.get("type")
    return {"integer": "1", "number": "1.5", "boolean": "true"}.get(str(kind), "value")


@plan_app.command("template")
@clean_errors
def plan_template(
    name: str,
    endpoint: str,
    purpose: Annotated[
        str, typer.Option(help="sample | variation | edge | invalid | time_profile | …")
    ] = "sample",
) -> None:
    """Print a starter plan entry for an endpoint (fill in real values before recording)."""
    model = load_model(layout(name))
    found = model.find_endpoint(endpoint)
    if found is None:
        raise fail(f"unknown endpoint {endpoint!r}")
    path_params = {p.name: _example(p.param_schema) for p in found.params if p.location == "path"}
    query = [
        (p.name, _example(p.param_schema))
        for p in found.params
        if p.location == "query" and p.required
    ]
    body: JsonValue = {} if found.request_body is not None else None
    call = PlannedCall.model_validate(
        {
            "id": f"{found.method.value.lower()}-{len(found.path)}-{purpose}",
            "endpoint": found.id,
            "purpose": purpose,
            "request": PlannedRequest(
                method=found.method,
                path=found.path,
                path_params=path_params,
                query=query,
                json_body=body,
            ),
        }
    )
    echo(call.model_dump_json(indent=2, exclude_defaults=True))


@plan_app.command("review")
@clean_errors
def plan_review(name: str) -> None:
    """Every mutating call across all plans — what the user approves once, up front."""
    fake = layout(name)
    approvals = _approvals(fake)
    per_endpoint: Counter[str] = Counter()
    rows: list[str] = []
    for path, plan in _plans(fake):
        for call in plan.mutating_calls():
            per_endpoint[call.endpoint] += call.repeat
            covered = "yes" if approvals.find(call) else "no"
            cleanup = ", ".join(call.cleanup) or "-"
            rows.append(
                f"| {path.name} | {call.id} | `{call.endpoint}` | {call.repeat} | {cleanup} | "
                f"{covered} | {call.note or ''} |"
            )
    if not rows:
        echo("No mutating calls planned.")
        return
    echo(
        "| plan | call | endpoint | times | cleaned up by | approved | why |\n"
        "|---|---|---|---|---|---|---|"
    )
    echo("\n".join(rows))
    echo("\nTotals per endpoint: " + ", ".join(f"{k} x{v}" for k, v in per_endpoint.items()))


@plan_app.command("approve")
@clean_errors
def plan_approve(
    name: str,
    endpoint: Annotated[str | None, typer.Option(help="Endpoint id or glob to approve.")] = None,
    max_calls: Annotated[
        int | None,
        typer.Option(
            help="Budget for --endpoint. A glob shares it across every endpoint it matches, "
            "unless --per-endpoint."
        ),
    ] = None,
    per_endpoint: Annotated[
        bool,
        typer.Option(
            "--per-endpoint",
            help="With a glob: one approval, with its own budget, per matching mutating endpoint.",
        ),
    ] = False,
    planned: Annotated[
        bool,
        typer.Option("--planned", help="Approve every mutating endpoint in the current plans."),
    ] = False,
    margin: Annotated[
        float, typer.Option(help="Budget multiplier for --planned (retries, refinements).")
    ] = 2.0,
    reason: Annotated[
        str | None, typer.Option(help="What the user approved, in their words.")
    ] = None,
) -> None:
    """Record the user's up-front approval (run only after the user said yes)."""
    fake = layout(name)
    approvals = _approvals(fake)
    added: list[Approval] = []
    if endpoint:
        targets = _mutating_matches(load_model(fake), endpoint) if per_endpoint else [endpoint]
        if not targets:
            raise fail(
                f"no mutating endpoint matches {endpoint!r} (see: fauxcall endpoints {name})"
            )
        added += [
            Approval(
                scope="endpoint",
                endpoint=target,
                max_calls=max_calls,
                reason=reason,
                approved_at=now(),
            )
            for target in targets
        ]
    if planned:
        counts: Counter[str] = Counter()
        for _, plan in _plans(fake):
            for call in plan.mutating_calls():
                counts[call.endpoint] += call.repeat
        for endpoint_id, count in counts.items():
            budget = max(1, int(count * margin))
            added.append(
                Approval(
                    scope="endpoint",
                    endpoint=endpoint_id,
                    max_calls=budget,
                    reason=reason,
                    approved_at=now(),
                )
            )
    if not added:
        raise fail("nothing to approve: pass --endpoint or --planned")
    approvals.approvals.extend(added)
    write_model(fake.approvals_path, approvals)
    echo("\n".join(f"approved {a.endpoint} (max {a.max_calls or 'unlimited'})" for a in added))


def _mutating_matches(model: ApiModel, pattern: str) -> list[str]:
    """Endpoints a glob matches that can need an approval: writes and MCP tool calls."""
    ids = [e.id for e in model.endpoints if not e.method.is_safe]
    if model.mcp is not None:
        ids += [f"MCP tools/call:{tool.name}" for tool in model.mcp.tools]
    return [endpoint_id for endpoint_id in ids if fnmatch.fnmatchcase(endpoint_id, pattern)]


@plan_app.command("check")
@clean_errors
def plan_check(name: str, plan: Path) -> None:
    """Validate a plan file and show which calls need approval."""
    fake = layout(name)
    try:
        parsed = read_model(plan, RecordingPlan)
    except ValueError as error:
        raise fail(f"invalid plan: {error}") from error
    model = load_model(fake)
    approvals = _approvals(fake)
    undocumented = {
        c.endpoint
        for c in parsed.calls
        if model.find_endpoint(c.endpoint) is None and not c.endpoint.startswith("MCP ")
    }
    # Routing probes (purpose "edge") may target paths the API doesn't have: that's the point.
    unknown = [
        c.endpoint for c in parsed.calls if c.endpoint in undocumented and c.purpose != "edge"
    ]
    for call in parsed.calls:
        state = (
            "read-only"
            if not call.is_mutating
            else ("approved" if approvals.find(call) else "NEEDS APPROVAL")
        )
        probe = " (routing probe: not in the model)" if call.endpoint in undocumented else ""
        echo(f"- {call.id}: {call.endpoint} x{call.repeat} — {state}{probe}")
    if unknown:
        raise fail(f"unknown endpoints: {', '.join(sorted(set(unknown)))}")


def _privacy(fake: FakeLayout, secrets: list[str], key: str | None) -> RecordingPrivacy:
    config_path = fake.data / "privacy.json"
    config = read_model(config_path, PrivacyConfig) if config_path.exists() else PrivacyConfig()
    return RecordingPrivacy(config, secret_values=secrets, pseudonym_key=key)


@clean_errors
def record(
    name: str,
    plan: Annotated[Path, typer.Argument(help="A plan file, usually data/plans/<endpoint>.json.")],
) -> None:
    """Execute a plan against the real API; every exchange is stored under data/recordings."""
    fake = layout(name)
    model = load_model(fake)
    settings = fake_settings(fake)
    cli = cli_settings()
    parsed = read_model(plan, RecordingPlan)
    headers = {k: v.get_secret_value() for k, v in settings.real.headers.items()}
    query = {k: v.get_secret_value() for k, v in settings.real.query.items()}
    key = settings.pseudonym_key.get_secret_value() if settings.pseudonym_key else None
    privacy = _privacy(fake, secret_values(settings), key)
    approvals = _approvals(fake)
    store = RecordingStore(fake.recordings)

    async def go() -> str:
        async with common.http_client(follow_redirects=False) as http:
            if model.kind == "mcp":
                tools = {t.name: t.definition for t in (model.mcp.tools if model.mcp else [])}
                mcp = McpRecorder(
                    store,
                    model.source.spec_url or model.source.url,  # the endpoint init resolved
                    http,
                    headers=headers,
                    privacy=privacy,
                    approvals=approvals,
                    approvals_path=fake.approvals_path,
                    rps=cli.record_rps,
                    max_calls=cli.record_max_calls,
                )
                report = await mcp.run(parsed, tools)
            else:
                target = RealTarget(
                    base_url=settings.real.base_url or model.real.base_url,
                    headers=headers,
                    query=query,
                    timeout_s=settings.real.timeout_seconds,
                    user_agent=cli.user_agent,
                )
                recorder = Recorder(
                    store,
                    target,
                    http,
                    approvals,
                    approvals_path=fake.approvals_path,
                    privacy=privacy,
                    rps=cli.record_rps,
                    max_calls=cli.record_max_calls,
                    files_root=fake.root,
                )
                report = await recorder.run(parsed)
        return report.summary()

    echo(run(go()))


@clean_errors
def sql(name: str, query: Annotated[str, typer.Argument(help="SELECT … FROM exchanges …")]) -> None:
    """Ad-hoc SQL over recordings (throwaway in-memory SQLite; JSON functions available)."""
    import sqlite3  # noqa: PLC0415

    from fauxcall.analysis.sqlview import as_table, build  # noqa: PLC0415
    from fauxcall.analysis.sqlview import query as run_query  # noqa: PLC0415

    fake = layout(name)
    connection = build(exchange for _, exchange in RecordingStore(fake.recordings).iter_exchanges())
    try:
        columns, rows = run_query(connection, query)
    except sqlite3.Error as error:
        raise fail(f"{type(error).__name__}: {error}") from error
    finally:
        connection.close()
    echo(as_table(columns, rows))


_PREVIEW: Final = 110


def _preview(exchange: Exchange) -> str:
    response = exchange.response
    if response is None:
        return "(no response)"
    if exchange.stream:
        first = next((event.data for event in exchange.stream if event.data), "")
        text = f"<{len(exchange.stream)} stream events> {first}"
    elif response.body.kind == "binary":
        text = f"<{response.body.size} bytes>"
    else:
        text = response.body.raw_text() or ""
    text = " ".join(text.split())
    return text if len(text) <= _PREVIEW else text[: _PREVIEW - 1] + "…"


def _row(exchange: Exchange) -> str:
    request, response = exchange.request, exchange.response
    query = "?" + urlencode(request.query) if request.query else ""
    if response is None:
        outcome = "no response"
    else:
        content_type = (response.header("content-type") or "").split(";")[0]
        outcome = f"{response.status} {exchange.elapsed_ms:.0f}ms {content_type}".rstrip()
    label = exchange.plan_call or "-"
    return (
        f"{exchange.id} {label} [{exchange.purpose}] {request.method.value} {request.path}{query}"
        f" -> {outcome}  {_preview(exchange)}"
    )


@clean_errors
def recordings(
    name: str,
    endpoint: Annotated[
        str | None, typer.Option(help="Endpoint id or glob, e.g. 'GET /v1/tickers*'.")
    ] = None,
    purpose: Annotated[str | None, typer.Option(help="sample, invalid, auth, edge …")] = None,
    status: Annotated[int | None, typer.Option(help="Only this response status.")] = None,
    grep: Annotated[
        str | None, typer.Option(help="Only exchanges containing this text anywhere.")
    ] = None,
    show: Annotated[str | None, typer.Option(help="Print one exchange in full (by id).")] = None,
    limit: Annotated[int, typer.Option(min=1, help="Most rows to print (the newest).")] = 60,
) -> None:
    """Browse recordings: one line per exchange (id, plan call, request, status, time, body)."""
    store = RecordingStore(layout(name).recordings)
    if show is not None:
        for path, exchange in store.iter_exchanges():
            if exchange.id == show or (
                show.isdigit() and exchange.id.lstrip("0") == show.lstrip("0")
            ):
                echo(path.read_text(encoding="utf-8").rstrip())
                return
        raise fail(f"no recording {show!r} (list them with: fauxcall recordings {name})")
    rows: list[str] = []
    for _, exchange in store.iter_exchanges():
        response = exchange.response
        if (
            (endpoint is not None and not fnmatch.fnmatchcase(exchange.endpoint, endpoint))
            or (purpose is not None and exchange.purpose != purpose)
            or (status is not None and (response is None or response.status != status))
            or (grep is not None and grep not in exchange.model_dump_json())
        ):
            continue
        rows.append(_row(exchange))
    if not rows:
        echo("no recordings match")
        return
    if len(rows) > limit:
        echo(f"… {len(rows) - limit} earlier recordings not shown (filter, or raise --limit)")
    for row in rows[-limit:]:
        echo(row)


# -- analyze --------------------------------------------------------------------------------------

analyze_app = typer.Typer(help="Turn recordings into evidence for the model.")


def _bodies(exchanges: list[Exchange]) -> list[JsonValue]:
    return [
        e.response.body.parsed()
        for e in exchanges
        if e.response is not None and e.response.body.kind == "json"
    ]


def _by_status(exchanges: list[Exchange]) -> dict[int, list[Exchange]]:
    grouped: defaultdict[int, list[Exchange]] = defaultdict(list)
    for exchange in exchanges:
        if exchange.response is not None:
            grouped[exchange.response.status].append(exchange)
    return dict(grouped)


def _status_table(by_status: dict[int, list[Exchange]]) -> None:
    echo("| status | count | content-type | example |\n|---|---|---|---|")
    for status, group in sorted(by_status.items()):
        response = group[0].response
        assert response is not None  # noqa: S101 - grouped by response status
        echo(f"| {status} | {len(group)} | {response.header('content-type')} | {group[0].id} |")


def _field_report(success: list[Exchange]) -> None:
    from fauxcall.analysis.profile import profile_documents, profile_table  # noqa: PLC0415
    from fauxcall.analysis.timing import evidence_table, time_fields  # noqa: PLC0415

    bodies = _bodies(success)
    if not bodies:
        return
    echo("\n## Fields (successful responses)\n")
    echo(profile_table(profile_documents(bodies)))
    evidence = time_fields(success)
    if evidence:
        echo("\n## Timestamp behavior\n")
        echo(evidence_table(evidence))


def _schema_report(found: EndpointModel, by_status: dict[int, list[Exchange]]) -> None:
    from fauxcall.analysis.schema import compare_schemas, infer_schema  # noqa: PLC0415

    for variant in found.responses:
        group_bodies = _bodies(by_status.get(variant.status, []))
        if not group_bodies or not isinstance(variant.documented_schema, dict):
            continue
        differences = compare_schemas(variant.documented_schema, infer_schema(group_bodies))
        if differences:
            echo(f"\n## Documented vs observed ({variant.status})\n")
            for difference in differences[:30]:
                echo(f"- `{difference.path}` {difference.kind}: {difference.note}")


@analyze_app.command("endpoint")
@clean_errors
def analyze_endpoint(
    name: str,
    endpoint: str,
    apply: Annotated[
        bool, typer.Option("--apply", help="Store observed schemas, content types, latency, style.")
    ] = False,
) -> None:
    """Statuses, field profiles, timestamp behavior, documented-vs-observed schema, latency."""
    fake = layout(name)
    model = load_model(fake)
    exchanges = RecordingStore(fake.recordings).for_endpoint(endpoint)
    if not exchanges:
        raise fail(f"no recordings for {endpoint!r}")
    by_status = _by_status(exchanges)
    echo(f"# {endpoint} — {len(exchanges)} recordings\n")
    _status_table(by_status)
    _field_report([e for code, group in by_status.items() if code in SUCCESS for e in group])
    found = model.find_endpoint(endpoint)
    if found is not None:
        _schema_report(found, by_status)
    timed = [e for e in exchanges if not e.stream]
    profile = LatencyProfile.from_samples(
        [e.elapsed_ms for e in timed if not _failed(e)],
        errors=[e.elapsed_ms for e in timed if _failed(e)],
    )
    errors = profile.error_samples_ms
    echo(
        f"\nLatency: p50 {profile.p50_ms:.0f} ms, p90 {profile.p90_ms:.0f} ms, "
        f"p99 {profile.p99_ms:.0f} ms"
        + (f"; errors ({len(errors)}): median {statistics.median(errors):.0f} ms" if errors else "")
    )
    if apply and found is not None:
        _apply(fake, model, found.id, by_status, profile)
    elif apply and (tool := _tool_of(model, endpoint)) is not None:
        tool.latency = profile  # an MCP tool: its timing is what --apply can store
        problems = save_model(fake, model)
        echo(
            "\nmodel updated" + ("" if not problems else " (problems: " + "; ".join(problems) + ")")
        )


def _failed(exchange: Exchange) -> bool:
    """An error answer: an HTTP error status, a JSON-RPC error or a tool result with isError."""
    response = exchange.response
    if response is None:
        return False
    if response.status >= 400:  # noqa: PLR2004 - HTTP errors
        return True
    message = response.body.json_value
    result = message.get("result") if isinstance(message, dict) else None
    return isinstance(message, dict) and (
        "error" in message or (isinstance(result, dict) and result.get("isError") is True)
    )


def _tool_of(model: ApiModel, endpoint: str) -> McpToolModel | None:
    prefix = "MCP tools/call:"
    if model.mcp is None or not endpoint.startswith(prefix):
        return None
    return model.mcp.tool(endpoint.removeprefix(prefix))


def _apply(
    fake: FakeLayout,
    model: ApiModel,
    endpoint_id: str,
    by_status: dict[int, list[Exchange]],
    profile: LatencyProfile,
) -> None:
    from fauxcall.analysis.schema import infer_schema  # noqa: PLC0415
    from fauxcall.model.endpoints import ResponseVariant  # noqa: PLC0415

    endpoint = model.endpoint(endpoint_id)
    variants = {variant.status: variant for variant in endpoint.responses}
    for status, group in by_status.items():
        response = group[0].response
        assert response is not None  # noqa: S101 - grouped by response status
        variant = variants.get(status) or ResponseVariant(status=status)
        bodies = _bodies(group)
        variant.observed_schema = infer_schema(bodies) if bodies else variant.observed_schema
        variant.content_type = response.header("content-type") or variant.content_type
        variant.examples = sorted({*variant.examples, *(e.id for e in group[:5])})
        variants[status] = variant
    endpoint.responses = [variants[s] for s in sorted(variants)]
    endpoint.latency = profile
    raw = [
        text
        for group in by_status.values()
        for e in group
        if e.response is not None
        and e.response.body.kind == "json"
        and (text := e.response.body.raw_text())
    ]
    if raw:
        model.json_style = infer_style(raw)
    problems = save_model(fake, model)
    echo("\nmodel updated" + ("" if not problems else " (problems: " + "; ".join(problems) + ")"))


@analyze_app.command("relations")
@clean_errors
def analyze_relations(
    name: str,
    world: Annotated[
        bool, typer.Option("--world", help="Analyze built collections instead of raw responses.")
    ] = False,
) -> None:
    """Candidate cross-endpoint references (value overlap), for the model's relations."""
    from fauxcall.analysis.relations import candidates_table, relation_candidates  # noqa: PLC0415

    fake = layout(name)
    groups: dict[str, list[JsonValue]] = {}
    if world:
        from fauxcall.world.io import load_collection  # noqa: PLC0415

        model = load_model(fake)
        for collection in model.collections:
            groups[collection.name] = [
                r.record for r in load_collection(fake.data / collection.path)
            ]
    else:
        grouped: defaultdict[str, list[JsonValue]] = defaultdict(list)
        for _, exchange in RecordingStore(fake.recordings).iter_exchanges():
            if exchange.response is not None and exchange.response.status in SUCCESS:
                body = exchange.response.body.parsed()
                if body is not None:
                    grouped[exchange.endpoint].append(body)
        groups = dict(grouped)
    echo(candidates_table(relation_candidates(groups)))


@analyze_app.command("jobs")
@clean_errors
def analyze_jobs(
    name: str,
    endpoint: Annotated[str, typer.Option(help="The status endpoint that was polled.")],
    state_path: Annotated[str, typer.Option(help="JSON path of the state, e.g. $.status")],
    terminal: Annotated[str, typer.Option(help="Comma-separated terminal states.")],
) -> None:
    """State durations and outcome rates from polling recordings."""
    from fauxcall.analysis.jobs import summarize, timelines  # noqa: PLC0415

    fake = layout(name)
    found = timelines(
        RecordingStore(fake.recordings).for_endpoint(endpoint),
        state_path=state_path,
        terminal=terminal.split(","),
    )
    for timeline in found:
        steps = " → ".join(
            f"{s.state} [{s.min_s:.1f}s..{'?' if s.max_s is None else f'{s.max_s:.1f}s'}]"
            for s in timeline.states
        )
        echo(f"- {timeline.job}: {steps} → {timeline.terminal}")
    summary = summarize(found)
    echo("\n" + summary.model_dump_json(indent=2))


@analyze_app.command("writes")
@clean_errors
def analyze_writes(
    name: str,
    write: Annotated[str, typer.Option(help="Exchange id of the write.")],
    readback: Annotated[str | None, typer.Option(help="Exchange id of the read-back.")] = None,
    id_field: Annotated[str, typer.Option()] = "id",
) -> None:
    """Does the write persist? What does the server add? How are ids assigned?"""
    from fauxcall.analysis.writes import compare_write, describe  # noqa: PLC0415

    fake = layout(name)
    exchanges = {
        exchange.id: exchange for _, exchange in RecordingStore(fake.recordings).iter_exchanges()
    }
    if write not in exchanges or (readback and readback not in exchanges):
        raise fail("unknown exchange id")
    evidence = compare_write(
        exchanges[write], exchanges.get(readback) if readback else None, id_field=id_field
    )
    echo(describe(evidence))
    echo(evidence.model_dump_json(indent=2))


@analyze_app.command("errors")
@clean_errors
def analyze_errors(
    name: str,
    template: Annotated[
        str | None, typer.Option(help="Create an error template with this name …")
    ] = None,
    source: Annotated[str | None, typer.Option("--from", help="… from this exchange id.")] = None,
) -> None:
    """List distinct error responses; optionally turn one into a named template in the model."""
    fake = layout(name)
    exchanges = [
        e
        for _, e in RecordingStore(fake.recordings).iter_exchanges()
        if e.response is not None and e.response.status >= ERROR_STATUS
    ]
    if template and source:
        _save_template(fake, exchanges, template, source)
        return
    seen: dict[tuple[str, int, str], str] = {}
    for exchange in exchanges:
        assert exchange.response is not None  # noqa: S101 - filtered above
        body = exchange.response.body
        text = jsonfmt.dumps(body.parsed()) if body.kind == "json" else (body.text or "")
        key = (exchange.endpoint, exchange.response.status, text[:120])
        seen.setdefault(key, exchange.id)
    echo("| endpoint | status | body (start) | example |\n|---|---|---|---|")
    for (endpoint, status, text), exchange_id in seen.items():
        echo(f"| `{endpoint}` | {status} | `{text.replace('|', '/')}` | {exchange_id} |")


def _save_template(fake: FakeLayout, exchanges: list[Exchange], template: str, source: str) -> None:
    chosen = next((e for e in exchanges if e.id == source), None)
    if chosen is None or chosen.response is None:
        raise fail(f"no error exchange {source!r}")
    response = chosen.response
    body = response.body
    model = load_model(fake)
    headers = {
        k: v
        for k, v in response.headers
        if k.lower() in {"retry-after", "www-authenticate", "allow"}
    }
    new = ErrorTemplate(
        name=template,
        status=response.status,
        content_type=response.header("content-type"),
        headers=headers,
        body_json=body.parsed() if body.kind == "json" else None,
        body_text=body.text if body.kind == "text" else None,
        examples=[chosen.id],
    )
    model.errors = [t for t in model.errors if t.name != template] + [new]
    save_model(fake, model)
    echo(
        f"template {template!r} saved — now generalize variable parts with "
        "{{placeholders}} in model.json"
    )


@analyze_app.command("style")
@clean_errors
def analyze_style(name: str) -> None:
    """Infer how the API spells JSON from every recorded body and store it in the model."""
    fake = layout(name)
    raw = [
        text
        for _, e in RecordingStore(fake.recordings).iter_exchanges()
        if e.response is not None
        and e.response.body.kind == "json"
        and (text := e.response.body.raw_text())
    ]
    model = load_model(fake)
    model.json_style = infer_style(raw)
    save_model(fake, model)
    echo(json.dumps(model.json_style.model_dump(), indent=2))
