"""Generate a fake's Python package (and the shared workspace files) from its model.

Files fall into three groups: *generated* (``routes.py``, compose, CI) are rewritten on every run;
*owned* files (``handlers.py``, settings, tests, ``pyproject.toml`` …) are created once and never
overwritten, so custom code and hand-written tests are safe; a fake's README is regenerated *around
its notes* (known limitations and usage notes between ``fauxcall:notes`` markers carry over).

Generated Python must pass the fake's own gates unchanged: ``ruff check`` with ``select = ["ALL"]``,
``ruff format --check`` and ``mypy --strict``. Everything taken from the API description (titles,
summaries, operation ids, paths, recorded arguments) is therefore sanitized here and spelled exactly
the way ``ruff format`` would spell it: string literals pick the quote style with fewer escapes and
escape every non-ASCII character, lines that cannot fit are exploded with magic trailing commas, and
the few lines that can never fit (a long string) carry a ``# noqa: E501``.
"""

from __future__ import annotations

import builtins
import json
import keyword
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlencode

import jinja2

from fauxcall import __version__
from fauxcall.errors import ConfigurationError
from fauxcall.privacy import REDACTED
from fauxcall.runtime.routing import route_identifier
from fauxcall.storage import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.model.recording import Exchange
    from fauxcall.paths import FakeLayout, Workspace
    from fauxcall.types import JsonValue

TEMPLATES: Final = Path(__file__).parent / "templates"
FIRST_PORT: Final = 8701
LINE_LIMIT: Final = 100
INDENT: Final = "    "
MAX_NAME: Final = 50
"""Longest generated function name (keeps every generated ``def`` line within the limit)."""

_PARAM: Final = re.compile(r"\{([^{}/]+)\}")
_RESERVED: Final = frozenset(
    {"annotations", "router", "handle", "handle_websocket", "request", "websocket", "response"}
)
"""Names a generated route function must not take (module names of ``routes.py`` and friends)."""

SCENARIO_SKIPPED_ROLES: Final = frozenset(
    {
        "create",
        "replace",
        "update",
        "delete",
        "job_submit",
        "job_status",
        "download",
        "upload",
        "sse",
        "websocket",
    }
)
"""Roles whose recordings depend on state created while recording, or that stream (no starters)."""

_TYPOGRAPHY: Final = str.maketrans(
    {
        0x2018: "'",  # left single quotation mark
        0x2019: "'",  # right single quotation mark
        0x201C: "'",  # left double quotation mark
        0x201D: "'",  # right double quotation mark
        0x2013: "-",  # en dash
        0x2014: "-",  # em dash
        0x2026: "...",  # horizontal ellipsis
        0x00D7: "x",  # multiplication sign
        0x00A0: " ",  # no-break space
    }
)
"""Typographic characters spelled in ASCII (docstrings must not hold ambiguous characters)."""

Mode = Literal["generated", "owned", "notes"]

_NOTES: Final = re.compile(
    r"(<!-- fauxcall:notes\b[^\n]*-->\n)(.*?)(<!-- /fauxcall:notes -->)", re.DOTALL
)
"""The author's region of a generated README (start marker, content, end marker)."""


# -- Python source helpers ------------------------------------------------------------------------


def py_string(text: str) -> str:
    r"""A Python string literal for ``text``, spelled the way ``ruff format`` leaves it alone.

    Double quotes unless the text holds more double than single quotes (then single quotes, which
    need fewer escapes); backslashes, the chosen quote and control characters escaped; every
    non-ASCII character escaped (``\xe9``, ``\u....``) so no ambiguous character reaches source.
    """
    quote = "'" if text.count('"') > text.count("'") else '"'
    escapes = {"\\": "\\\\", quote: "\\" + quote, "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char in escapes:
            out.append(escapes[char])
        elif code < 0x20 or code == 0x7F or 0x7F < code <= 0xFF:  # noqa: PLR2004 - byte ranges
            out.append(f"\\x{code:02x}")
        elif code > 0xFFFF:  # noqa: PLR2004
            out.append(f"\\U{code:08x}")
        elif code > 0xFF:  # noqa: PLR2004
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return quote + "".join(out) + quote


def _py_number(value: float) -> str:
    """A float as ``ruff format`` spells it (``1e20``, not ``1e+20``)."""
    text = repr(float(value))
    if "e" in text:
        mantissa, exponent = text.split("e")
        text = f"{mantissa.removesuffix('.0')}e{exponent.removeprefix('+')}"
    return text


def py_literal(value: JsonValue) -> str:
    """A JSON value as a one-line Python literal (``true`` -> ``True``, ``null`` -> ``None``)."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _py_number(value)
    if isinstance(value, str):
        return py_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(py_literal(item) for item in value) + "]"
    return "{" + ", ".join(f"{py_string(k)}: {py_literal(v)}" for k, v in value.items()) + "}"


def overlong(line: str) -> bool:
    """Whether ruff's E501 flags ``line`` (so it needs ``# noqa: E501``).

    Mirrors ruff's exemptions: a line that is a single "word" (no whitespace, e.g. a bare path
    literal) or that ends with a URL starting within the limit is never reported, and a ``noqa`` on
    such a line would itself be an error (RUF100).
    """
    if len(line) <= LINE_LIMIT:
        return False
    chunks = line.split()
    if len(chunks) < 2:  # noqa: PLR2004 - "a single word"
        return False
    last = chunks[-1]
    return not ("://" in last and len(line) - len(last) <= LINE_LIMIT)


def _noqa(line: str) -> str:
    """``line`` with ``# noqa: E501`` when even ``ruff format`` cannot make it fit."""
    return f"{line}  # noqa: E501" if overlong(line) else line


@dataclass(frozen=True, slots=True)
class Atom:
    """Source code that is never split (a name, or an expression known to be short)."""

    code: str


type Argument = Atom | JsonValue


def _inline(argument: Argument) -> str:
    return argument.code if isinstance(argument, Atom) else py_literal(argument)


def value_lines(argument: Argument, indent: str, prefix: str = "", suffix: str = "") -> list[str]:
    """``prefix + argument + suffix`` at ``indent``, exploded like ``ruff format`` when too long.

    Containers that do not fit open on the first line and hold one item per line, each with a
    (magic) trailing comma, recursively. Scalars and atoms are never split; when they cannot fit the
    line gets ``# noqa: E501``.
    """
    line = f"{indent}{prefix}{_inline(argument)}{suffix}"
    if len(line) <= LINE_LIMIT or not isinstance(argument, dict | list) or not argument:
        return [_noqa(line)]
    inner = indent + INDENT
    if isinstance(argument, dict):
        lines = [f"{indent}{prefix}{{"]
        for key, item in argument.items():
            lines += value_lines(item, inner, f"{py_string(key)}: ", ",")
        lines.append(f"{indent}}}{suffix}")
        return lines
    lines = [f"{indent}{prefix}["]
    for item in argument:
        lines += value_lines(item, inner, "", ",")
    lines.append(f"{indent}]{suffix}")
    return lines


def call_lines(indent: str, head: str, arguments: Sequence[Argument]) -> list[str]:
    """``head(arguments)`` on one line, or one argument per line (trailing comma) when too long."""
    line = f"{indent}{head}(" + ", ".join(_inline(argument) for argument in arguments) + ")"
    if len(line) <= LINE_LIMIT:
        return [line]
    lines = [f"{indent}{head}("]
    for argument in arguments:
        lines += value_lines(argument, indent + INDENT, suffix=",")
    lines.append(f"{indent})")
    return lines


def import_line(module: str, names: Sequence[str]) -> str:
    """``from module import a, b``, one name per line (like ``ruff format``) when too long."""
    line = f"from {module} import {', '.join(names)}"
    if len(line) <= LINE_LIMIT:
        return line
    return f"from {module} import (\n" + "".join(f"{INDENT}{name},\n" for name in names) + ")"


def doc_text(text: str, width: int) -> str:
    """``text`` made safe for a one-line docstring, at most ``width`` characters.

    Whitespace is collapsed; backslashes and double quotes (which could end the docstring) are
    replaced; typographic characters become ASCII and accents are dropped (``café`` -> ``cafe``);
    other non-ASCII text is left out. Too long: cut and ended with ``...``.
    """
    flat = " ".join(text.split()).translate(_TYPOGRAPHY).replace("\\", "/").replace('"', "'")
    ascii_only = "".join(
        char
        for char in unicodedata.normalize("NFKD", flat)
        if char.isascii() and char.isprintable()
    )
    flat = " ".join(ascii_only.split())
    if len(flat) > width:
        flat = flat[: width - 3].rstrip() + "..."
    return flat


def toml_string(text: str) -> str:
    """A one-line TOML basic string."""
    return json.dumps(" ".join(text.split()), ensure_ascii=False).replace("\x7f", "\\u007f")


def markdown_text(text: str) -> str:
    """Text for one line of markdown (also safe inside table cells)."""
    return " ".join(text.split()).replace("|", "\\|")


def identifier(base: str, taken: set[str], *, prefix: str = "op_", limit: int = MAX_NAME) -> str:
    """A unique, lowercase ASCII Python identifier derived from ``base`` (recorded in ``taken``).

    ``findPetsByStatus`` -> ``find_pets_by_status``; keywords, builtins and the generated modules'
    own names get ``prefix``; long names are cut; collisions get ``_2``, ``_3`` …
    """
    folded = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", folded)
    snake = re.sub(r"[^0-9A-Za-z]+", "_", snake).strip("_").lower() or "endpoint"
    if (
        snake[0].isdigit()
        or keyword.iskeyword(snake)
        or snake in _RESERVED
        or hasattr(builtins, snake)
    ):
        snake = f"{prefix}{snake}"
    snake = snake[:limit].rstrip("_")
    name, counter = snake, 2
    while name in taken:
        suffix = f"_{counter}"
        name, counter = f"{snake[: limit - len(suffix)].rstrip('_')}{suffix}", counter + 1
    taken.add(name)
    return name


# -- routes ---------------------------------------------------------------------------------------


def fauxcall_requirement(source: str | None) -> tuple[str, str | None]:
    """The dependency spec for ``fauxcall`` and, for a local checkout, its path.

    ``source`` (``FAUXCALL_SOURCE``) is a local checkout (a directory), a version specifier
    (``">=0.2"``) or a full requirement (``"fauxcall==0.2.1"``); by default the fake depends on the
    running version's compatible range.

    Raises:
        ConfigurationError: ``source`` looks like a path but is not a directory.
    """
    if source:
        if source.startswith(("==", ">", "<", "!=", "~=")):
            return f"fauxcall{source}", None
        candidate = Path(source).expanduser() if source.startswith("~/") else Path(source)
        if candidate.is_dir():
            return "fauxcall", str(candidate.resolve())
        if source.startswith(("/", "~", ".")):
            raise ConfigurationError(f"FAUXCALL_SOURCE {source!r} is not a directory")
        return source, None
    major, minor, *_ = [*__version__.split("."), "0", "0"]
    if major == "0":
        return f"fauxcall>={__version__},<0.{int(minor) + 1}", None
    return f"fauxcall>={__version__},<{int(major) + 1}", None


def checkout_reference(local_path: str | None, workspace_root: Path) -> str | None:
    """How the workspace refers to a local fauxcall checkout.

    Relative when the workspace lives inside the checkout (example fakes in the repository stay
    usable wherever it is cloned), absolute otherwise.
    """
    if local_path is None:
        return None
    checkout = Path(local_path).resolve()
    root = workspace_root.resolve()
    if root.is_relative_to(checkout):
        return Path(os.path.relpath(checkout, root)).as_posix()
    return local_path


def with_extra(requirement: str, extra: str) -> str:
    """``requirement`` with ``extra`` added to fauxcall's extras (``fauxcall[testing]>=0.1``)."""
    match = re.match(
        r"^\s*fauxcall(?:\s*\[([^\]]*)\])?(?=\s*(?:$|[=<>!~@;(,]))(.*)$",
        requirement,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return f"fauxcall[{extra}]"
    extras = [name.strip() for name in (match.group(1) or "").split(",") if name.strip()]
    if extra not in extras:
        extras.append(extra)
    return f"fauxcall[{','.join(extras)}]{match.group(2)}"


def specificity(endpoint: EndpointModel) -> tuple[int, int, str]:
    """Sort key: more literal characters first, then fewer parameters (the real routers' rule)."""
    literal = len(_PARAM.sub("", endpoint.path))
    params = len(_PARAM.findall(endpoint.path))
    return (-literal, params, endpoint.path)


def route_path(path: str) -> str:
    """A path FastAPI can route: parameters become identifiers (``{pet-id}`` -> ``{pet_id}``)."""
    return _PARAM.sub(lambda m: "{" + param_identifier(m.group(1)) + "}", path)


def param_identifier(name: str) -> str:
    """A parameter name as a Python/Starlette identifier (shared with the runtime)."""
    return route_identifier(name)


@dataclass(frozen=True, slots=True)
class Route:
    """One generated route."""

    decorator: str
    path: str
    function: str
    doc: str
    endpoint_id: str

    @property
    def websocket(self) -> bool:
        """Whether this is a WebSocket route."""
        return self.decorator == "websocket"

    @property
    def path_line(self) -> str:
        """The decorator's path argument line, with the ``noqa`` codes it needs."""
        line = f"{INDENT}{py_string(self.path)},"
        codes = ["E501"] if overlong(line) else []
        if not self.websocket and _PARAM.search(self.path):
            codes.append("FAST003")  # parameters are parsed by the engine, not the signature
        return f"{line}  # noqa: {', '.join(codes)}" if codes else line

    def block(self) -> str:
        """The decorated function, preceded by the two blank lines that separate definitions."""
        lines = ["", "", f"@router.{self.decorator}(", self.path_line]
        lines.append(f"{INDENT}name={py_string(self.function)},")
        if self.websocket:
            lines += [")", f"async def {self.function}(websocket: WebSocket) -> None:"]
        else:
            lines.append(f"{INDENT}include_in_schema=False,")
            lines += [")", f"async def {self.function}(request: Request) -> Response:"]
        lines.append(f'{INDENT}"""{self.doc}"""')
        if self.websocket:
            lines += call_lines(
                INDENT, "await _serve_websocket", [Atom("websocket"), self.endpoint_id]
            )
        else:
            lines += call_lines(INDENT, "return await _serve", [Atom("request"), self.endpoint_id])
        return "\n".join(lines)


def _doc(endpoint: EndpointModel) -> str:
    """``METHOD /path: summary.`` as a one-line docstring that fits on its line."""
    budget = LINE_LIMIT - len(f'{INDENT}"""') - len('"""')
    summary = doc_text(endpoint.summary or "", budget).rstrip(". ")
    text = f"{endpoint.method.value} {endpoint.path}" + (f": {summary}" if summary else "")
    text = doc_text(text, budget - 1)
    return text if text.endswith(".") else f"{text}."


def routes_imports(package: str, routes: Sequence[Route]) -> str:
    """The import block of ``routes.py``: only what its routes use."""
    http = any(not route.websocket for route in routes)
    websocket = any(route.websocket for route in routes)
    fastapi = ["APIRouter", *(["Request", "Response"] if http else [])]
    fastapi += ["WebSocket"] if websocket else []
    runtime = [*(["handle"] if http else []), *(["handle_websocket"] if websocket else [])]
    custom = [*(["HANDLERS"] if http else []), *(["WEBSOCKET_HANDLERS"] if websocket else [])]
    block = [import_line("fastapi", fastapi)]
    if runtime:
        block.append(import_line("fauxcall.runtime", runtime))
        block.append("")
        block.append(import_line(f"{package}.handlers", custom))
    return "\n".join(block)


def routes_for(model: ApiModel) -> list[Route]:
    """Routes for every REST endpoint, most specific first."""
    taken: set[str] = set()
    routes: list[Route] = []
    for endpoint in sorted(model.endpoints, key=specificity):
        websocket = endpoint.role == "websocket"
        base = endpoint.operation_id or f"{endpoint.method.value}_{endpoint.path}"
        routes.append(
            Route(
                decorator="websocket" if websocket else endpoint.method.value.lower(),
                path=route_path(endpoint.path),
                function=identifier(base, taken),
                doc=_doc(endpoint),
                endpoint_id=endpoint.id,
            )
        )
    return routes


# -- files ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScaffoldResult:
    """What a scaffold run did."""

    written: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self, root: Path) -> str:
        """Readable list of changes (paths relative to ``root`` when they are inside it)."""
        lines = [f"- wrote `{_display(p, root)}`" for p in self.written]
        lines += [f"- kept `{_display(p, root)}` (yours)" for p in self.kept]
        lines += [f"- {note}" for note in self.notes]
        return "\n".join(lines)


def _display(path: Path, root: Path) -> str:
    resolved, base = path.resolve(), root.resolve()
    return str(resolved.relative_to(base) if resolved.is_relative_to(base) else path)


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 - generating Python/TOML/Markdown, not HTML
    )
    env.filters["doc"] = doc_text
    env.filters["toml"] = toml_string
    env.filters["md"] = markdown_text
    return env


def _write(target: Path, text: str, mode: Mode, result: ScaffoldResult) -> None:
    if mode == "owned" and target.exists():
        result.kept.append(target)
        return
    if mode == "notes" and target.exists():
        current = _NOTES.search(target.read_text(encoding="utf-8"))
        if current is None:  # from before notes regions: whatever was added there is the author's
            result.kept.append(target)
            return
        text = _NOTES.sub(lambda fresh: fresh[1] + current[2] + fresh[3], text, count=1)
    atomic_write_text(target, text)
    result.written.append(target)


def _configured_port(fake: FakeLayout) -> int | None:
    config = fake.root / "fauxcall.toml"
    if not config.exists():
        return None
    try:
        port = tomllib.loads(config.read_text(encoding="utf-8")).get("port")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def separate_ports(workspace: Workspace, current: str | None = None) -> list[str]:
    """Give fakes that share a configured port their own; returns what moved, for the summary.

    Ports are workspace state (``fauxcall up`` and compose bind them all at once). Fakes built in
    different workspaces and put together can clash: the fake being scaffolded (``current``)
    yields to the others, which may already be in use; among the others the first by name keeps
    the port. A fake that moves gets the lowest free port, changing only the ``port`` line of its
    ``fauxcall.toml``.
    """
    configured = {
        fake.name: (fake, port)
        for fake in workspace.fakes()
        if (port := _configured_port(fake)) is not None
    }
    in_use = {port for _, port in configured.values()}
    kept: set[int] = set()
    moved: list[str] = []
    for name in sorted(configured, key=lambda name: (name == current, name)):
        fake, port = configured[name]
        if port not in kept:
            kept.add(port)
            continue
        free = FIRST_PORT
        while free in in_use:
            free += 1
        if _set_port(fake.root / "fauxcall.toml", free):
            in_use.add(free)
            kept.add(free)
            moved.append(
                f"{name}: port {port} was another fake's; it now uses {free} (fauxcall.toml)"
            )
        else:
            moved.append(
                f"{name}: port {port} is another fake's too: change `port` in fauxcall.toml"
            )
    return moved


def _set_port(config: Path, port: int) -> bool:
    """Rewrite the top-level ``port = …`` line (before any table); False when there is none."""
    text = config.read_text(encoding="utf-8")
    table = re.search(r"^\[", text, re.MULTILINE)
    head, rest = (text[: table.start()], text[table.start() :]) if table else (text, "")
    updated, count = re.subn(
        r"^port\s*=\s*\d+[ \t]*$", f"port = {port}", head, count=1, flags=re.MULTILINE
    )
    if count != 1:
        return False
    atomic_write_text(config, updated + rest)
    return True


def assign_port(workspace: Workspace, layout: FakeLayout) -> int:
    """The fake's port: kept if already configured, else the lowest free one from 8701."""
    used: dict[str, int] = {}
    for fake in workspace.fakes():
        port = _configured_port(fake)
        if port is not None:
            used[fake.name] = port
    if layout.name in used:
        return used[layout.name]
    taken = set(used.values())
    port = FIRST_PORT
    while port in taken:
        port += 1
    return port


def credential_lines(model: ApiModel) -> list[str]:
    """``.env.example`` lines for the credentials the real API expects (one per setting)."""
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    for scheme in model.auth.schemes if model.auth is not None else []:
        name = scheme.name or "api_key"
        if scheme.kind == "api_key":
            if scheme.location == "query":
                query.setdefault(name, "your-api-key")
            elif scheme.location == "cookie":
                headers.setdefault("Cookie", f"{name}=your-api-key")
            else:
                headers.setdefault(name, "your-api-key")
        elif scheme.kind in {"bearer", "oauth2"}:
            headers.setdefault("Authorization", "Bearer your-token")
        elif scheme.kind == "basic":
            headers.setdefault("Authorization", "Basic base64(user:password)")
    lines = []
    if headers:
        lines.append(f"FAUXCALL_REAL__HEADERS='{json.dumps(headers)}'")
    if query:
        lines.append(f"FAUXCALL_REAL__QUERY='{json.dumps(query)}'")
    return lines


_HTTP_OK: Final = 200

AGENT_TOKEN: Final = "any-token"  # noqa: S105 - a placeholder, not a credential
"""The credential examples tell agents to send: a fake in auth mode ``any`` takes any value."""


def mcp_server_config(model: ApiModel, port: int) -> dict[str, object]:
    """An MCP client's entry for the fake (``.mcp.json``): its URL and any headers it needs."""
    path = model.mcp.transport.path if model.mcp else "/mcp"
    entry: dict[str, object] = {"type": "http", "url": f"http://127.0.0.1:{port}{path}"}
    headers = agent_headers(model)
    if headers:
        entry["headers"] = headers
    return entry


def agent_headers(model: ApiModel) -> dict[str, str]:
    """Headers an agent sends the fake when the real service wants credentials in headers."""
    headers: dict[str, str] = {}
    for scheme in model.auth.schemes if model.auth is not None else []:
        if scheme.kind in {"bearer", "oauth2"}:
            headers.setdefault("Authorization", f"Bearer {AGENT_TOKEN}")
        elif scheme.kind == "basic":
            headers.setdefault("Authorization", "Basic YW55OnRva2Vu")  # any:token
        elif scheme.kind == "api_key" and scheme.location == "header" and scheme.name:
            headers.setdefault(scheme.name, AGENT_TOKEN)
    return headers


def _auth_query_names(model: ApiModel) -> set[str]:
    schemes = model.auth.schemes if model.auth is not None else []
    return {s.name or "" for s in schemes if s.kind == "api_key" and s.location == "query"}


def _scenario_target(model: ApiModel, exchange: Exchange) -> list[str] | None:
    """The calls of a starter scenario for one recorded exchange (``None``: not a good starter)."""
    request = exchange.request
    if model.kind == "mcp":
        sent = request.body.parsed()
        if not isinstance(sent, dict) or sent.get("method") != "tools/call":
            return None
        assert exchange.response is not None  # noqa: S101 - checked by the caller
        reply = exchange.response.body.parsed()
        if exchange.response.status != _HTTP_OK or not isinstance(reply, dict):
            return None  # refused over HTTP (a 403 …): not a starter for a result
        if "result" not in reply:
            return None  # a JSON-RPC error: call_tool would return the error, not a result
        params = sent.get("params")
        params = params if isinstance(params, dict) else {}
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        name = str(params.get("name", ""))
        call = call_lines(INDENT, "result = await mcp.call_tool", [name, arguments])
        return [*call, f"{INDENT}assert result is not None"]
    endpoint = model.find_endpoint(exchange.endpoint)
    if endpoint is None or endpoint.role in SCENARIO_SKIPPED_ROLES or request.method.value != "GET":
        return None
    assert exchange.response is not None  # noqa: S101 - checked by the caller
    skip = _auth_query_names(model)
    query = [(k, v) for k, v in request.query if k not in skip and REDACTED not in v]
    url = request.path + (f"?{urlencode(query)}" if query else "")
    call = call_lines(INDENT, "response = await api.get", [url])
    return [*call, f"{INDENT}assert response.status_code == {exchange.response.status}"]


def scenarios_for(
    model: ApiModel, exchanges: Sequence[Exchange], limit: int = 12
) -> list[dict[str, str]]:
    """Starter e2e scenarios from recorded read calls (the agent writes many more).

    One per endpoint, from ``sample``/``variation`` recordings: GETs of stateless REST endpoints
    (the recorded path and query, credentials left out) or MCP ``tools/call`` requests (the
    recorded arguments).
    """
    seen: set[str] = set()
    taken: set[str] = set()
    scenarios: list[dict[str, str]] = []
    for exchange in exchanges:
        if (
            exchange.response is None
            or exchange.purpose not in {"sample", "variation"}
            or exchange.endpoint in seen
        ):
            continue
        body = _scenario_target(model, exchange)
        if body is None:
            continue
        seen.add(exchange.endpoint)
        name = identifier(exchange.endpoint, taken, prefix="", limit=MAX_NAME - 14)
        scenarios.append({"name": f"recorded_{name}", "body": "\n".join(body)})
        if len(scenarios) >= limit:
            break
    return scenarios


def _members_table(text: str) -> tuple[int, int] | None:
    """Start and end offsets of the ``[tool.uv.workspace]`` table body."""
    header = re.search(r"(?m)^\[tool\.uv\.workspace\][ \t]*(?:#.*)?$", text)
    if header is None:
        return None
    following = re.search(r"(?m)^\[", text[header.end() :])
    end = header.end() + following.start() if following else len(text)
    return header.end(), end


def _update_members(pyproject: Path, member: str) -> bool:
    """Add ``member`` to the workspace's ``members`` (sorted); returns whether the file changed."""
    text = pyproject.read_text(encoding="utf-8")
    workspace = tomllib.loads(text).get("tool", {}).get("uv", {}).get("workspace", {})
    members = workspace.get("members", [])
    if member in members:
        return False
    rendered = "members = [" + ", ".join(f'"{m}"' for m in sorted({*members, member})) + "]"
    table = _members_table(text)
    if table is None:
        new_text = text.rstrip() + f"\n\n[tool.uv.workspace]\n{rendered}\n"
    else:
        start, end = table
        body, count = re.subn(r"(?m)^members\s*=\s*\[[^\]]*\]", rendered, text[start:end], count=1)
        if count == 0:
            body = f"\n{rendered}{body}"
        new_text = text[:start] + body + text[end:]
    atomic_write_text(pyproject, new_text)
    return True


def _workflow_dir(workspace: Workspace, project_root: Path) -> str:
    """The workspace as the CI sees it: relative to the project root (else just its name)."""
    root, base = workspace.root.resolve(), project_root.resolve()
    if root.is_relative_to(base) and root != base:
        return root.relative_to(base).as_posix()
    return workspace.root.name if root != base else "."


def scaffold(
    layout: FakeLayout,
    workspace: Workspace,
    model: ApiModel,
    *,
    exchanges: Sequence[Exchange] = (),
    project_root: Path | None = None,
    source: str | None = None,
) -> ScaffoldResult:
    """Create/refresh the fake's package and the workspace files."""
    env = _environment()
    result = ScaffoldResult(notes=separate_ports(workspace, layout.name))
    requirement, local_path = fauxcall_requirement(source)
    port = assign_port(workspace, layout)
    routes = routes_for(model) if model.kind == "rest" else []
    context: dict[str, object] = {
        "name": layout.name,
        "title": model.title,
        "package": layout.package,
        "port": port,
        "mcp": model.kind == "mcp",
        "mcp_path": model.mcp.transport.path if model.mcp else "/mcp",
        "real_base_url": model.real.base_url,
        "source_url": model.source.url,
        "fauxcall_requirement": requirement,
        "testing_requirement": with_extra(requirement, "testing"),
        "routes": routes,
        "http_routes": any(not route.websocket for route in routes),
        "websockets": any(route.websocket for route in routes),
        "imports": routes_imports(layout.package, routes),
        "credential_lines": credential_lines(model),
        "endpoint_count": len(model.endpoints),
        "tool_count": len(model.mcp.tools) if model.mcp else 0,
        "resource_count": len(model.mcp.resources) if model.mcp else 0,
        "prompt_count": len(model.mcp.prompts) if model.mcp else 0,
        "has_world": bool(model.collections) or (layout.data / "world").is_dir(),
        "agent_headers": agent_headers(model),
        "mcp_config": json.dumps(
            {"mcpServers": {layout.name: mcp_server_config(model, port)}}, indent=2
        )
        if model.kind == "mcp"
        else "",
        "scenarios": scenarios_for(model, exchanges),
        "fixture": "mcp" if model.kind == "mcp" else "api",
        "fixture_type": "McpClient" if model.kind == "mcp" else "ApiClient",
    }
    fake_files: list[tuple[str, Path, Mode]] = [
        ("fake/pyproject.toml.j2", layout.root / "pyproject.toml", "owned"),
        ("fake/README.md.j2", layout.root / "README.md", "notes"),
        ("fake/fauxcall.toml.j2", layout.root / "fauxcall.toml", "owned"),
        ("fake/env.example.j2", layout.root / ".env.example", "generated"),
        ("fake/Dockerfile.j2", layout.root / "Dockerfile", "owned"),
        ("fake/src/__init__.py.j2", layout.src / "__init__.py", "owned"),
        ("fake/src/__main__.py.j2", layout.src / "__main__.py", "owned"),
        ("fake/src/settings.py.j2", layout.src / "settings.py", "owned"),
        ("fake/src/app.py.j2", layout.src / "app.py", "owned"),
        ("fake/src/routes.py.j2", layout.src / "routes.py", "generated"),
        ("fake/src/handlers.py.j2", layout.src / "handlers.py", "owned"),
        ("fake/tests/conftest.py.j2", layout.tests / "conftest.py", "owned"),
        ("fake/tests/unit/test_app.py.j2", layout.tests / "unit" / "test_app.py", "owned"),
        ("fake/tests/unit/test_routes.py.j2", layout.tests / "unit" / "test_routes.py", "owned"),
        (
            "fake/tests/unit/test_recorded.py.j2",
            layout.tests / "unit" / "test_recorded.py",
            "owned",
        ),
        (
            "fake/tests/e2e/test_scenarios.py.j2",
            layout.tests / "e2e" / "test_scenarios.py",
            "owned",
        ),
    ]
    for template, target, mode in fake_files:
        _write(target, env.get_template(template).render(**context), mode, result)
    _write_workspace(env, workspace, layout, local_path, result)
    root = project_root if project_root is not None else workspace.root
    workspace_context = {
        "fakes": _workspace_fakes(workspace, layout, model, port),
        "workspace_dir": _workflow_dir(workspace, root),
    }
    for template, target in (
        ("workspace/README.md.j2", workspace.root / "README.md"),
        ("workspace/compose.yaml.j2", workspace.root / "compose.yaml"),
    ):
        _write(target, env.get_template(template).render(**workspace_context), "generated", result)
    if project_root is not None:
        workflow = project_root / ".github" / "workflows" / "fauxcall.yml"
        text = env.get_template("workspace/ci.yml.j2").render(**workspace_context)
        _write(workflow, text, "generated", result)
    return result


def _write_workspace(
    env: jinja2.Environment,
    workspace: Workspace,
    layout: FakeLayout,
    local_path: str | None,
    result: ScaffoldResult,
) -> None:
    pyproject = workspace.root / "pyproject.toml"
    if pyproject.exists():
        if _update_members(pyproject, layout.name):
            result.written.append(pyproject)
        else:
            result.kept.append(pyproject)
    else:
        text = env.get_template("workspace/pyproject.toml.j2").render(
            members=[layout.name], fauxcall_path=checkout_reference(local_path, workspace.root)
        )
        _write(pyproject, text, "owned", result)
    _write(
        workspace.root / ".gitignore",
        env.get_template("workspace/gitignore.j2").render(),
        "owned",
        result,
    )
    # Images are built from the workspace root: secrets stay out of them, always.
    _write(
        workspace.root / ".dockerignore",
        env.get_template("workspace/dockerignore.j2").render(),
        "generated",
        result,
    )


def _workspace_fakes(
    workspace: Workspace, current: FakeLayout, model: ApiModel, port: int
) -> list[dict[str, object]]:
    """Every scaffolded fake of the workspace (name, title, port), sorted by name."""
    from fauxcall.model.api import ApiModel as ApiModelType  # noqa: PLC0415
    from fauxcall.storage import read_model  # noqa: PLC0415

    entries: dict[str, dict[str, object]] = {}
    for fake in workspace.fakes():
        if not (fake.root / "pyproject.toml").exists():
            continue  # not scaffolded yet: no package to run, build or test
        title = fake.name
        try:
            title = read_model(fake.model_path, ApiModelType).title
        except (OSError, ValueError):
            title = fake.name
        entries[fake.name] = {
            "name": fake.name,
            "title": title,
            "port": assign_port(workspace, fake),
        }
    entries[current.name] = {"name": current.name, "title": model.title, "port": port}
    return [entries[name] for name in sorted(entries)]
