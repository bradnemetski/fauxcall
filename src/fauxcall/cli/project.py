"""CLI: create fakes and track their build (init, status, progress, endpoints, spec, env …)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path  # noqa: TC003 - Typer resolves the annotations at runtime
from typing import TYPE_CHECKING, Annotated, Any, Final, cast, get_args

import typer

from fauxcall import __version__
from fauxcall.build.init import ensure_env, init_fake
from fauxcall.cli import common
from fauxcall.cli.common import (
    clean_errors,
    cli_settings,
    echo,
    fail,
    fake_settings,
    layout,
    load_model,
    load_progress,
    now,
    run,
    save_model,
    save_progress,
    secret_values,
    workspace,
)
from fauxcall.model.api import ApiModel
from fauxcall.model.progress import ENDPOINT_STEPS, PHASES, GateAnswers, PhaseState, Status
from fauxcall.privacy import Pseudonymizer
from fauxcall.recording.mcp import McpRecorder
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.recording.store import RecordingStore
from fauxcall.spec import OpenApiDocument, endpoints_from_spec, parse_spec_text, snapshot_docs
from fauxcall.storage import atomic_write_text, read_model

if TYPE_CHECKING:
    from fauxcall.model.progress import EndpointStep, Phase

STATUSES: Final[tuple[str, ...]] = get_args(Status)
CREDENTIALS: Final[tuple[str, ...]] = ("none_needed", "provided", "missing")
MUTATIONS: Final[tuple[str, ...]] = ("approved", "declined", "none_planned")


def _one_of(kind: str, value: str, allowed: tuple[str, ...]) -> str:
    """``value`` if it is allowed, else a clean CLI failure listing the choices."""
    if value not in allowed:
        raise fail(f"unknown {kind} {value!r}; one of: {', '.join(allowed)}")
    return value


# -- init / status --------------------------------------------------------------------------------


def _prepared_headers(name: str | None) -> dict[str, str]:
    """Credentials the user already put in the fake's ``.env``, for connecting during init.

    Recording and testing read the same file, so the user writes the key once, before the build.
    """
    if not name:
        return {}
    fake = workspace().fake(name)
    if not fake.env_file.exists():
        return {}
    real = fake_settings(fake).real
    return {key: value.get_secret_value() for key, value in real.headers.items()}


@clean_errors
def init(
    target: Annotated[
        str, typer.Argument(help="OpenAPI/Swagger URL or file, docs page, or MCP server URL.")
    ],
    name: Annotated[str | None, typer.Option(help="Name of the fake (kebab-case).")] = None,
    base_url: Annotated[
        str | None, typer.Option(help="Real API base URL when the spec lacks one.")
    ] = None,
    mcp: Annotated[
        bool, typer.Option("--mcp", help="Treat the target as an MCP server URL.")
    ] = False,
    auth_env: Annotated[
        str | None,
        typer.Option(
            help="MCP only: env var holding a bearer token. Usually not needed: with --name, the "
            "headers in the fake's prepared .env (FAUXCALL_REAL__HEADERS) are used."
        ),
    ] = None,
) -> None:
    """Identify the input, snapshot it and create the fake's model and checkpoints."""
    headers = _prepared_headers(name)
    if auth_env:
        token = os.environ.get(auth_env)
        if not token:
            raise fail(f"environment variable {auth_env} is not set")
        headers["authorization"] = token if " " in token else f"Bearer {token}"

    settings = cli_settings()

    async def go() -> None:
        async with common.http_client() as http:
            result = await init_fake(
                target,
                workspace=workspace(),
                http=http,
                name=name,
                base_url=base_url,
                force_mcp=mcp,
                mcp_headers=headers,
                rps=settings.record_rps,
                max_calls=settings.record_max_calls,
            )
        echo(f"# Created fake `{result.layout.name}` in {result.layout.root}\n")
        echo(result.summary)
        for note in result.notes:
            echo(f"- note: {note}")
        echo(
            f"\nCredentials (if the real API needs any) go in {result.layout.env_file} "
            "(gitignored) — see .env.example after `fauxcall scaffold`."
        )
        echo(f"Next: `fauxcall status {result.layout.name}`")

    run(go())


@clean_errors
def status(
    name: Annotated[str | None, typer.Argument(help="Fake name (omit to list all).")] = None,
) -> None:
    """Where each fake's build stands and what to do next (resume point)."""
    if name is None:
        fakes = workspace().fakes()
        if not fakes:
            echo(f"No fakes yet in {workspace().root}. Start with `fauxcall init <url>`.")
            return
        echo("| fake | source | next |\n|---|---|---|")
        for fake in fakes:
            progress = load_progress(fake)
            echo(f"| {fake.name} | {progress.source} | {progress.next_action()} |")
        return
    fake = layout(name)
    progress = load_progress(fake)
    echo(f"# {fake.name} — next: **{progress.next_action()}**\n")
    echo("| phase | status | notes |\n|---|---|---|")
    for phase in PHASES:
        state = progress.phases.get(phase, PhaseState())
        echo(f"| {phase} | {state.status} | {'; '.join(state.notes[-2:])} |")
    if progress.endpoints:
        echo("\n| endpoint | " + " | ".join(ENDPOINT_STEPS) + " |")
        echo("|---|" + "---|" * len(ENDPOINT_STEPS))
        for entry in progress.endpoints:
            cells = " | ".join(entry.steps.get(step, "pending") for step in ENDPOINT_STEPS)
            echo(f"| `{entry.endpoint}` | {cells} |")
    if progress.gate:
        gate = progress.gate
        echo(
            f"\nGate: credentials={gate.credentials}, jev_key={gate.jev_key}, "
            f"mutations={gate.mutations}"
        )
    if progress.log:
        echo("\nRecent notes:\n" + "\n".join(f"- {line}" for line in progress.log[-5:]))


# -- progress -------------------------------------------------------------------------------------

progress_app = typer.Typer(
    help="Update build checkpoints (so an interrupted build resumes exactly)."
)


@progress_app.command("phase")
@clean_errors
def progress_phase(
    name: str,
    phase: Annotated[str, typer.Argument(help=f"One of: {', '.join(PHASES)}")],
    state: Annotated[str, typer.Argument(help=f"One of: {' | '.join(STATUSES)}")],
    note: Annotated[str | None, typer.Option(help="Why (shown by `status`).")] = None,
) -> None:
    """Set a phase's status."""
    key = cast("Phase", _one_of("phase", phase, PHASES))
    _one_of("state", state, STATUSES)
    fake = layout(name)
    progress = load_progress(fake)
    current = progress.phases.get(key, PhaseState())
    update: dict[str, object] = {"status": state}
    if state == "in_progress" and current.started_at is None:
        update["started_at"] = now()
    if state in {"done", "skipped"}:
        update["finished_at"] = now()
    if note:
        update["notes"] = [*current.notes, note]
    progress.phases[key] = PhaseState.model_validate({**current.model_dump(), **update})
    save_progress(fake, progress)
    echo(f"{phase}: {state}")


@progress_app.command("endpoint")
@clean_errors
def progress_endpoint(
    name: str,
    endpoint: Annotated[str, typer.Argument(help="Endpoint id (or 'all').")],
    step: Annotated[str, typer.Argument(help=f"One of: {', '.join(ENDPOINT_STEPS)}")],
    state: Annotated[str, typer.Argument(help=f"One of: {' | '.join(STATUSES)}")],
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Set one endpoint loop step."""
    key = cast("EndpointStep", _one_of("step", step, ENDPOINT_STEPS))
    value = cast("Status", _one_of("state", state, STATUSES))
    fake = layout(name)
    progress = load_progress(fake)
    targets = progress.endpoints if endpoint == "all" else [progress.endpoint(endpoint)]
    for entry in targets:
        entry.steps[key] = value
        if note:
            entry.notes.append(note)
    save_progress(fake, progress)
    echo(f"{endpoint}: {step} = {state}")


@progress_app.command("gate")
@clean_errors
def progress_gate(
    name: str,
    credentials: Annotated[str, typer.Option(help=" | ".join(CREDENTIALS))] = "none_needed",
    jev: Annotated[
        bool, typer.Option("--jev/--no-jev", help="Whether the user has a Jev key.")
    ] = False,
    mutations: Annotated[str, typer.Option(help=" | ".join(MUTATIONS))] = "none_planned",
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Record the answers from the single up-front gate."""
    _one_of("credentials answer", credentials, CREDENTIALS)
    _one_of("mutations answer", mutations, MUTATIONS)
    fake = layout(name)
    progress = load_progress(fake)
    progress.gate = GateAnswers.model_validate(
        {
            "credentials": credentials,
            "jev_key": jev,
            "mutations": mutations,
            "notes": [note] if note else [],
            "answered_at": now(),
        }
    )
    progress.phases["gate"] = PhaseState(status="done", finished_at=now())
    save_progress(fake, progress)
    echo("gate recorded")


@progress_app.command("note")
@clean_errors
def progress_note(name: str, text: str) -> None:
    """Append a short note to the build log."""
    fake = layout(name)
    progress = load_progress(fake)
    progress.log.append(f"{now():%Y-%m-%d %H:%M} {text}")
    save_progress(fake, progress)


# -- endpoints / model ----------------------------------------------------------------------------


@clean_errors
def endpoints(
    name: str,
    show: Annotated[str | None, typer.Option(help="Show one endpoint's full model.")] = None,
) -> None:
    """List endpoints (role, collection, recordings) or show one in full."""
    fake = layout(name)
    model = load_model(fake)
    store = RecordingStore(fake.recordings)
    if show:
        endpoint = model.find_endpoint(show)
        if endpoint is None:
            raise fail(f"unknown endpoint {show!r}")
        echo(endpoint.model_dump_json(indent=2, exclude_defaults=True, by_alias=True))
        echo(f"\nrecordings: {len(store.for_endpoint(show))}")
        return
    echo("| endpoint | role | collection | recordings | summary |\n|---|---|---|---|---|")
    for endpoint in model.endpoints:
        count = len(store.for_endpoint(endpoint.id))
        echo(
            f"| `{endpoint.id}` | {endpoint.role} | {endpoint.collection or ''} | {count} | "
            f"{endpoint.summary or ''} |"
        )
    if model.mcp is not None:
        echo("\n| tool | behavior | recordings |\n|---|---|---|")
        for tool in model.mcp.tools:
            count = len(store.for_endpoint(f"MCP tools/call:{tool.name}"))
            echo(f"| `{tool.name}` | {tool.behavior} | {count} |")


model_app = typer.Typer(help="Validate and inspect data/model.json (edit the file directly).")


@model_app.command("check")
@clean_errors
def model_check(name: str) -> None:
    """Validate the model: schema and cross-references."""
    fake = layout(name)
    try:
        model = load_model(fake)
    except ValueError as error:
        raise fail(f"model.json is invalid:\n{error}") from error
    problems = model.problems()
    if problems:
        echo("Problems:\n" + "\n".join(f"- {p}" for p in problems))
        raise typer.Exit(1)
    echo(f"model OK: {len(model.endpoints)} endpoints, {len(model.collections)} collections")


@model_app.command("schema")
@clean_errors
def model_schema(
    part: Annotated[
        str | None, typer.Option(help="Only one definition, e.g. EndpointModel.")
    ] = None,
    brief: Annotated[
        bool,
        typer.Option("--brief", help="Readable summary: every part's fields, types and defaults."),
    ] = False,
) -> None:
    """Print the JSON Schema of data/model.json (with field descriptions)."""
    schema = ApiModel.model_json_schema()
    definitions: dict[str, Any] = schema.get("$defs", {})
    if part and part not in {*definitions, "ApiModel"}:
        raise fail(f"unknown part {part!r}; one of: {', '.join(sorted(definitions))}")
    if brief:
        parts = {"ApiModel": schema, **definitions}
        chosen = {part: parts[part]} if part else parts
        echo("\n\n".join(_brief_part(name, value) for name, value in chosen.items()))
        return
    if part and part != "ApiModel":
        schema = definitions[part]
    echo(json.dumps(schema, indent=2))


def _type_name(spec: dict[str, Any]) -> str:
    """A JSON-schema property type, briefly: ``str``, ``list[EndpointModel]``, ``a | b``."""
    if "$ref" in spec:
        return str(spec["$ref"]).rsplit("/", 1)[-1]
    if "enum" in spec:
        return " | ".join(json.dumps(value) for value in spec["enum"])
    if "const" in spec:
        return json.dumps(spec["const"])
    for combined in ("anyOf", "oneOf"):
        if combined in spec:
            return " | ".join(_type_name(option) for option in spec[combined])
    kind = spec.get("type")
    if kind == "array":
        return f"list[{_type_name(spec.get('items', {}))}]"
    if kind == "object" and isinstance(spec.get("additionalProperties"), dict):
        return f"dict[str, {_type_name(spec['additionalProperties'])}]"
    names = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
    return names.get(str(kind), str(kind) if kind else "any")


def _brief_part(name: str, spec: dict[str, Any]) -> str:
    """One model as text: its description, then a line per field."""
    lines = [f"## {name}"]
    description = str(spec.get("description", "")).strip()
    if description:
        lines.append(description.splitlines()[0])
    if "enum" in spec:
        lines.append(f"one of: {_type_name(spec)}")
    required = set(spec.get("required", []))
    for field_name, field_spec in spec.get("properties", {}).items():
        text = f"- {field_name}: {_type_name(field_spec)}"
        if field_name in required:
            text += " (required)"
        elif "default" in field_spec:
            text += f" = {json.dumps(field_spec['default'])}"
        hint = str(field_spec.get("description", "")).strip()
        if hint:
            text += f" — {hint.splitlines()[0]}"
        lines.append(text)
    return "\n".join(lines)


@model_app.command("show")
@clean_errors
def model_show(name: str) -> None:
    """Print the model."""
    echo(layout(name).model_path.read_text())


# -- spec import (for html_docs inputs) -----------------------------------------------------------

spec_app = typer.Typer(help="Work with the API description.")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


@spec_app.command("import")
@clean_errors
def spec_import(
    name: str,
    file: Annotated[
        Path | None, typer.Argument(help="Defaults to data/source/openapi.json.")
    ] = None,
) -> None:
    """Add endpoints from an OpenAPI document you wrote, keeping existing ones.

    For free-form docs, or for endpoints only the docs' prose mentions (e.g. a WebSocket feed).
    """
    fake = layout(name)
    model = load_model(fake)
    source = file or fake.source_dir / "openapi.json"
    spec = parse_spec_text(source.read_text(), source=str(source))
    doc = OpenApiDocument(spec, spec_url=None)
    known = {endpoint.id for endpoint in model.endpoints}
    base = model.real.base_path
    added = []
    for found in endpoints_from_spec(doc):
        endpoint = found
        if base and not _under(endpoint.path, base):
            endpoint = endpoint.model_copy(
                update={
                    "id": f"{endpoint.method.value} {base}{endpoint.path}",
                    "path": base + endpoint.path,
                }
            )
        if endpoint.id not in known:
            known.add(endpoint.id)
            added.append(endpoint)
    model.endpoints.extend(added)
    data = fake.data.resolve()
    resolved = source.resolve()
    if resolved.is_relative_to(data):
        model.source.spec_file = resolved.relative_to(data).as_posix()
    save_model(fake, model)
    progress = load_progress(fake)
    for endpoint in added:
        progress.endpoint(endpoint.id)
    save_progress(fake, progress)
    echo(f"added {len(added)} endpoint{'' if len(added) == 1 else 's'}")


# -- env / docs / mcp -----------------------------------------------------------------------------

env_app = typer.Typer(help="The fake's .env (credentials, keys) — values are never printed.")


@env_app.command("check")
@clean_errors
def env_check(name: str) -> None:
    """Which settings are present (without showing values)."""
    fake = layout(name)
    settings = fake_settings(fake)
    rows = [
        ("real base URL", settings.real.base_url or "(model default)"),
        ("real headers", ", ".join(settings.real.headers) or "-"),
        ("real query params", ", ".join(settings.real.query) or "-"),
        ("pseudonym key", "set" if settings.pseudonym_key else "MISSING"),
        ("Jev key (TYPESAFE_API_KEY)", "set" if settings.typesafe_api_key else "not set"),
    ]
    for label, value in rows:
        echo(f"- {label}: {value}")


@env_app.command("key")
@clean_errors
def env_key(name: str) -> None:
    """Ensure the fake has a pseudonymization key."""
    fake = layout(name)
    if not fake.env_file.exists():
        ensure_env(fake, load_model(fake).real.base_url)
        echo("created .env with a new key")
        return
    if fake_settings(fake).pseudonym_key is not None:
        echo("key already present")
        return
    with fake.env_file.open("a", encoding="utf-8") as handle:
        handle.write(f"\nFAUXCALL_PSEUDONYM_KEY={Pseudonymizer.generate_key()}\n")
    echo("key added")


docs_app = typer.Typer(help="Documentation helpers (for agents without a web fetch tool).")


@docs_app.command("fetch")
@clean_errors
def docs_fetch(
    url: str, out: Annotated[Path | None, typer.Option(help="Write to a file.")] = None
) -> None:
    """Fetch a docs page as readable text and list its links."""

    async def go() -> str:
        async with common.http_client() as http:
            snapshot = await snapshot_docs(url, http)
        return (
            snapshot.text + "\n\n## Links\n\n" + "\n".join(f"- {link}" for link in snapshot.links)
        )

    text = run(go())
    if out:
        atomic_write_text(out, text)
        echo(f"wrote {out}")
    else:
        echo(text)


mcp_app = typer.Typer(help="MCP-specific commands.")


@mcp_app.command("introspect")
@clean_errors
def mcp_introspect(name: str) -> None:
    """Re-run MCP introspection (e.g. after adding credentials), keeping modeled tool behavior."""
    fake = layout(name)
    model = load_model(fake)
    settings = fake_settings(fake)
    cli = cli_settings()
    headers = {k: v.get_secret_value() for k, v in settings.real.headers.items()}
    config_path = fake.data / "privacy.json"
    privacy = RecordingPrivacy(
        read_model(config_path, PrivacyConfig) if config_path.exists() else PrivacyConfig(),
        secret_values=secret_values(settings),
        pseudonym_key=settings.pseudonym_key.get_secret_value() if settings.pseudonym_key else None,
    )

    async def go() -> None:
        async with common.http_client() as http:
            recorder = McpRecorder(
                RecordingStore(fake.recordings),
                model.source.spec_url or model.source.url,  # the endpoint init resolved
                http,
                headers=headers,
                privacy=privacy,
                rps=cli.record_rps,
                max_calls=cli.record_max_calls,
            )
            introspection = await recorder.introspect()
        previous = {tool.name: tool for tool in (model.mcp.tools if model.mcp else [])}
        for tool in introspection.model.tools:
            old = previous.get(tool.name)
            if old is not None:
                tool.behavior, tool.computed, tool.semantic, tool.latency = (
                    old.behavior,
                    old.computed,
                    old.semantic,
                    old.latency,
                )
        model.mcp = introspection.model
        model.kind = "mcp"
        save_model(fake, model)
        progress = load_progress(fake)
        for tool in introspection.model.tools:
            progress.endpoint(f"MCP tools/call:{tool.name}")
        save_progress(fake, progress)
        echo(f"{len(introspection.model.tools)} tools; " + "; ".join(introspection.notes))

    run(go())


# -- doctor ---------------------------------------------------------------------------------------


@clean_errors
def doctor() -> None:
    """Check the environment FauxCall needs."""
    settings = cli_settings()
    uv = shutil.which("uv")
    checks = [
        ("fauxcall", True, __version__),
        ("python >= 3.12", sys.version_info >= (3, 12), sys.version.split()[0]),
        ("uv on PATH", uv is not None, uv or "install: https://docs.astral.sh/uv/"),
        ("workspace", True, f"{settings.workspace} ({len(workspace().fakes())} fakes)"),
        (
            "Jev key (optional)",
            True,
            "set" if settings.typesafe_api_key else "not set — runtime judgments use fallbacks",
        ),
        (
            "docker (optional)",
            True,
            "available" if shutil.which("docker") else "not found — compose files still generated",
        ),
        ("FAUXCALL_SOURCE", True, settings.source or f"fauxcall=={__version__} from PyPI"),
    ]
    healthy = all(ok for _, ok, _ in checks)
    for label, ok, detail in checks:
        echo(f"{'✓' if ok else '✗'} {label}: {detail}")
    if not healthy:
        raise typer.Exit(1)
