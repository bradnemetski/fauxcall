"""``fauxcall init``: identify the input, snapshot it, create the skeleton and checkpoints."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from dotenv import dotenv_values

from fauxcall import jsonfmt
from fauxcall.errors import ConfigurationError, SpecError
from fauxcall.model.api import ApiModel, RealTarget, RoutingModel, SourceInfo
from fauxcall.model.mcp import FALLBACK_PROTOCOL_VERSION
from fauxcall.model.progress import PhaseState, Progress
from fauxcall.paths import FakeLayout, Workspace, slugify, validate_slug
from fauxcall.privacy import Pseudonymizer
from fauxcall.recording.mcp import McpRecorder
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.recording.store import RecordingStore
from fauxcall.spec import (
    OpenApiDocument,
    auth_from_spec,
    endpoints_from_spec,
    html_to_text,
    suggest_role,
    summarize,
)
from fauxcall.spec.discover import discover
from fauxcall.storage import atomic_write_text, write_json, write_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

    import httpx2

    from fauxcall.spec import DiscoveredSource
    from fauxcall.types import JsonValue

GITIGNORE_REQUIRED: Final = (".env", ".env.*", "!.env.example")
"""Workspace ``.gitignore`` lines that keep credentials out of git (ensured before any secret)."""

GITIGNORE_TEMPLATE: Final = Path(__file__).parent / "templates" / "workspace" / "gitignore.j2"
"""Static template shared with the scaffolder."""

_DESCRIPTION_PREVIEW: Final = 120
DEFAULT_RPS: Final = 2.0
"""Requests per second to a real MCP server during introspection (the recorder's default)."""
DEFAULT_MAX_CALLS: Final = 2000
"""Call budget of one introspection (the recorder's default)."""


@dataclass(slots=True)
class InitResult:
    """What ``init`` created."""

    layout: FakeLayout
    model: ApiModel
    summary: str
    notes: list[str] = field(default_factory=list)


def origin(url: str) -> str:
    """``scheme://host[:port]`` of a URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def header_secrets(values: Iterable[str]) -> list[str]:
    """Secret values to scrub for credential header values: each in full and without its scheme.

    ``"Bearer abc"`` yields ``"Bearer abc"`` and ``"abc"``, so neither spelling reaches disk.
    """
    found: list[str] = []
    for value in values:
        for candidate in (value, value.partition(" ")[2].strip()):
            if candidate and candidate not in found:
                found.append(candidate)
    return found


def _create_private(path: Path, text: str) -> bool:
    """Create ``path`` readable by its owner only (``0600`` from the start); never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    path.chmod(0o600)
    return True


def ensure_env(layout: FakeLayout, base_url: str) -> bool:
    """Create the fake's ``.env``: real base URL and a fresh pseudonym key (never overwrites).

    The file is created with ``0600`` permissions: it holds the pseudonym key and, later, the
    credentials for the real API. A ``.env`` prepared in advance (credentials for a headless run)
    is kept as it is; only the base URL and pseudonym key are added when it lacks them. Returns
    whether anything was written.
    """
    if layout.env_file.exists():
        existing = dotenv_values(layout.env_file)
        missing = {
            "FAUXCALL_REAL__BASE_URL": base_url,
            "FAUXCALL_PSEUDONYM_KEY": Pseudonymizer.generate_key(),
        }
        lines = [f"{name}={value}\n" for name, value in missing.items() if not existing.get(name)]
        if not lines:
            return False
        text = layout.env_file.read_text(encoding="utf-8")
        separator = "" if not text or text.endswith("\n") else "\n"
        with layout.env_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(separator + "".join(lines))
        layout.env_file.chmod(0o600)
        return True
    text = (
        "# Secrets for this fake. Gitignored: never commit it.\n"
        f"FAUXCALL_REAL__BASE_URL={base_url}\n"
        f"FAUXCALL_PSEUDONYM_KEY={Pseudonymizer.generate_key()}\n"
        "# Credentials for the real API go here (see .env.example): one line, e.g. a token\n"
        '# FAUXCALL_REAL__HEADERS=\'{"Authorization": "Bearer ..."}\'\n'
        "# or an API key header\n"
        '# FAUXCALL_REAL__HEADERS=\'{"X-API-Key": "..."}\'\n'
        "# TYPESAFE_API_KEY=...\n"
    )
    return _create_private(layout.env_file, text)


def env_value(layout: FakeLayout, name: str) -> str | None:
    """One value from the fake's ``.env`` (``None`` when absent)."""
    if not layout.env_file.exists():
        return None
    value = dotenv_values(layout.env_file).get(name)
    return value or None


def default_gitignore() -> str:
    """The workspace ``.gitignore`` written when there is none (the scaffold's template)."""
    return GITIGNORE_TEMPLATE.read_text(encoding="utf-8")


def ensure_workspace_gitignore(workspace: Workspace) -> bool:
    """Make sure ``.env`` files are ignored before any credential is written.

    Creates the workspace ``.gitignore`` when missing; otherwise appends whichever of the required
    credential patterns it lacks (everything else in it is left alone). Returns whether it changed.
    """
    gitignore = workspace.root / ".gitignore"
    if not gitignore.exists():
        atomic_write_text(gitignore, default_gitignore())
        return True
    text = gitignore.read_text(encoding="utf-8")
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITIGNORE_REQUIRED if line not in present]
    if not missing:
        return False
    separator = "" if not text or text.endswith("\n") else "\n"
    addition = "# Added by FauxCall: credentials never belong in git.\n" + "\n".join(missing)
    atomic_write_text(gitignore, f"{text}{separator}{addition}\n")
    return True


def new_progress(name: str, source: str, endpoint_ids: list[str], now: datetime) -> Progress:
    """Checkpoints with discovery done and every endpoint pending."""
    progress = Progress(fake=name, source=source, started_at=now, updated_at=now)
    progress.phases["discover"] = PhaseState(status="done", started_at=now, finished_at=now)
    for endpoint_id in endpoint_ids:
        progress.endpoint(endpoint_id)
    return progress


def _layout(workspace: Workspace, name: str) -> FakeLayout:
    layout = workspace.fake(validate_slug(name))
    if layout.exists():
        raise ConfigurationError(
            f"{layout.root} already exists — resume it with `fauxcall status {name}`, refresh it "
            "with /fauxcall:refresh, or choose another --name"
        )
    return layout


def _with_server(
    spec: Mapping[str, JsonValue], url: str, *, swagger2: bool
) -> dict[str, JsonValue]:
    """A copy of ``spec`` whose (only) server is ``url`` — how ``--base-url`` takes effect."""
    updated: dict[str, JsonValue] = dict(spec)
    if swagger2:
        parts = urlsplit(url)
        updated["schemes"] = [parts.scheme]
        updated["host"] = parts.netloc
        updated["basePath"] = parts.path or "/"
    else:
        updated["servers"] = [{"url": url}]
    return updated


def _document(
    spec: dict[str, JsonValue], spec_url: str | None, base_url: str | None
) -> OpenApiDocument:
    """The spec as a document, its server replaced by ``--base-url`` when given.

    ``--base-url`` supplies the origin; its path, when it has one, is the base path every endpoint
    is served under (else the spec's own base path is kept). Endpoint ids, auth rules and the real
    target are all derived from the same document, so they stay consistent.
    """
    doc = OpenApiDocument(spec, spec_url=spec_url)
    if not base_url:
        return doc
    if not urlsplit(base_url).netloc:
        raise ConfigurationError(f"--base-url must be an absolute URL, got {base_url!r}")
    path = urlsplit(base_url).path.rstrip("/") or doc.base_path
    server = origin(base_url) + path
    return OpenApiDocument(_with_server(spec, server, swagger2=doc.is_swagger2), spec_url=spec_url)


def _from_openapi(
    discovered: DiscoveredSource, target: str, name: str | None, base_url: str | None, now: datetime
) -> tuple[ApiModel, str, str]:
    spec = discovered.spec
    if not isinstance(spec, dict):
        raise ConfigurationError("the discovered spec is not a JSON object")
    spec_url = discovered.spec_url
    doc = _document(spec, spec_url, base_url)
    try:
        real = doc.real_target()
    except SpecError as exc:
        raise ConfigurationError(
            "the spec has no absolute server URL; pass --base-url https://api.example.com"
        ) from exc
    if not real.base_url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"the spec's server {real.base_url!r} is not http(s); pass --base-url "
            "https://api.example.com"
        )
    title = doc.title or discovered.title or "API"
    endpoints = endpoints_from_spec(doc)
    spec_paths = []
    if (
        spec_url
        and spec_url.startswith(("http://", "https://"))
        and origin(spec_url) == real.base_url
    ):
        spec_paths.append(urlsplit(spec_url).path)
    text = discovered.spec_text or jsonfmt.dumps(spec)
    extension = "yaml" if text.lstrip()[:1] not in {"{", "["} else "json"
    model = ApiModel(
        name=name or slugify(title),
        title=title,
        kind="rest",
        source=SourceInfo(
            kind=discovered.kind,
            url=target,
            spec_url=spec_url,
            spec_file=f"source/openapi.{extension}",
            spec_version=doc.version,
            title=title,
            api_version=doc.api_version,
            fetched_at=now,
        ),
        real=real,
        auth=auth_from_spec(doc),
        endpoints=endpoints,
        routing=RoutingModel(spec_paths=spec_paths),
    )
    hints = "\n".join(
        f"- `{e.id}` → suggested role **{role}**"
        + (f" (collection `{collection}`)" if collection else "")
        for e in endpoints
        for role, collection in [suggest_role(e, endpoints)]
    )
    summary = (
        summarize(doc, endpoints) + "\n\n### Suggested roles (confirm with evidence)\n\n" + hints
    )
    return model, text, summary


@dataclass(frozen=True, slots=True)
class McpAccess:
    """How ``init`` talks to an MCP server: credentials and the recording pace and budget."""

    headers: dict[str, str] = field(default_factory=dict)
    rps: float = DEFAULT_RPS
    max_calls: int = DEFAULT_MAX_CALLS


@contextlib.asynccontextmanager
async def target_credentials(
    http: httpx2.AsyncClient, target: str, headers: Mapping[str, str]
) -> AsyncIterator[None]:
    """Send ``headers`` with every request to ``target``'s host (and nowhere else) meanwhile.

    Discovery follows links to other sites (scripts, specs, conventional locations); credentials
    for the target must never reach them. Headers a request already sets are left alone.
    """
    if not headers:
        yield
        return
    host = urlsplit(target if "://" in target else f"https://{target}").netloc.lower()

    async def authenticate(request: httpx2.Request) -> None:
        if urlsplit(str(request.url)).netloc.lower() == host:
            for header, value in headers.items():
                request.headers.setdefault(header, value)

    hooks = http.event_hooks
    http.event_hooks = {**hooks, "request": [*hooks.get("request", []), authenticate]}
    try:
        yield
    finally:
        http.event_hooks = hooks


async def init_fake(  # noqa: PLR0913 - the options of `fauxcall init`
    target: str,
    *,
    workspace: Workspace,
    http: httpx2.AsyncClient,
    name: str | None = None,
    base_url: str | None = None,
    force_mcp: bool = False,
    mcp_headers: dict[str, str] | None = None,
    rps: float = DEFAULT_RPS,
    max_calls: int = DEFAULT_MAX_CALLS,
    now: datetime | None = None,
) -> InitResult:
    """Create a new fake from a spec URL/file, docs page or MCP server URL.

    ``mcp_headers`` (e.g. ``Authorization``) are sent to the target's host during discovery and
    MCP introspection, and redacted from every recording; ``rps`` and ``max_calls`` pace and cap
    the introspection calls (``FAUXCALL_RECORD_RPS`` / ``FAUXCALL_RECORD_MAX_CALLS`` in the CLI).
    """
    moment = now or datetime.now(UTC)
    if name is not None:
        validate_slug(name)
    workspace.root.mkdir(parents=True, exist_ok=True)
    ensure_workspace_gitignore(workspace)
    access = McpAccess(headers=dict(mcp_headers or {}), rps=rps, max_calls=max_calls)
    if force_mcp:
        return await _init_mcp(target, target, workspace, http, name, access, moment)
    async with target_credentials(http, target, access.headers):
        discovered = await discover(target, http)
    if discovered.kind == "mcp":
        endpoint = target
        if discovered.mcp_transport.get("transport") == "streamable_http":
            endpoint = discovered.mcp_transport.get("endpoint") or target
        return await _init_mcp(target, endpoint, workspace, http, name, access, moment)
    if discovered.kind == "html_docs":
        return _init_docs(discovered, target, workspace, name, base_url, moment)
    model, spec_text, summary = _from_openapi(discovered, target, name, base_url, moment)
    layout = _layout(workspace, model.name)
    assert model.source.spec_file is not None  # noqa: S101
    atomic_write_text(layout.data / model.source.spec_file, spec_text)
    write_model(layout.model_path, model)
    write_model(
        layout.progress_path,
        new_progress(layout.name, target, [e.id for e in model.endpoints], moment),
    )
    write_model(layout.data / "privacy.json", PrivacyConfig())
    ensure_env(layout, model.real.base_url)
    notes = list(discovered.notes)
    return InitResult(layout=layout, model=model, summary=summary, notes=notes)


def _init_docs(
    discovered: DiscoveredSource,
    target: str,
    workspace: Workspace,
    name: str | None,
    base_url: str | None,
    now: datetime,
) -> InitResult:
    snapshot = html_to_text(discovered.page_text or "", discovered.url)
    title = snapshot.title or discovered.title or "API"
    layout = _layout(workspace, name or slugify(title))
    real_base = origin(base_url) if base_url else origin(target)
    model = ApiModel(
        name=layout.name,
        title=title,
        kind="rest",
        source=SourceInfo(
            kind="html_docs",
            url=target,
            spec_file="source/openapi.json",
            title=title,
            fetched_at=now,
        ),
        real=RealTarget(
            base_url=real_base, base_path=urlsplit(base_url).path.rstrip("/") if base_url else ""
        ),
    )
    atomic_write_text(layout.source_dir / "docs.md", snapshot.text)
    write_json(layout.source_dir / "docs-links.json", list(snapshot.links))
    write_model(layout.model_path, model)
    write_model(layout.progress_path, new_progress(layout.name, target, [], now))
    write_model(layout.data / "privacy.json", PrivacyConfig())
    ensure_env(layout, real_base)
    summary = (
        f"Free-form documentation captured in `{layout.source_dir / 'docs.md'}` "
        f"({len(snapshot.links)} linked pages listed in docs-links.json).\n\n"
        "Next: read the docs (follow links as needed), write an OpenAPI 3.1 description of every "
        f"endpoint to `{layout.source_dir / 'openapi.json'}`, then run "
        f"`fauxcall spec import {layout.name}` — recordings will confirm or correct it."
    )
    return InitResult(layout=layout, model=model, summary=summary, notes=list(discovered.notes))


def _description(definition: JsonValue) -> str:
    """A tool's description, on one line and cut short (for the summary)."""
    text = definition.get("description") if isinstance(definition, dict) else None
    flat = " ".join(str(text or "").split())
    if len(flat) <= _DESCRIPTION_PREVIEW:
        return flat
    return flat[: _DESCRIPTION_PREVIEW - 1].rstrip() + "…"


async def _init_mcp(
    target: str,
    endpoint: str,
    workspace: Workspace,
    http: httpx2.AsyncClient,
    name: str | None,
    access: McpAccess,
    now: datetime,
) -> InitResult:
    host = urlsplit(endpoint).netloc.split(":")[0]
    provisional = name or slugify(host.replace("mcp.", "").replace(".", " "))
    layout = _layout(workspace, provisional)
    ensure_env(layout, origin(endpoint))
    privacy = RecordingPrivacy(
        PrivacyConfig(),
        secret_values=header_secrets(access.headers.values()),
        pseudonym_key=env_value(layout, "FAUXCALL_PSEUDONYM_KEY"),
    )
    recorder = McpRecorder(
        RecordingStore(layout.recordings),
        endpoint,
        http,
        headers=access.headers,
        privacy=privacy,
        rps=access.rps,
        max_calls=access.max_calls,
    )
    introspection = await recorder.introspect()
    mcp = introspection.model
    result = mcp.initialize_result if isinstance(mcp.initialize_result, dict) else {}
    info = result.get("serverInfo")
    info = info if isinstance(info, dict) else {}
    title = str(info.get("title") or info.get("name") or host)
    model = ApiModel(
        name=layout.name,
        title=title,
        kind="mcp",
        source=SourceInfo(
            kind="mcp",
            url=target,
            spec_url=endpoint,
            spec_file="source/mcp.json",
            title=title,
            fetched_at=now,
        ),
        real=RealTarget(base_url=origin(endpoint)),
        mcp=mcp,
    )
    write_json(
        layout.source_dir / "mcp.json",
        {
            "initialize": mcp.initialize_result,
            "tools": [t.definition for t in mcp.tools],
            "resources": mcp.resources,
            "resource_templates": mcp.resource_templates,
            "prompts": mcp.prompts,
        },
    )
    write_model(layout.model_path, model)
    endpoint_ids = [f"MCP tools/call:{tool.name}" for tool in mcp.tools]
    write_model(layout.progress_path, new_progress(layout.name, target, endpoint_ids, now))
    write_model(layout.data / "privacy.json", PrivacyConfig())
    tools = "\n".join(f"- `{tool.name}` — {_description(tool.definition)}" for tool in mcp.tools)
    transport = mcp.transport
    summary = (
        f"MCP server **{title}** at {endpoint} ({transport.kind}, answers with "
        f"{transport.response_mode}, protocol "
        f"{transport.default_protocol_version or FALLBACK_PROTOCOL_VERSION}).\n\n"
        f"### Tools\n\n{tools or '(none)'}\n\n"
        + "\n".join(f"- {note}" for note in introspection.notes)
    )
    return InitResult(layout=layout, model=model, summary=summary, notes=introspection.notes)
