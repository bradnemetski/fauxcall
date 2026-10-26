"""CLI: plans, the up-front approval, recording, and analysis of the evidence."""

from __future__ import annotations

import fnmatch
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path  # noqa: TC003 - Typer resolves the annotations at runtime
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlencode

import jsonschema
import typer

from fauxcall import jsonfmt
from fauxcall.analysis.access import access_report
from fauxcall.cli import common
from fauxcall.cli.common import (
    EXIT_ERROR,
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
from fauxcall.model.plan import (
    Approval,
    Approvals,
    HeadlessAnswers,
    PlannedCall,
    PlannedRequest,
    RecordingPlan,
)
from fauxcall.model.realism import LatencyProfile
from fauxcall.recording.mcp import McpRecorder, answer_of, needs_approval
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.recording.recorder import RealTarget, Recorder, RunReport
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import read_model, write_model

if TYPE_CHECKING:
    from collections.abc import Callable

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


ANSWERS: Final = "answers.json"
"""A headless run's answers, written by the user before the run (``references/headless.md``)."""


def apply_answers(fake: FakeLayout) -> list[str]:
    """Apply the write approval in the fake's ``answers.json``; returns the endpoints added.

    In a headless run the answers file *is* the user's approval, so the CLI applies it itself
    (every command that can write calls this) and the agent never approves anything. Each
    matching mutating endpoint gets its own budget (``max_calls_per_endpoint``), for recording
    and parity alike. Endpoints it already approved are left alone.
    """
    path = fake.root / ANSWERS
    if not path.is_file():
        return []
    try:
        answers = read_model(path, HeadlessAnswers)
    except ValueError as error:
        raise fail(f"{path} is not a valid answers file: {error}") from error
    mutations = answers.mutations
    if mutations is None or not mutations.approve:
        return []
    approvals = _approvals(fake)
    applied = {a.endpoint for a in approvals.approvals if a.approved_by == ANSWERS}
    model = load_model(fake)
    budget = mutations.max_calls_per_endpoint or cli_settings().approval_budget
    targets = [t for glob in mutations.endpoints for t in _mutating_matches(model, glob)]
    added = [
        Approval(
            scope="endpoint",
            endpoint=target,
            max_calls=budget,
            reason=mutations.reason,
            approved_by=ANSWERS,
            approved_at=now(),
        )
        for target in dict.fromkeys(targets)
        if target not in applied
    ]
    if added:
        approvals.approvals.extend(added)
        write_model(fake.approvals_path, approvals)
    return [approval.endpoint for approval in added]


def _applied(fake: FakeLayout) -> None:
    """Apply ``answers.json`` and say so when it approved something new."""
    added = apply_answers(fake)
    if added:
        echo(f"applied the write approval in {ANSWERS}: {', '.join(added)}")


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


def _example_value(schema: JsonValue) -> JsonValue:
    """A typed placeholder for a JSON Schema: its example, default or first enum value if any."""
    if not isinstance(schema, dict):
        return "value"
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    placeholders: dict[str, JsonValue] = {
        "integer": 1,
        "number": 1.5,
        "boolean": True,
        "array": [],
        "object": {},
        "null": None,
    }
    return placeholders.get(str(kind), "value")


def _mcp_template(model: ApiModel, endpoint: str, purpose: str) -> PlannedCall:
    """A plan entry for ``MCP tools/call:<tool>``, ``resources/read:<uri>`` or ``prompts/get:…``."""
    method, _, target = endpoint.removeprefix("MCP ").partition(":")
    path = model.mcp.transport.path if model.mcp else "/mcp"
    body: JsonValue
    if method == "tools/call":
        tool = model.mcp.tool(target) if model.mcp else None
        if tool is None:
            raise fail(f"unknown tool {target!r} (see: fauxcall endpoints {model.name})")
        definition = tool.definition if isinstance(tool.definition, dict) else {}
        schema = definition.get("inputSchema")
        schema = schema if isinstance(schema, dict) else {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        wanted = required if isinstance(required, list) and required else list(properties)
        arguments = {str(k): _example_value(properties.get(str(k))) for k in wanted}
        body = {"name": target, "arguments": arguments}
    elif method == "resources/read" and target:
        body = {"uri": target}
    elif method == "prompts/get" and target:
        body = {"name": target, "arguments": {}}
    else:
        body = {}
    slug = re.sub(r"[^a-z0-9]+", "-", f"{target or method}-{purpose}".lower()).strip("-")
    return PlannedCall.model_validate(
        {
            "id": slug,
            "endpoint": endpoint,
            "purpose": purpose,
            "request": {"method": "POST", "path": path, "json_body": body},
        }
    )


@plan_app.command("template")
@clean_errors
def plan_template(
    name: str,
    endpoint: str,
    purpose: Annotated[
        str, typer.Option(help="sample | variation | edge | invalid | time_profile | …")
    ] = "sample",
) -> None:
    """Print a starter plan entry for an endpoint or MCP tool (fill in real values first)."""
    model = load_model(layout(name))
    if endpoint.startswith("MCP "):
        echo(
            _mcp_template(model, endpoint, purpose).model_dump_json(indent=2, exclude_defaults=True)
        )
        return
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
    """Every write across all plans: exactly what the user approves, once, up front."""
    fake = layout(name)
    _applied(fake)
    approvals = _approvals(fake)
    model = load_model(fake)
    writes = _writes(model)
    outcomes = _recorded_outcomes(fake)
    per_endpoint: Counter[str] = Counter()
    rows: list[str] = []
    declared: list[str] = []
    new = 0
    for path, plan in _plans(fake):
        by_id = {call.id: call for call in plan.calls}
        for call in plan.calls:
            if not writes(call):
                if call.mutating is False and writes(call.model_copy(update={"mutating": None})):
                    why = call.note or "no reason given"
                    declared.append(f"- {path.name} `{call.id}`: `{call.endpoint}` ({why})")
                continue
            per_endpoint[call.endpoint] += call.repeat
            approved = _approval_state(approvals, call)
            new += approved == "**new**"
            rows.append(
                f"| {path.name} | {call.id} | `{call.endpoint}` | {call.repeat} | "
                f"{_cleanup_state(call, by_id, outcomes)} | {approved} | {call.note or ''} |"
            )
    if not rows and not declared:
        echo("No mutating calls planned.")
        return
    if not rows:
        echo("No mutating calls planned.")
    else:
        echo(
            "| plan | call | endpoint | times | cleaned up by | approved | why |\n"
            "|---|---|---|---|---|---|---|"
        )
        echo("\n".join(rows))
        echo("\nTotals per endpoint: " + ", ".join(f"{k} x{v}" for k, v in per_endpoint.items()))
        if new:
            echo(
                f"{new} write(s) are new or changed since the user approved: ask before recording."
            )
    if declared:
        echo(
            "\nDeclared read-only by the plans (`mutating: false`), so they run without approval; "
            "show the user these too:\n" + "\n".join(declared)
        )
    echo(
        "\nRecording runs exactly these writes. Parity runs may also write: default "
        f"{cli_settings().approval_budget} per endpoint (approval_budget, "
        "FAUXCALL_APPROVAL_BUDGET). Ask the user for that number."
    )


def _approval_state(approvals: Approvals, call: PlannedCall) -> str:
    if approvals.find(call) is not None:
        return "yes"
    if any(approval.covers(call) for approval in approvals.approvals):
        return "yes, all its calls made"
    return "**new**"


def _recorded_outcomes(fake: FakeLayout) -> dict[tuple[str, str], bool]:
    """``(plan call id, endpoint)`` -> whether its latest recording succeeded."""
    outcomes: dict[tuple[str, str], bool] = {}
    for _, exchange in RecordingStore(fake.recordings).iter_exchanges():
        if exchange.plan_call is not None:
            outcomes[exchange.plan_call, exchange.endpoint] = _succeeded(exchange)
    return outcomes


def _succeeded(exchange: Exchange) -> bool:
    """A response below 400 that isn't a JSON-RPC error or a failed tool result."""
    response = exchange.response
    if response is None or response.status >= ERROR_STATUS:
        return False
    body = response.body.parsed()
    if not isinstance(body, dict):
        return True
    result = body.get("result")
    return "error" not in body and not (isinstance(result, dict) and result.get("isError"))


def _cleanup_state(
    call: PlannedCall, by_id: dict[str, PlannedCall], outcomes: dict[tuple[str, str], bool]
) -> str:
    if not call.cleanup:
        return "**nothing: it stays on the real service**"
    notes = []
    for cleanup_id in call.cleanup:
        cleanup = by_id.get(cleanup_id)
        worked = outcomes.get((cleanup_id, cleanup.endpoint)) if cleanup else None
        notes.append(cleanup_id + (" (**failed when recorded**)" if worked is False else ""))
    return ", ".join(notes)


@plan_app.command("approve")
@clean_errors
def plan_approve(
    name: str,
    endpoint: Annotated[str | None, typer.Option(help="Endpoint id or glob to approve.")] = None,
    max_calls: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Real calls each approval allows (default: the approval_budget setting, 100). "
            "A glob shares them across every endpoint it matches, unless --per-endpoint. With "
            "--planned: the writes parity runs may make per endpoint.",
        ),
    ] = None,
    unlimited: Annotated[
        bool, typer.Option("--unlimited", help="No cap at all (only when the user asked for it).")
    ] = False,
    per_endpoint: Annotated[
        bool,
        typer.Option(
            "--per-endpoint",
            help="With a glob: one approval, with its own budget, per matching mutating endpoint.",
        ),
    ] = False,
    planned: Annotated[
        bool,
        typer.Option(
            "--planned",
            help="Approve exactly the reviewed writes in the current plans (each its `repeat` "
            "times, for recording; an edited or new write needs approving again), plus "
            "--max-calls writes per endpoint for parity runs.",
        ),
    ] = False,
    call_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--call",
            help="Approve this reviewed plan entry (by id) again, its `repeat` times more: "
            "to re-run a write the user already approved once.",
        ),
    ] = None,
    reason: Annotated[
        str | None, typer.Option(help="What the user approved, in their words.")
    ] = None,
) -> None:
    """Record the user's up-front approval (run only after the user said yes)."""
    fake = layout(name)
    approvals = _approvals(fake)
    if unlimited and max_calls is not None:
        raise fail("pass --max-calls or --unlimited, not both")
    budget = None if unlimited else (max_calls or cli_settings().approval_budget)
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
                max_calls=budget,
                reason=reason,
                approved_at=now(),
            )
            for target in targets
        ]
    if planned:
        writes = _writes(load_model(fake))
        calls = [call for _, plan in _plans(fake) for call in filter(writes, plan.calls)]
        reviewed = {
            call.fingerprint(): call
            for call in calls
            if not any(a.scope == "call" and a.covers(call) for a in approvals.approvals)
        }
        added += [
            Approval(
                scope="call",
                endpoint=call.endpoint,
                fingerprint=fingerprint,
                max_calls=call.repeat,
                use="recording",
                reason=reason,
                approved_at=now(),
            )
            for fingerprint, call in reviewed.items()
        ]
        added += [
            Approval(
                scope="endpoint",
                endpoint=target,
                max_calls=budget,
                use="parity",
                reason=reason,
                approved_at=now(),
            )
            for target in dict.fromkeys(call.endpoint for call in calls)
        ]
    for call_id in call_ids or []:
        writes = _writes(load_model(fake))
        found = [c for _, plan in _plans(fake) for c in plan.calls if c.id == call_id]
        if not found:
            raise fail(f"no plan entry with id {call_id!r} (see: fauxcall plan review {name})")
        added += [
            Approval(
                scope="call",
                endpoint=call.endpoint,
                fingerprint=call.fingerprint(),
                max_calls=call.repeat,
                use="recording",
                reason=reason,
                approved_at=now(),
            )
            for call in {c.fingerprint(): c for c in found if writes(c)}.values()
        ]
    if not added:
        raise fail("nothing to approve: pass --endpoint, --planned or --call")
    lines = [_on_top(approvals, approval) for approval in added]
    approvals.approvals.extend(added)
    write_model(fake.approvals_path, approvals)
    echo("\n".join(lines))


def _on_top(approvals: Approvals, approval: Approval) -> str:
    """Make a new budget add to what earlier approvals of the same thing allowed; describe it.

    Spent calls are counted per endpoint (or per reviewed call), and all approvals of it share
    the count: approving 30 more after 33 were spent must allow 63 in all, not 30.
    """
    what = (
        f"the reviewed call `{approval.endpoint}` ({approval.fingerprint}) for recording"
        if approval.scope == "call"
        else approval.endpoint + (" for parity runs" if approval.use == "parity" else "")
    )
    if approval.max_calls is None:
        return f"approved {what} (max unlimited)"
    key = Approvals.budget_key(approval)
    spent = approvals.used.get(key, 0)
    earlier = [a.max_calls for a in approvals.approvals if Approvals.budget_key(a) == key]
    if None in earlier:
        return f"approved {what}: already unlimited"
    allowed = max([spent, *(cap for cap in earlier if cap is not None)])
    if not allowed:
        return f"approved {what} (max {approval.max_calls})"
    more = approval.max_calls
    approval.max_calls = allowed + more
    return (
        f"approved {what}: {more} more calls ({approval.max_calls} in all; {spent} already spent)"
    )


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
    _applied(fake)
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
    writes = _writes(model)
    for call in parsed.calls:
        state = (
            "read-only"
            if not writes(call)
            else ("approved" if approvals.find(call) else "NEEDS APPROVAL (new or changed)")
        )
        probe = " (routing probe: not in the model)" if call.endpoint in undocumented else ""
        echo(f"- {call.id}: {call.endpoint} x{call.repeat} — {state}{probe}")
    if unknown:
        raise fail(f"unknown endpoints: {', '.join(sorted(set(unknown)))}")


def _writes(model: ApiModel) -> Callable[[PlannedCall], bool]:
    """Which planned calls need approval: the rule ``fauxcall record`` applies."""
    tools = {tool.name: tool.definition for tool in model.mcp.tools} if model.mcp else {}
    return lambda call: needs_approval(call, tools)


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
    _applied(fake)
    headers = {k: v.get_secret_value() for k, v in settings.real.headers.items()}
    query = {k: v.get_secret_value() for k, v in settings.real.query.items()}
    key = settings.pseudonym_key.get_secret_value() if settings.pseudonym_key else None
    privacy = _privacy(fake, secret_values(settings), key)
    approvals = _approvals(fake)
    store = RecordingStore(fake.recordings)

    async def go() -> RunReport:
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
        return report

    report = run(go())
    echo(report.summary())
    if report.failed:
        raise typer.Exit(EXIT_ERROR)


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


def _jsonl(exchange: Exchange) -> str:
    """An exchange as one JSON line: what was sent and what came back, bodies parsed."""
    response = exchange.response
    line: JsonValue = {
        "id": exchange.id,
        "endpoint": exchange.endpoint,
        "plan_call": exchange.plan_call,
        "purpose": exchange.purpose,
        "method": exchange.request.method.value,
        "path": exchange.request.path,
        "query": [list(pair) for pair in exchange.request.query],
        "request": exchange.request.body.parsed(),
        "status": response.status if response is not None else None,
        "elapsed_ms": exchange.elapsed_ms,
        "response": response.body.parsed() if response is not None else None,
        "error": exchange.error,
    }
    return jsonfmt.dumps(line)


def _mcp_row(exchange: Exchange) -> str:
    """An MCP call: method and target, arguments, how it answered, and the answer's text."""
    sent = exchange.request.body.parsed()
    params = sent.get("params") if isinstance(sent, dict) else None
    arguments = params.get("arguments") if isinstance(params, dict) else None
    response = exchange.response
    reply = response.body.parsed() if response is not None else None
    if response is None:
        outcome = "no response"
    else:
        outcome = f"{response.status} {answer_of(reply)} {exchange.elapsed_ms:.0f}ms"
    shown = f" {_excerpt(jsonfmt.dumps(arguments))}" if arguments else ""
    label = exchange.plan_call or "-"
    method = exchange.endpoint.removeprefix("MCP ")
    row = f"{exchange.id} {label} [{exchange.purpose}] {method}{shown} -> {outcome}"
    text = _excerpt(_reply_text(reply) or _preview(exchange))
    return f"{row}  {text}" if text else row


def _reply_text(reply: JsonValue) -> str | None:
    """The readable part of a JSON-RPC reply: an error's message, or a result's text blocks."""
    if not isinstance(reply, dict):
        return None
    error = reply.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    result = reply.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [str(p.get("text")) for p in content if isinstance(p, dict) and p.get("text")]
        if texts:
            return " ".join(texts)
    return None


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW else flat[: _PREVIEW - 1] + "…"


def _row(exchange: Exchange) -> str:
    if exchange.endpoint.startswith("MCP "):
        return _mcp_row(exchange)
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


def _show(store: RecordingStore, show: str, name: str) -> None:
    for path, exchange in store.iter_exchanges():
        if exchange.id == show or (show.isdigit() and exchange.id.lstrip("0") == show.lstrip("0")):
            echo(path.read_text(encoding="utf-8").rstrip())
            return
    raise fail(f"no recording {show!r} (list them with: fauxcall recordings {name})")


def _matches(
    exchange: Exchange,
    endpoint: str | None,
    purpose: str | None,
    status: int | None,
    grep: str | None,
) -> bool:
    response = exchange.response
    return not (
        (endpoint is not None and not fnmatch.fnmatchcase(exchange.endpoint, endpoint))
        or (purpose is not None and exchange.purpose != purpose)
        or (status is not None and (response is None or response.status != status))
        or (grep is not None and grep not in exchange.model_dump_json())
    )


@clean_errors
def recordings_delete(
    name: str,
    ids: Annotated[
        list[str] | None, typer.Option("--id", help="An exchange id (repeatable).")
    ] = None,
    endpoint: Annotated[
        str | None, typer.Option(help="Every exchange of this endpoint id or glob.")
    ] = None,
    purpose: Annotated[str | None, typer.Option(help="… with this purpose only.")] = None,
) -> None:
    """Delete recordings (a leak to re-record after fixing privacy.json, a bad probe …)."""
    if not ids and endpoint is None:
        raise fail("say what to delete: --id, or --endpoint (with --purpose)")
    store = RecordingStore(layout(name).recordings)
    wanted = {i.lstrip("0") for i in ids or []}
    deleted = []
    for path, exchange in list(store.iter_exchanges()):
        chosen = exchange.id.lstrip("0") in wanted or (
            endpoint is not None and _matches(exchange, endpoint, purpose, None, None)
        )
        if chosen:
            store.delete(path, exchange)
            deleted.append(exchange.id)
    echo(f"deleted {len(deleted)} recordings" + (f": {', '.join(deleted)}" if deleted else ""))


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
    jsonl: Annotated[
        bool,
        typer.Option(
            "--jsonl",
            help="One JSON object per matching exchange, in full and unlimited: for reading many "
            "with a script instead of writing one over data/recordings.",
        ),
    ] = False,
) -> None:
    """Browse recordings: one line per exchange (id, plan call, request, status, time, body).

    MCP calls show their tool and arguments, how they answered (ok, isError, error -32602) and
    the answer's text.
    """
    store = RecordingStore(layout(name).recordings)
    if show is not None:
        _show(store, show, name)
        return
    rows: list[str] = []
    for _, exchange in store.iter_exchanges():
        if not _matches(exchange, endpoint, purpose, status, grep):
            continue
        rows.append(_jsonl(exchange) if jsonl else _row(exchange))
    if jsonl:
        for row in rows:
            echo(row)
        return
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
    if success and success[0].endpoint.startswith("MCP "):
        # JSON-RPC answers: the result is what matters, not the envelope (jsonrpc, id)
        bodies = [b["result"] for b in bodies if isinstance(b, dict) and "result" in b]
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
    # A stream's elapsed time is its length, not a latency; MCP answers sent as SSE are no stream.
    timed = [e for e in exchanges if not e.stream or e.endpoint.startswith("MCP ")]
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
    tool = _tool_of(model, endpoint)
    learned = _invalid_args_mode(tool, exchanges) if tool is not None else None
    if learned is not None:
        mode, agreeing, probes = learned
        echo(f"Arguments that break its schema: answered as {mode} ({agreeing} of {probes} probes)")
    repeats = _repeated_answers(exchanges) if tool is not None else None
    varies = repeats is not None and repeats[0] * 2 > repeats[1]
    if repeats is not None:
        differing, repeated = repeats
        verdict = (
            "it varies (compared by shape only)"
            if varies
            else "mostly the same (a difference can be state that changed in between)"
            if differing
            else "it answers them the same"
        )
        echo(f"Same arguments, different answers: {differing} of {repeated} repeated: {verdict}")
    if apply and found is None and tool is not None:
        tool.latency = profile  # an MCP tool: its timing, argument checking and repeatability
        if learned is not None:
            tool.invalid_args = learned[0]
        if repeats is not None:
            tool.varies = varies
        problems = save_model(fake, model)
        echo(
            "\nmodel updated" + ("" if not problems else " (problems: " + "; ".join(problems) + ")")
        )


def _repeated_answers(exchanges: list[Exchange]) -> tuple[int, int] | None:
    """Of the argument sets answered more than once, how many got different answers.

    Only successful answers count, compared without ``_meta`` (per-session ids). ``None``: no
    arguments were answered twice.
    """
    calls: Counter[str] = Counter()
    answers: defaultdict[str, set[str]] = defaultdict(set)
    for exchange in exchanges:
        sent = exchange.request.body.parsed()
        params = sent.get("params") if isinstance(sent, dict) else None
        reply = exchange.response.body.parsed() if exchange.response is not None else None
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(params, dict) or not isinstance(result, dict) or result.get("isError"):
            continue
        key = jsonfmt.canonical_dumps(params.get("arguments"))
        calls[key] += 1
        answers[key].add(jsonfmt.canonical_dumps({k: v for k, v in result.items() if k != "_meta"}))
    repeated = [key for key, count in calls.items() if count > 1]
    if not repeated:
        return None
    return sum(len(answers[key]) > 1 for key in repeated), len(repeated)


InvalidArgs = Literal["jsonrpc_error", "tool_error", "accept"]


def _invalid_args_mode(
    tool: McpToolModel, exchanges: list[Exchange]
) -> tuple[InvalidArgs, int, int] | None:
    """How the server answered the recorded calls whose arguments break the tool's schema.

    A JSON-RPC error, a failed tool result (``isError``), or an answer as if they were fine.
    Only schema violations count: the fake checks the schema, and arguments that pass it but
    the server rejects are answered like any other recorded call. ``None``: none recorded.
    """
    definition = tool.definition if isinstance(tool.definition, dict) else {}
    schema = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return None
    votes: Counter[InvalidArgs] = Counter()
    for exchange in exchanges:
        sent = exchange.request.body.parsed()
        params = sent.get("params") if isinstance(sent, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else None
        if arguments is None or exchange.response is None:
            continue
        try:
            jsonschema.validate(jsonfmt.plain(arguments), schema)
        except jsonschema.ValidationError:
            pass
        except jsonschema.SchemaError:
            return None
        else:
            continue
        answer = answer_of(exchange.response.body.parsed())
        if answer.startswith("error"):
            votes["jsonrpc_error"] += 1
        elif answer == "isError":
            votes["tool_error"] += 1
        else:
            votes["accept"] += 1
    if not votes:
        return None
    ((mode, agreeing),) = votes.most_common(1)
    return mode, agreeing, sum(votes.values())


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


@analyze_app.command("access")
@clean_errors
def analyze_access(name: str) -> None:
    """What the credentials may do: endpoints and tools the real service refused for permission.

    Record one cheap read of each endpoint or tool first. Show the user what was refused before
    planning around it: the fake will refuse it too.
    """
    fake = layout(name)
    report = access_report(e for _, e in RecordingStore(fake.recordings).iter_exchanges())
    if not report:
        echo("Nothing recorded yet: record one read of each endpoint or tool first.")
        return
    refused = [entry for entry in report if entry.refused]
    fine = len(report) - len(refused)
    if not refused:
        echo(f"The credentials were refused nowhere ({fine} endpoints or tools called).")
        return
    echo("| endpoint | calls | refused | why | refused calls |\n|---|---|---|---|---|")
    for entry in refused:
        echo(
            f"| `{entry.endpoint}` | {entry.calls} | {entry.refused} | "
            f"{'; '.join(entry.reasons)} | {'; '.join(entry.examples) or '-'} |"
        )
    echo(f"\n{fine} other endpoints or tools were never refused.")
    blocked = [e.endpoint for e in refused if e.all_refused]
    partly = [e.endpoint for e in refused if not e.all_refused]
    if blocked:
        echo(f"\nRefused every time: {', '.join(blocked)}.")
    if partly:
        echo(f"Refused for some arguments: {', '.join(partly)}.")
    echo(
        "The fake will refuse the same. Tell the user before planning: keep these credentials, "
        "or use a test key with the permissions their agents need."
    )


@analyze_app.command("stream")
@clean_errors
def analyze_stream(
    name: str,
    apply: Annotated[
        bool, typer.Option("--apply", help="Set server_messages and message_member_order.")
    ] = False,
) -> None:
    """What the MCP server sends on its own, from the recordings of the client's GET stream.

    Plans with ``"listen": true`` record it. Parity expects a scenario to compare each kind
    (``await mcp.heard("notifications/message")``).
    """
    from fauxcall.analysis.stream import stream_report  # noqa: PLC0415

    fake = layout(name)
    model = load_model(fake)
    if model.mcp is None:
        raise fail(f"{name} is not an MCP fake")
    report = stream_report(e for _, e in RecordingStore(fake.recordings).iter_exchanges())
    if not report.counts:
        echo('Nothing recorded on the client\'s GET stream: record a plan with "listen": true.')
        return
    echo("| method | times | kind |\n|---|---|---|")
    for method in report.methods:
        kind = "request (the client answers)" if method in report.requests else "notification"
        echo(f"| `{method}` | {report.counts[method]} | {kind} |")
    echo(f"\nMember order: {', '.join(report.member_order)}")
    if not apply:
        echo("Run again with --apply to set server_messages and message_member_order.")
        return
    model.mcp.server_messages = sorted(report.counts)
    model.mcp.transport.message_member_order = report.member_order
    save_model(fake, model)
    echo("Applied: parity now expects a scenario to compare each of these.")


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
