"""The ``fauxcall`` command-line tool — the engine behind the ``/fauxcall`` skills.

Every step of building a fake is a command, so any agent (or person) can drive the process::

    fauxcall init <url>              # discover + snapshot + model skeleton + checkpoints
    fauxcall status <name>           # where the build stands / resume point
    fauxcall plan … / record …       # evidence from the real API (writes need the approval)
    fauxcall analyze …               # turn evidence into model decisions
    fauxcall world build <name>      # the consistent dataset
    fauxcall scaffold <name>         # the fake's package, tests, Docker, CI
    fauxcall verify <name> --target parity   # compliance against the real API
    fauxcall up                      # run fakes for your agents
"""

from __future__ import annotations

import typer

from fauxcall import __version__
from fauxcall.cli import building, evidence, project, running
from fauxcall.cli.common import clean_errors, cli_settings
from fauxcall.logs import configure_logging

app = typer.Typer(
    name="fauxcall",
    help=(
        "Record real APIs and MCP servers; serve high-fidelity fakes for repeatable agent testing."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
@clean_errors
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version."
    ),
) -> None:
    del version
    settings = cli_settings()
    configure_logging(settings.log_level, settings.log_format)


app.command()(project.init)
app.command()(project.status)
app.command()(project.doctor)
app.command()(project.endpoints)
app.add_typer(project.progress_app, name="progress")
app.add_typer(project.model_app, name="model")
app.add_typer(project.spec_app, name="spec")
app.add_typer(project.env_app, name="env")
app.add_typer(project.docs_app, name="docs")
app.add_typer(project.mcp_app, name="mcp")
app.add_typer(evidence.plan_app, name="plan")
app.command()(evidence.record)
app.command()(evidence.sql)
app.command()(evidence.recordings)
app.command("recordings-delete")(evidence.recordings_delete)
app.add_typer(evidence.analyze_app, name="analyze")
app.add_typer(building.world_app, name="world")
app.command()(building.scaffold)
app.command()(building.report)
app.command()(running.serve)
app.command()(running.up)
app.command()(running.down)
app.command()(running.ps)
app.command()(running.verify)


def main() -> None:
    """Console-script entry point."""
    app()
