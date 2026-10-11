"""Figure out what a ``/fauxcall <url>`` input is, and fetch its machine-readable description.

The input can be an OpenAPI/Swagger document (local file or URL, JSON or YAML), an interactive docs
page that embeds one (Swagger UI, Redoc and similar renderers), free-form HTML documentation, or a
remote MCP server (Streamable HTTP or the legacy SSE transport). :func:`discover` tries, in order:

1. an existing local file → parsed as a spec (``openapi``);
2. ``GET <url>`` → an OpenAPI/Swagger document (``openapi``);
3. an HTML page → the spec it embeds (inline Swagger UI config, its initializer script,
   ``configUrl``, ``<redoc spec-url>``, ``Redoc.init(...)``, ``<link rel="alternate">``), then
   conventional spec locations next to it (``swagger_ui`` / ``redoc``, or ``openapi`` when the page
   is not a known renderer);
4. an MCP ``initialize`` handshake when the GET failed, returned an event stream or JSON-RPC, or
   the path ends with ``/mcp`` or ``/sse`` (``mcp``);
5. otherwise the page itself (``html_docs``).

Every step is recorded; the record becomes the result's ``notes`` or, when nothing works, the
:class:`~fauxcall.errors.SpecError` message.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit
from urllib.request import url2pathname

import httpx2
from bs4 import BeautifulSoup
from pydantic import Field

from fauxcall import __version__, jsonfmt
from fauxcall.errors import SpecError
from fauxcall.logs import get_logger
from fauxcall.model.api import SourceKind
from fauxcall.model.common import Model, RawJson
from fauxcall.spec.docs import attribute, page_title
from fauxcall.spec.loader import (
    EVENT_STREAM,
    FetchedDocument,
    describe_error,
    excerpt,
    fetch_document,
    media_type,
    parse_spec_text,
    read_body,
    read_local,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fauxcall.types import JsonValue

__all__ = ["DiscoveredSource", "SourceKind", "discover", "extract_spec_urls"]

logger = get_logger(__name__)

MCP_PROTOCOL_VERSION: Final = "2025-06-18"
CONVENTIONAL_SPEC_PATHS: Final = (
    "openapi.json",
    "swagger.json",
    "v3/api-docs",
    "openapi.yaml",
    "swagger/v1/swagger.json",
    "api-docs",
)
"""Where frameworks usually serve their spec, probed next to a page that embeds none."""

MAX_CANDIDATES: Final = 8
"""Spec URLs fetched per page before giving up on its embedded references."""

MAX_SCRIPTS: Final = 3
"""Same-origin scripts fetched from a renderer page to find its configuration."""

MAX_SSE_EVENTS: Final = 50
"""Events read from a stream while waiting for the ``initialize`` response."""

MCP_HANDSHAKE_SECONDS: Final = 30.0
"""Overall deadline for one MCP handshake (servers may hold event streams open forever)."""

_INITIALIZE_ID: Final = 1
_MCP_HEADERS: Final = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_PLACEHOLDER_SPEC: Final = "https://petstore.swagger.io/v2/swagger.json"
"""Swagger UI's default ``url``; left in place by many deployments that configure it elsewhere."""

_QUOTED_VALUE: Final = r"""\s*:\s*(["'`])([^"'`\s]+)\1"""
_URL_KEY_RE: Final = re.compile(r"""(?<![\w$.])["']?url["']?""" + _QUOTED_VALUE)
_CONFIG_URL_RE: Final = re.compile(r"""(?<![\w$.])["']?configUrl["']?""" + _QUOTED_VALUE)
_REDOC_INIT_RE: Final = re.compile(r"""Redoc\.init\(\s*(["'`])([^"'`\s]+)\1""")
_ABSOLUTE_URL_RE: Final = re.compile(r"""https?://[^\s"'`<>()\[\]{}\\,;|^]+""")
_QUOTED_PATH_RE: Final = re.compile(r"""(["'`])([\w./~%:@+-]{1,300})\1""")
"""A string literal that could be a URL or path, e.g. ``fetch("/spec.json")``."""
_SPEC_FILE_RE: Final = re.compile(
    r"(?:^|/)(?:openapi|swagger|api-docs|spec)[\w.-]*\.(?:json|ya?ml)$|/api-docs$|/openapi$",
    re.IGNORECASE,
)
_BARE_HOST_RE: Final = re.compile(
    r"(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})"
    r"(?::\d+)?(?:[/?#].*)?",
    re.IGNORECASE,
)
_FILE_SUFFIXES: Final = (".json", ".yaml", ".yml", ".html", ".htm", ".txt", ".md")
_SPEC_ATTRIBUTES: Final = (
    ("redoc", "spec-url"),
    ("rapi-doc", "spec-url"),
    ("openapi-explorer", "spec-url"),
    ("elements-api", "apidescriptionurl"),
)
_RENDERER_TAGS: Final = (
    ("rapi-doc", "RapiDoc"),
    ("elements-api", "Stoplight Elements"),
    ("openapi-explorer", "OpenAPI Explorer"),
)
_LIBRARY_SCRIPT_WORDS: Final = (
    "bundle",
    "standalone",
    "preset",
    "polyfill",
    "vendor",
    "jquery",
    "analytics",
    "gtag",
    "gtm",
    "swagger-ui.js",
    "redoc",
    "rapidoc",
    ".min.",
)
_SWAGGER_CONFIG_KEYS: Final = frozenset(
    {
        "configUrl",
        "oauth2RedirectUrl",
        "validatorUrl",
        "dom_id",
        "deepLinking",
        "docExpansion",
        "layout",
        "displayRequestDuration",
        "operationsSorter",
        "tagsSorter",
    }
)


class DiscoveredSource(Model):
    """What a ``/fauxcall`` input turned out to be, with everything fetched along the way."""

    kind: SourceKind
    url: str = Field(description="The input, as given.")
    spec_url: str | None = Field(default=None, description="The machine-readable spec found.")
    spec: RawJson = Field(default=None, description="The parsed spec (openapi/swagger_ui/redoc).")
    spec_text: str | None = Field(default=None, description="The spec as fetched, to snapshot.")
    page_text: str | None = Field(default=None, description="The HTML page (docs and renderers).")
    title: str | None = None
    mcp_initialize: RawJson = Field(
        default=None, description="The raw JSON-RPC response to 'initialize' (mcp)."
    )
    mcp_transport: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Observed transport: transport (streamable_http|sse), endpoint, content_type, "
            "response_mode (json|sse), session_header/session_id when the server issued one."
        ),
    )
    notes: list[str] = Field(default_factory=list, description="What was tried, in order.")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

_EXPLICIT, _GENERIC, _LINKED = 0, 1, 2


class _Candidates:
    """Spec URLs found on a page, in tiers: explicit configuration, URLs in scripts, links."""

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url
        self.page_host = (urlsplit(page_url).hostname or "").lower()
        self.tiers: tuple[list[str], list[str], list[str]] = ([], [], [])
        self.configs: list[str] = []
        self.found_in: dict[str, str] = {}

    def _absolute(self, raw: str | None) -> str | None:
        value = (raw or "").strip()
        if not value or "${" in value or "{{" in value:
            return None
        url = urldefrag(urljoin(self.page_url, value)).url
        if urlsplit(url).scheme not in {"http", "https"} or url in self.found_in:
            return None
        if url == _PLACEHOLDER_SPEC and self.page_host != "petstore.swagger.io":
            return None
        return url

    def add(self, tier: int, raw: str | None, where: str) -> None:
        url = self._absolute(raw)
        if url is not None:
            self.found_in[url] = where
            self.tiers[tier].append(url)

    def add_config(self, raw: str | None, where: str) -> None:
        url = self._absolute(raw)
        if url is not None:
            self.found_in[url] = where
            self.configs.append(url)

    def ranked(self) -> list[str]:
        """Explicit URLs in page order, then the rest with the page's own host first."""
        explicit, generic, linked = self.tiers

        def same_host_first(urls: list[str]) -> list[str]:
            return sorted(urls, key=lambda url: (urlsplit(url).hostname or "") != self.page_host)

        return [*explicit, *same_host_first(generic), *same_host_first(linked)]


@dataclass(slots=True)
class _PageAnalysis:
    renderer: str | None
    title: str | None
    spec_urls: list[str]
    config_urls: list[str]
    script_urls: list[str]
    found_in: dict[str, str] = field(default_factory=dict)


def _spec_like(url: str) -> bool:
    return bool(_SPEC_FILE_RE.search(urlsplit(url).path))


def _unescape_js(text: str) -> str:
    return text.replace("\\/", "/").replace('\\"', '"').replace("\\'", "'")


def _renderer(soup: BeautifulSoup, text: str) -> str | None:
    lowered = text.lower()
    if soup.find("redoc") is not None or "redoc.init(" in lowered or "redoc.standalone" in lowered:
        return "Redoc"
    if "swagger-ui" in lowered or "swaggeruibundle" in lowered:
        return "Swagger UI"
    for tag, name in _RENDERER_TAGS:
        if soup.find(tag) is not None:
            return name
    return "Scalar" if soup.find("script", id="api-reference") is not None else None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _script_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Same-origin scripts that may hold a renderer's configuration (not libraries)."""
    urls: list[str] = []
    for tag in soup.find_all("script", src=True):
        url = urljoin(page_url, attribute(tag, "src") or "")
        name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
        if _origin(url) != _origin(page_url) or any(w in name for w in _LIBRARY_SCRIPT_WORDS):
            continue
        if url not in urls:
            urls.append(url)
    return urls[:MAX_SCRIPTS]


def _scan_tags(soup: BeautifulSoup, found: _Candidates) -> None:
    for tag_name, name in _SPEC_ATTRIBUTES:
        for tag in soup.find_all(tag_name):
            found.add(_EXPLICIT, attribute(tag, name), f"<{tag_name} {name}>")
    for tag in soup.find_all("script", id="api-reference"):
        found.add(_EXPLICIT, attribute(tag, "data-url"), "Scalar's data-url")
    for link in soup.find_all("link", href=True):
        rel = (attribute(link, "rel") or "").lower().split()
        kind = (attribute(link, "type") or "").lower()
        if "service-desc" in rel or (
            "alternate" in rel and ("openapi" in kind or "swagger" in kind)
        ):
            found.add(_EXPLICIT, attribute(link, "href"), f"<link rel={' '.join(rel)}>")


def _scan_scripts(
    scripts: Sequence[tuple[str, str]], found: _Candidates, *, renderer: str | None
) -> None:
    for where, raw in scripts:
        text = _unescape_js(raw)
        for match in _REDOC_INIT_RE.finditer(text):
            found.add(_EXPLICIT, match.group(2), f"Redoc.init() in {where}")
        for match in _CONFIG_URL_RE.finditer(text):
            found.add_config(match.group(2), f"configUrl in {where}")
        for match in _URL_KEY_RE.finditer(text):
            if renderer is not None or _spec_like(match.group(2)):
                found.add(_EXPLICIT, match.group(2), f"url in {where}")
        for match in _ABSOLUTE_URL_RE.finditer(text):
            if _spec_like(match.group(0)):
                found.add(_GENERIC, match.group(0), f"a spec-like URL in {where}")
        for match in _QUOTED_PATH_RE.finditer(text):
            if _spec_like(match.group(2)):
                found.add(_GENERIC, match.group(2), f"a spec-like path in {where}")


def _analyze_page(
    html: str, page_url: str, scripts: Sequence[tuple[str, str]] = ()
) -> _PageAnalysis:
    soup = BeautifulSoup(html, "html.parser")
    renderer = _renderer(soup, html + "".join(text for _, text in scripts))
    found = _Candidates(page_url)
    if renderer == "Swagger UI":  # queryConfigEnabled: ?url=... / ?configUrl=... on the page
        query = parse_qs(urlsplit(page_url).query)
        for value in query.get("configUrl", []):
            found.add_config(value, "the page's ?configUrl= parameter")
        for value in query.get("url", []):
            found.add(_EXPLICIT, value, "the page's ?url= parameter")
    _scan_tags(soup, found)
    inline = [("an inline script", tag.get_text()) for tag in soup.find_all("script", src=False)]
    _scan_scripts([*inline, *scripts], found, renderer=renderer)
    for anchor in soup.find_all("a", href=True):
        href = attribute(anchor, "href") or ""
        if _spec_like(href):
            found.add(_LINKED, href, "a link on the page")
    return _PageAnalysis(
        renderer=renderer,
        title=page_title(soup),
        spec_urls=found.ranked(),
        config_urls=found.configs,
        script_urls=_script_urls(soup, page_url),
        found_in=found.found_in,
    )


def extract_spec_urls(html: str, page_url: str, *, scripts: Sequence[str] = ()) -> list[str]:
    """Spec URLs referenced by a docs page, most likely first, resolved against ``page_url``.

    Looks at Redoc (``<redoc spec-url>``, ``Redoc.init("...")``), Swagger UI configuration
    (``url: "..."``, ``urls: [{url: ...}]``, also inside ``JSON.parse('...')``), other renderers
    (RapiDoc, Stoplight Elements, Scalar, OpenAPI Explorer), ``<link rel="alternate"
    type="application/openapi+json">`` and ``rel="service-desc"``, spec-like absolute URLs in
    scripts (same host first) and links to spec files. Swagger UI's placeholder Petstore URL is
    ignored, and so are ``configUrl`` references (:func:`discover` follows those).

    Args:
        html: The page.
        page_url: The page's URL.
        scripts: Texts of scripts the page loads (e.g. ``swagger-initializer.js``), scanned as if
            they were inline.
    """
    named = [(f"script #{index}", text) for index, text in enumerate(scripts, start=1)]
    return _analyze_page(html, page_url, named).spec_urls


def _swagger_config_urls(text: str, base_url: str) -> list[str]:
    """Spec URLs in a Swagger UI configuration document (``urls[].url``, then ``url``)."""
    try:
        value = jsonfmt.loads(text)
    except (ValueError, RecursionError):
        return []
    if not isinstance(value, dict):
        return []
    entries = value.get("urls")
    raw = (
        [entry.get("url") for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list)
        else []
    )
    if "url" in value and not _SWAGGER_CONFIG_KEYS.isdisjoint(value):
        raw.append(value["url"])
    urls: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            url = urldefrag(urljoin(base_url, item.strip())).url
            if url not in urls:
                urls.append(url)
    return urls


def _conventional_bases(url: str) -> list[str]:
    """The URL itself as a directory (``/v1`` → ``/v1/``), its directory, and its origin."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}/"
    path = parts.path or "/"
    bases: list[str] = []
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not path.endswith("/") and last and "." not in last:
        bases.append(origin + path.lstrip("/") + "/")
    for base in (urljoin(origin + path.lstrip("/"), "."), origin):
        if base not in bases:
            bases.append(base)
    return bases


def _kind_for(renderer: str | None) -> SourceKind:
    if renderer is None:
        return "openapi"
    return "redoc" if renderer == "Redoc" else "swagger_ui"


def _parse(document: FetchedDocument) -> dict[str, JsonValue] | None:
    try:
        return parse_spec_text(document.text, source=document.url)
    except SpecError:
        return None


def _describe_spec(spec: Mapping[str, JsonValue]) -> str:
    if "openapi" in spec:
        return f"OpenAPI {spec['openapi']} document"
    return f"Swagger {spec.get('swagger')} document"


def _spec_title(spec: Mapping[str, JsonValue]) -> str | None:
    info = spec.get("info")
    title = info.get("title") if isinstance(info, dict) else None
    return title.strip() or None if isinstance(title, str) else None


def _looks_like_jsonrpc(text: str) -> bool:
    try:
        value = jsonfmt.loads(text)
    except (ValueError, RecursionError):
        return False
    items = value if isinstance(value, list) else [value]
    return any(isinstance(item, dict) and "jsonrpc" in item for item in items)


def _mcp_path(url: str) -> bool:
    return urlsplit(url).path.rstrip("/").endswith(("/mcp", "/sse"))


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _initialize_request() -> dict[str, JsonValue]:
    return {
        "jsonrpc": "2.0",
        "id": _INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fauxcall", "version": __version__},
        },
    }


def _match_response(text: str) -> dict[str, JsonValue] | None:
    """The JSON-RPC message (or batch member) answering our ``initialize`` request."""
    try:
        value = jsonfmt.loads(text)
    except (ValueError, RecursionError):
        return None
    for item in value if isinstance(value, list) else [value]:
        if (
            isinstance(item, dict)
            and str(item.get("id")) == str(_INITIALIZE_ID)
            and ("result" in item or "error" in item)
        ):
            return item
    return None


def _initialize_result(
    message: dict[str, JsonValue] | None,
) -> tuple[dict[str, JsonValue] | None, str]:
    """``(result, "")`` for a successful initialize, else ``(None, what went wrong)``."""
    if message is None:
        return None, "no JSON-RPC response to initialize"
    error = message.get("error")
    if isinstance(error, dict):
        return None, f"JSON-RPC error {error.get('code')}: {error.get('message')}"
    result = message.get("result")
    if isinstance(result, dict) and isinstance(result.get("serverInfo"), dict):
        return result, ""
    return None, "the initialize result has no serverInfo"


async def _sse_response(response: httpx2.Response) -> dict[str, JsonValue] | None:
    """Read SSE events until one carries the response to ``initialize``."""
    seen = 0
    async for event in httpx2.EventSource(response):
        message = _match_response(event.data)
        if message is not None:
            return message
        seen += 1
        if seen >= MAX_SSE_EVENTS:
            break
    return None


def _session_header(response: httpx2.Response) -> tuple[str, str] | None:
    """``(name as sent, value)`` of the ``Mcp-Session-Id`` header, if the server issued one."""
    for name, value in response.headers.raw:
        if name.lower() == b"mcp-session-id":
            return name.decode("latin-1"), value.decode("latin-1")
    return None


def _auth_hint(response: httpx2.Response) -> str:
    challenge = response.headers.get("www-authenticate")
    if challenge and response.status_code in {httpx2.codes.UNAUTHORIZED, httpx2.codes.FORBIDDEN}:
        return (
            f" — authorization required (WWW-Authenticate: {excerpt(challenge)}); "
            "configure credentials on the HTTP client"
        )
    return ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class _Found(NamedTuple):
    document: FetchedDocument
    spec: dict[str, JsonValue]


class _Discovery:
    """One :func:`discover` run: the client, and a trail of everything tried, in order."""

    def __init__(self, target: str, client: httpx2.AsyncClient) -> None:
        self.target = target
        self.client = client
        self.trail: list[str] = []
        self.fetched: set[str] = set()

    def amend(self, text: str) -> None:
        """Add to the latest trail line (its outcome)."""
        self.trail[-1] += text

    # -- fetching ------------------------------------------------------------------------------

    async def fetch(self, url: str, why: str | None = None) -> tuple[FetchedDocument | None, str]:
        """GET ``url`` without recording; returns the document (or ``None``) and a trail line."""
        self.fetched.add(url)
        prefix = f"GET {url}" + (f" (from {why})" if why else "")
        try:
            document = await fetch_document(url, self.client)
        except SpecError as exc:
            cause = exc.__cause__
            return None, f"{prefix} failed: {describe_error(cause) if cause else exc}"
        line = f"{prefix} -> HTTP {document.status}"
        if document.media_type:
            line += f" ({document.media_type})"
        return document, line

    async def get(
        self, url: str, why: str | None = None, *, detail: bool = False
    ) -> FetchedDocument | None:
        """GET ``url`` and record it (with an excerpt of an error body when ``detail``)."""
        document, line = await self.fetch(url, why)
        if detail and document is not None and not document.is_success and document.text.strip():
            line += f": {excerpt(document.text)}"
        self.trail.append(line)
        return document

    # -- flow ----------------------------------------------------------------------------------

    async def run(self, url: str) -> DiscoveredSource:
        page = await self.get(url, detail=True)
        if page is None or not page.is_success:
            found = await self.after_failure(url, reachable=page is not None)
        elif page.media_type == EVENT_STREAM:
            found = await self.probe_mcp(page.url, event_stream=True)
        elif page.is_html:
            found = await self.from_html(page)
        else:
            found = await self.from_document(page)
        if found is None:
            raise self.failure()
        return found

    async def after_failure(self, url: str, *, reachable: bool) -> DiscoveredSource | None:
        found = await self.probe_mcp(url, event_stream=False)
        if found is None and reachable:
            spec = await self.conventional(url)
            if spec is not None:
                return self.spec_source("openapi", spec)
        return found

    async def from_document(self, page: FetchedDocument) -> DiscoveredSource | None:
        """A 2xx answer that is neither HTML nor an event stream."""
        spec = _parse(page)
        if spec is not None:
            self.amend(f": {_describe_spec(spec)}")
            return self.spec_source("openapi", _Found(page, spec))
        self.amend(": not an OpenAPI/Swagger document")
        if _looks_like_jsonrpc(page.text) or _mcp_path(page.url):
            mcp = await self.probe_mcp(page.url, event_stream=False)
            if mcp is not None:
                return mcp
        configured = _swagger_config_urls(page.text, page.url)
        found = await self.try_candidates(configured, base_url=page.url)
        if found is not None:
            return self.spec_source("swagger_ui", found)
        found = await self.conventional(page.url)
        if found is not None:
            return self.spec_source("openapi", found)
        if page.media_type.startswith("text/"):
            self.trail.append("no spec found: treating the text as free-form documentation")
            return DiscoveredSource(
                kind="html_docs", url=self.target, page_text=page.text, notes=list(self.trail)
            )
        return None

    async def from_html(self, page: FetchedDocument) -> DiscoveredSource | None:
        analysis = _analyze_page(page.text, page.url)
        self.amend(f": {analysis.renderer} page" if analysis.renderer else ": HTML page")
        found = await self.try_analysis(analysis, page.url)
        if found is None and analysis.renderer is not None and analysis.script_urls:
            scripts = await self.fetch_scripts(analysis.script_urls)
            if scripts:
                analysis = _analyze_page(page.text, page.url, scripts)
                found = await self.try_analysis(analysis, page.url)
        if found is None:
            found = await self.conventional(page.url)
        if found is not None:
            return self.spec_source(
                _kind_for(analysis.renderer), found, page=page, page_title=analysis.title
            )
        if _mcp_path(page.url):
            mcp = await self.probe_mcp(page.url, event_stream=False)
            if mcp is not None:
                return mcp
        self.trail.append("no spec found: treating the page as free-form documentation")
        return DiscoveredSource(
            kind="html_docs",
            url=self.target,
            page_text=page.text,
            title=analysis.title,
            notes=list(self.trail),
        )

    async def try_analysis(self, analysis: _PageAnalysis, base_url: str) -> _Found | None:
        return await self.try_candidates(
            [*analysis.config_urls, *analysis.spec_urls],
            base_url=base_url,
            found_in=analysis.found_in,
        )

    async def try_candidates(
        self, urls: Sequence[str], *, base_url: str, found_in: Mapping[str, str] | None = None
    ) -> _Found | None:
        """Fetch candidates in order until one is a spec, following Swagger UI configs."""
        queue = deque(urls)
        origins = dict(found_in or {})
        attempts = 0
        while queue and attempts < MAX_CANDIDATES:
            url = queue.popleft()
            if url in self.fetched:
                continue
            attempts += 1
            document = await self.get(url, origins.get(url))
            if document is None or not document.is_success:
                continue
            spec = _parse(document)
            if spec is not None:
                self.amend(f": {_describe_spec(spec)}")
                return _Found(document, spec)
            targets = [t for t in _swagger_config_urls(document.text, base_url) if t not in queue]
            if targets:
                self.amend(": a Swagger UI configuration")
                queue.extendleft(reversed(targets))
                origins.update(dict.fromkeys(targets, f"the configuration at {url}"))
            else:
                self.amend(": not an OpenAPI/Swagger document")
        return None

    async def fetch_scripts(self, urls: Sequence[str]) -> list[tuple[str, str]]:
        scripts: list[tuple[str, str]] = []
        for url in urls:
            document = await self.get(url, "a script on the page")
            if document is not None and document.is_success:
                scripts.append((urlsplit(url).path.rsplit("/", 1)[-1], document.text))
        return scripts

    async def conventional(self, url: str) -> _Found | None:
        """Probe the usual spec locations next to ``url`` (quietly; one summary line)."""
        bases = _conventional_bases(url)
        candidates = [
            base + name
            for base in bases
            for name in CONVENTIONAL_SPEC_PATHS
            if base + name not in self.fetched
        ]
        for candidate in candidates:
            document, line = await self.fetch(candidate)
            if document is None:
                self.trail.append(f"stopped probing conventional spec locations: {line}")
                return None
            spec = _parse(document) if document.is_success else None
            if spec is not None:
                self.trail.append(f"{line}: {_describe_spec(spec)} (a conventional location)")
                return _Found(document, spec)
        self.trail.append(
            f"no spec at the conventional locations ({', '.join(CONVENTIONAL_SPEC_PATHS)}) "
            f"under {' or '.join(bases)}"
        )
        return None

    # -- MCP -----------------------------------------------------------------------------------

    async def probe_mcp(self, url: str, *, event_stream: bool) -> DiscoveredSource | None:
        """Try an MCP ``initialize``: Streamable HTTP first, then the legacy SSE transport."""
        found = await self.mcp_streamable(url)
        if found is None and (event_stream or urlsplit(url).path.rstrip("/").endswith("/sse")):
            found = await self.mcp_legacy_sse(url)
        return found

    async def mcp_streamable(self, url: str) -> DiscoveredSource | None:
        label = f"POST {url} (MCP initialize)"
        try:
            async with (
                asyncio.timeout(MCP_HANDSHAKE_SECONDS),
                self.client.stream(
                    "POST",
                    url,
                    json=_initialize_request(),
                    headers=_MCP_HEADERS,
                    follow_redirects=True,
                ) as response,
            ):
                content_type = response.headers.get("content-type") or ""
                if not response.is_success:
                    body = await read_body(response, limit=4096, truncate=True)
                    detail = f": {excerpt(body)}" if body.strip() else ""
                    status = response.status_code
                    self.trail.append(f"{label} -> HTTP {status}{_auth_hint(response)}{detail}")
                    return None
                streamed = media_type(content_type) == EVENT_STREAM
                if streamed:
                    message = await _sse_response(response)
                else:
                    message = _match_response(await read_body(response))
                session = _session_header(response)
                endpoint = str(response.url)
        except TimeoutError:
            self.trail.append(f"{label}: no answer within {MCP_HANDSHAKE_SECONDS:g} seconds")
            return None
        except (httpx2.HTTPError, httpx2.InvalidURL, SpecError) as exc:
            self.trail.append(f"{label} failed: {describe_error(exc)}")
            return None
        result, problem = _initialize_result(message)
        if result is None or message is None:
            self.trail.append(f"{label} -> HTTP 200 ({media_type(content_type)}): {problem}")
            return None
        transport = {
            "transport": "streamable_http",
            "endpoint": endpoint,
            "content_type": content_type,
            "response_mode": "sse" if streamed else "json",
        }
        if session is not None:
            transport["session_header"], transport["session_id"] = session
        return self.mcp_source(message, result, transport)

    async def mcp_legacy_sse(self, url: str) -> DiscoveredSource | None:
        """The 2024-11-05 transport: GET an event stream, POST to the ``endpoint`` it announces."""
        label = f"GET {url} (MCP SSE transport)"
        try:
            async with (
                asyncio.timeout(MCP_HANDSHAKE_SECONDS),
                self.client.stream(
                    "GET", url, headers={"Accept": EVENT_STREAM}, follow_redirects=True
                ) as response,
            ):
                kind = media_type(response.headers.get("content-type"))
                if not response.is_success or kind != EVENT_STREAM:
                    status = response.status_code
                    self.trail.append(f"{label} -> HTTP {status} ({kind or 'no content type'})")
                    return None
                return await self.legacy_handshake(response, label)
        except TimeoutError:
            self.trail.append(f"{label}: no answer within {MCP_HANDSHAKE_SECONDS:g} seconds")
            return None
        except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
            self.trail.append(f"{label} failed: {describe_error(exc)}")
            return None

    async def legacy_handshake(
        self, stream: httpx2.Response, label: str
    ) -> DiscoveredSource | None:
        endpoint: str | None = None
        seen = 0
        async for event in httpx2.EventSource(stream):
            seen += 1
            if seen > MAX_SSE_EVENTS:
                break
            if endpoint is None:
                if event.event == "endpoint":
                    endpoint = urljoin(str(stream.url), event.data.strip())
                    posted = await self.client.post(
                        endpoint, json=_initialize_request(), headers=_MCP_HEADERS
                    )
                    if not posted.is_success:
                        self.trail.append(
                            f"POST {endpoint} (MCP initialize) -> HTTP {posted.status_code}"
                        )
                        return None
                continue
            message = _match_response(event.data)
            if message is None:
                continue
            result, problem = _initialize_result(message)
            if result is None:
                self.trail.append(f"{label}: {problem}")
                return None
            transport = {
                "transport": "sse",
                "endpoint": endpoint,
                "content_type": EVENT_STREAM,
                "response_mode": "sse",
            }
            return self.mcp_source(message, result, transport)
        missing = "no 'endpoint' event" if endpoint is None else "no initialize response"
        self.trail.append(f"{label}: {missing} on the stream")
        return None

    # -- results -------------------------------------------------------------------------------

    def spec_source(
        self,
        kind: SourceKind,
        found: _Found,
        *,
        page: FetchedDocument | None = None,
        page_title: str | None = None,
    ) -> DiscoveredSource:
        return DiscoveredSource(
            kind=kind,
            url=self.target,
            spec_url=found.document.url,
            spec=found.spec,
            spec_text=found.document.text,
            page_text=page.text if page is not None else None,
            title=_spec_title(found.spec) or page_title,
            notes=list(self.trail),
        )

    def mcp_source(
        self,
        message: dict[str, JsonValue],
        result: dict[str, JsonValue],
        transport: dict[str, str],
    ) -> DiscoveredSource:
        info = result.get("serverInfo")
        info = info if isinstance(info, dict) else {}
        name = next((v for v in (info.get("title"), info.get("name")) if isinstance(v, str)), None)
        self.trail.append(
            f"MCP server {name!r} {info.get('version')} answered initialize "
            f"(protocol {result.get('protocolVersion')}) over {transport['transport']}"
        )
        return DiscoveredSource(
            kind="mcp",
            url=self.target,
            title=name,
            mcp_initialize=message,
            mcp_transport=transport,
            notes=list(self.trail),
        )

    def failure(self) -> SpecError:
        tried = "\n".join(f"  - {line}" for line in self.trail)
        return SpecError(
            f"could not find an OpenAPI/Swagger spec, docs page or MCP server at {self.target!r}."
            f"\nTried:\n{tried}\nPass the spec itself (a JSON/YAML file or URL), its Swagger UI or "
            "Redoc page, a docs page, or an MCP endpoint (usually ending in /mcp)."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _local_file(target: str) -> Path | None:
    """The local file ``target`` names, if any (plain paths and ``file://`` URLs)."""
    if target.startswith("file:"):
        path = Path(url2pathname(urlsplit(target).path))
        if not path.is_file():
            raise SpecError(f"no such file: {path}")
        return path
    if "://" in target:
        return None
    path = Path(target).expanduser()
    try:
        if path.is_file():
            return path.resolve()
        is_dir = path.is_dir()
    except OSError:  # e.g. a name too long to be a path
        return None
    if is_dir:
        raise SpecError(f"{target} is a directory, not a spec file")
    return None


def _http_url(target: str) -> str:
    """``target`` as an http(s) URL (a bare host like ``api.example.com/v1`` gets ``https://``)."""
    parts = urlsplit(target)
    if parts.scheme in {"http", "https"} and parts.netloc:
        return target
    head = target.split("/", 1)[0].lower()
    if (
        "://" not in target
        and _BARE_HOST_RE.fullmatch(target)
        and not head.endswith(_FILE_SUFFIXES)
    ):
        scheme = "http" if head.startswith(("localhost", "127.")) else "https"
        return f"{scheme}://{target}"
    raise SpecError(f"{target!r} is neither an existing file nor an http(s) URL")


async def discover(target: str, client: httpx2.AsyncClient) -> DiscoveredSource:
    """Work out what ``target`` is and fetch what describes it.

    Args:
        target: What the user passed to ``/fauxcall``: a spec file path or URL, a Swagger UI /
            Redoc page, a docs page, or a remote MCP server URL.
        client: The HTTP client to use (the caller controls timeouts, headers and credentials).

    Returns:
        The source, with the parsed spec (``openapi``/``swagger_ui``/``redoc``), the page
        (``html_docs`` and renderers) or the ``initialize`` response (``mcp``), and notes on what
        was tried.

    Raises:
        SpecError: Nothing usable was found; the message lists every attempt.
    """
    target = target.strip()
    local = _local_file(target)
    if local is not None:
        text = read_local(local)
        spec = parse_spec_text(text, source=str(local))
        source = DiscoveredSource(
            kind="openapi",
            url=target,
            spec_url=local.as_uri(),
            spec=spec,
            spec_text=text,
            title=_spec_title(spec),
            notes=[f"read {local}: {_describe_spec(spec)}"],
        )
    else:
        source = await _Discovery(target, client).run(_http_url(target))
    logger.info("spec.discovered", kind=source.kind, url=target, spec_url=source.spec_url)
    return source
