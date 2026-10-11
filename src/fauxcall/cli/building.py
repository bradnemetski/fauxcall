"""CLI: build the world, generate the package, report."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from fauxcall.build.scaffold import scaffold as generate
from fauxcall.build.world import build_world, summary, write_world
from fauxcall.cli.common import (
    clean_errors,
    cli_settings,
    echo,
    fail,
    layout,
    load_model,
    load_progress,
    workspace,
)
from fauxcall.recording.store import RecordingStore
from fauxcall.storage import atomic_write_text
from fauxcall.world import World, check_world, expand_collection, resolve_relation_targets
from fauxcall.world.io import load_collection, save_collection

if TYPE_CHECKING:
    from fauxcall.model.api import ApiModel
    from fauxcall.model.recording import Exchange
    from fauxcall.paths import FakeLayout

world_app = typer.Typer(help="The consistent dataset the fake serves (data/world).")


def _exchanges(fake: FakeLayout) -> list[Exchange]:
    return [exchange for _, exchange in RecordingStore(fake.recordings).iter_exchanges()]


@world_app.command("build")
@clean_errors
def world_build(name: str) -> None:
    """Collect every collection's records from recordings, then check consistency."""
    fake = layout(name)
    model = load_model(fake)
    if not model.collections:
        raise fail("the model has no collections yet")
    builds = build_world(model, _exchanges(fake))
    write_world(fake.data, model, builds)
    echo(summary(builds))
    _report_problems(fake)


def _report_problems(fake: FakeLayout) -> None:
    model = load_model(fake)
    problems = check_world(
        model, World(model, fake.data).session(), import_paths=[fake.root / "src"]
    )
    if problems:
        echo("\nConsistency problems (record the missing entities, or fix relations):")
        echo("\n".join(f"- {problem}" for problem in problems[:100]))
        raise typer.Exit(1)
    echo("\nworld is consistent")


@world_app.command("check")
@clean_errors
def world_check(name: str) -> None:
    """Check relations and invariants of the built world."""
    _report_problems(layout(name))


@world_app.command("expand")
@clean_errors
def world_expand(name: str, collection: str) -> None:
    """Grow a collection to its expansion target (generated, invariant-checked, frozen)."""
    fake = layout(name)
    model = load_model(fake)
    spec = model.collection(collection)
    if spec.expansion is None:
        raise fail(f"collection {collection!r} has no 'expansion' settings in the model")
    path = fake.data / spec.path
    records = [r for r in load_collection(path) if not r.synthetic]
    targets = resolve_relation_targets(model, World(model, fake.data).session())
    references = {
        r.from_field: targets.get(r.name, [])
        for r in model.relations
        if r.from_collection == collection
    }
    expanded = expand_collection(spec, records, spec.expansion, reference_values=references)
    save_collection(path, expanded)
    echo(f"{collection}: {len(records)} recorded + {len(expanded) - len(records)} generated")
    _report_problems(fake)


@clean_errors
def scaffold(
    name: str,
    project_root: Annotated[
        Path | None, typer.Option(help="Where .github/workflows goes (default: cwd).")
    ] = None,
) -> None:
    """Generate (or refresh) the fake's package, tests, Docker and CI files."""
    fake = layout(name)
    model = load_model(fake)
    root = project_root or Path.cwd()
    result = generate(
        fake,
        workspace(),
        model,
        exchanges=_exchanges(fake),
        project_root=root,
        source=cli_settings().source,
    )
    echo(result.summary(root))
    echo(f"\nNext: `cd {fake.root} && uv sync && uv run pytest`")


def _coverage_lines(model: ApiModel, exchanges: int) -> list[str]:
    roles = Counter(endpoint.role for endpoint in model.endpoints)
    policies = Counter(policy.kind for endpoint in model.endpoints for policy in endpoint.fields)
    lines = [
        "## Coverage",
        "",
        (
            f"- endpoints: {len(model.endpoints)} "
            f"({', '.join(f'{k} {v}' for k, v in roles.items()) or 'n/a'})"
        ),
    ]
    if model.mcp is not None:
        lines.append(
            f"- MCP tools: {len(model.mcp.tools)} · resources: {len(model.mcp.resources)} · "
            f"prompts: {len(model.mcp.prompts)}"
        )
    collections = ", ".join(c.name for c in model.collections) or "none"
    lines += [
        f"- recordings: {exchanges} exchanges",
        f"- collections: {collections}; relations: {len(model.relations)}",
        f"- field policies: {', '.join(f'{k} {v}' for k, v in policies.items()) or 'none'}",
        (
            f"- jobs: {len(model.jobs)} · calendars: {len(model.calendars)} · "
            f"rate limits: {len(model.rate_limits)} · error templates: {len(model.errors)}"
        ),
    ]
    return lines


@clean_errors
def report(name: str) -> None:
    """Write data/reports/summary.md: what the fake covers and how faithful it is."""
    fake = layout(name)
    model = load_model(fake)
    progress = load_progress(fake)
    lines = [
        f"# {model.title} — FauxCall fake `{fake.name}`",
        "",
        (
            f"Source: {model.source.url} ({model.source.kind}) · "
            f"real API: {model.real.base_url}{model.real.base_path}"
        ),
        "",
        *_coverage_lines(model, len(_exchanges(fake))),
        "",
        "## Parity",
        "",
    ]
    parity = fake.reports / "parity.json"
    if parity.exists():
        data = json.loads(parity.read_text())
        verdict = "PASS" if data["ok"] else "FAIL"
        lines.append(
            f"Compliance **{data['score']:.2%}** (threshold {data['threshold']:.0%}) — {verdict}."
        )
    else:
        lines.append("No parity run yet (`uv run pytest tests/e2e --target=parity`).")
    problems = model.problems()
    lines += ["", "## Open items", ""]
    lines += [f"- model: {p}" for p in problems] or ["- none in the model"]
    lines += [
        f"- {entry.endpoint}: {'; '.join(entry.notes)}"
        for entry in progress.endpoints
        if entry.notes
    ]
    text = "\n".join(lines) + "\n"
    atomic_write_text(fake.reports / "summary.md", text)
    echo(text)
