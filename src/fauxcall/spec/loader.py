"""Load API descriptions: fetch documents over HTTP, read local files, parse JSON or YAML specs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx2
import yaml
from pydantic import Field

from fauxcall import jsonfmt
from fauxcall.errors import SpecError
from fauxcall.logs import get_logger
from fauxcall.model.common import Model

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

logger = get_logger(__name__)

MAX_DOCUMENT_BYTES: Final = 32 * 1024 * 1024
"""Bodies larger than this are refused: specs are big, but not this big."""

MAX_YAML_NODES: Final = 2_000_000
"""A YAML document may expand (through aliases) to at most this many nodes: no alias bombs."""

DEFAULT_ACCEPT: Final = (
    "application/json, application/yaml;q=0.9, text/yaml;q=0.9, text/html;q=0.9, */*;q=0.8"
)
"""Prefer machine-readable specs, accept docs pages and anything else."""

EVENT_STREAM: Final = "text/event-stream"
SPEC_MARKERS: Final = ("openapi", "swagger")
"""Top-level fields that identify an OpenAPI 3.x (``openapi``) or Swagger 2.0 document."""

_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_EXCERPT_LENGTH: Final = 200
_VERSION_RE: Final = re.compile(r"\d+(?:\.\d+)*(?:[-+.]\w+)*")


def media_type(content_type: str | None) -> str:
    """The media type of a Content-Type value: ``"Text/HTML; charset=utf-8"`` → ``"text/html"``."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def excerpt(text: str, limit: int = _EXCERPT_LENGTH) -> str:
    """``text`` on one line, cut to ``limit`` characters (for error messages)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def describe_error(exc: BaseException) -> str:
    """``"ConnectError: [Errno 61] Connection refused"`` (just the class name if no message)."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class FetchedDocument(Model):
    """A document fetched over HTTP."""

    url: str = Field(description="Final URL, after redirects.")
    status: int
    content_type: str | None = Field(default=None, description="The Content-Type header as sent.")
    text: str = Field(default="", description="Decoded body; empty for event streams.")

    @property
    def media_type(self) -> str:
        """The content type without parameters, lowercased (``""`` when absent)."""
        return media_type(self.content_type)

    @property
    def is_success(self) -> bool:
        """Whether the status is 2xx."""
        return httpx2.codes.is_success(self.status)

    @property
    def is_html(self) -> bool:
        """Whether the document is an HTML page (by content type, or by sniffing the body)."""
        if self.media_type in _HTML_TYPES:
            return True
        head = self.text.lstrip()[:1024].lower()
        return head.startswith("<") and ("<html" in head or "<!doctype html" in head)


async def read_body(
    response: httpx2.Response, *, limit: int = MAX_DOCUMENT_BYTES, truncate: bool = False
) -> str:
    """Read and decode a streamed response body.

    Args:
        response: A response opened with ``client.stream(...)``.
        limit: Maximum number of bytes to read.
        truncate: Stop quietly at ``limit`` (for excerpts) instead of raising.

    Raises:
        SpecError: The body is larger than ``limit`` and ``truncate`` is false.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        if size + len(chunk) > limit:
            if not truncate:
                raise SpecError(f"{response.url} is larger than {limit} bytes")
            chunks.append(chunk[: limit - size])
            break
        chunks.append(chunk)
        size += len(chunk)
    text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    return text.removeprefix("\ufeff")


async def fetch_document(
    url: str, client: httpx2.AsyncClient, *, accept: str = DEFAULT_ACCEPT
) -> FetchedDocument:
    """GET ``url``, following redirects. HTTP error statuses are returned, not raised.

    Event streams are not read (they may never end); their ``text`` is empty.

    Raises:
        SpecError: The request failed (connection, timeout, invalid URL) or the body is too large.
    """
    try:
        async with client.stream(
            "GET", url, headers={"Accept": accept}, follow_redirects=True
        ) as response:
            content_type = response.headers.get("content-type")
            streaming = media_type(content_type) == EVENT_STREAM
            text = "" if streaming else await read_body(response)
            document = FetchedDocument(
                url=str(response.url),
                status=response.status_code,
                content_type=content_type,
                text=text,
            )
    except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
        raise SpecError(f"GET {url} failed: {describe_error(exc)}") from exc
    logger.debug("spec.fetched", url=document.url, status=document.status, type=content_type)
    return document


async def fetch_text(url: str, client: httpx2.AsyncClient) -> FetchedDocument:
    """GET ``url`` (following redirects) and return the decoded document.

    The client is the caller's, so timeouts, proxies and credentials are under its control.

    Raises:
        SpecError: The request failed or the server answered with a non-2xx status.
    """
    document = await fetch_document(url, client)
    if not document.is_success:
        detail = f": {excerpt(document.text)}" if document.text.strip() else ""
        raise SpecError(f"GET {url} returned HTTP {document.status}{detail}")
    return document


def read_local(path: Path) -> str:
    """Read a local spec file as UTF-8 (a byte-order mark is dropped).

    Raises:
        SpecError: The file is missing, unreadable or not UTF-8 text.
    """
    try:
        return path.expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _SpecLoader(yaml.SafeLoader):
    """A safe YAML loader whose scalars mean what they would in JSON.

    YAML 1.1 (PyYAML's default) turns ``on``/``off``/``yes``/``no`` into booleans and unquoted
    dates into ``date`` objects. Specs are JSON documents at heart, so here only ``true``/``false``
    are booleans and timestamps stay strings.
    """


_SpecLoader.yaml_implicit_resolvers = {
    first: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


@dataclass(slots=True)
class _NodeBudget:
    remaining: int
    source: str

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise SpecError(
                f"{self.source} expands to more than {MAX_YAML_NODES} nodes (a YAML alias bomb?)"
            )


def _json_key(key: object) -> str:
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    return str(key)


def _to_json(value: object, budget: _NodeBudget) -> JsonValue:
    """Turn what PyYAML built into JSON-compatible data (string keys, finite numbers)."""
    budget.spend()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {_json_key(key): _to_json(item, budget) for key, item in value.items()}
    if isinstance(value, set):  # !!set: sorted, so parsing is deterministic
        return [_to_json(item, budget) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_to_json(item, budget) for item in value]
    return str(value)  # explicitly tagged scalars, e.g. !!timestamp or !!binary


def _load_yaml(text: str, source: str) -> JsonValue:
    loader = _SpecLoader(text)
    try:
        loaded: object = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise SpecError(f"{source} is neither valid JSON nor valid YAML: {exc}") from exc
    finally:
        loader.dispose()
    return _to_json(loaded, _NodeBudget(MAX_YAML_NODES, source))


def _looks_like_version(value: JsonValue) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and _VERSION_RE.fullmatch(value.strip()) is not None


def _type_name(value: JsonValue) -> str:
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "a string"
    return "null" if value is None else f"a {type(value).__name__}"


def parse_spec_text(text: str, *, source: str = "<input>") -> dict[str, JsonValue]:
    """Parse an OpenAPI/Swagger document written in JSON or YAML.

    JSON is tried first (keeping float spellings, see :mod:`fauxcall.jsonfmt`), then YAML with
    JSON-compatible scalars: only ``true``/``false`` are booleans, timestamps stay strings and
    mapping keys become strings (``200:`` → ``"200"``).

    Args:
        text: The document.
        source: Where it came from, for error messages (a URL or file name).

    Raises:
        SpecError: The text is not JSON/YAML, or not a mapping whose ``openapi`` or ``swagger``
            field holds a version (an API index linking ``"openapi": "/v1/openapi.json"`` is not a
            spec).
    """
    body = text.removeprefix("\ufeff")
    if not body.strip():
        raise SpecError(f"{source} is empty")
    try:
        try:
            value = jsonfmt.loads(body)
        except ValueError:
            value = _load_yaml(body, source)
    except RecursionError as exc:
        raise SpecError(f"{source} is nested too deeply to be an API description") from exc
    if not isinstance(value, dict):
        hint = " (it looks like HTML or XML)" if body.lstrip().startswith("<") else ""
        raise SpecError(
            f"{source} is not an OpenAPI/Swagger document: expected a mapping, "
            f"got {_type_name(value)}{hint}"
        )
    marker = next((marker for marker in SPEC_MARKERS if marker in value), None)
    if marker is None:
        raise SpecError(
            f"{source} is not an OpenAPI/Swagger document: it has no 'openapi' or 'swagger' field"
        )
    if not _looks_like_version(value[marker]):
        # e.g. an API index linking to its spec: {"openapi": "/v1/openapi.json"}
        raise SpecError(
            f"{source} is not an OpenAPI/Swagger document: its {marker!r} field is "
            f"{excerpt(repr(value[marker]), 60)}, not a version"
        )
    return value
