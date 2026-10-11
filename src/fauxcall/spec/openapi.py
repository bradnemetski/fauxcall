"""OpenAPI 2.0 (Swagger) / 3.0 / 3.1 documents → FauxCall model skeletons.

:class:`OpenApiDocument` wraps a parsed spec: its version, internal ``$ref`` resolution, where the
real API lives (:meth:`~OpenApiDocument.real_target`) and its operations.
:func:`endpoints_from_spec` and :func:`auth_from_spec` turn it into
:class:`~fauxcall.model.EndpointModel` and :class:`~fauxcall.model.AuthModel` skeletons. Roles are
decided later, from recorded evidence; :func:`suggest_role` offers a first guess and
:func:`summarize` renders a compact overview the agent reads before planning recordings.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Final, Literal, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

from fauxcall.errors import SpecError
from fauxcall.model.api import RealTarget
from fauxcall.model.auth import AuthModel, AuthScheme, OAuthModel
from fauxcall.model.endpoints import (
    EndpointModel,
    EndpointRole,
    ParamLocation,
    ParamModel,
    RequestBodyModel,
    ResponseVariant,
)
from fauxcall.spec.loader import media_type
from fauxcall.types import HttpMethod, JsonValue

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

HTTP_METHODS: Final = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
"""Path-item keys that are operations (OpenAPI 2.0-3.1)."""

MAX_REF_EXPANSIONS: Final = 1000
"""``$ref``s expanded per :meth:`OpenApiDocument.resolve` call; the rest stay references."""

JSON: Final = "application/json"
_FORM_URLENCODED: Final = "application/x-www-form-urlencoded"
_MULTIPART: Final = "multipart/form-data"
_PATH_ITEM_FIELDS: Final = frozenset({"$ref", "summary", "description", "servers", "parameters"})
_LOCATIONS: Final[dict[str, ParamLocation]] = {
    "path": "path",
    "query": "query",
    "header": "header",
    "cookie": "cookie",
}
_API_KEY_LOCATIONS: Final[dict[str, Literal["header", "query", "cookie"]]] = {
    "header": "header",
    "query": "query",
    "cookie": "cookie",
}
_IGNORED_HEADERS: Final = frozenset({"accept", "content-type", "authorization"})
_JSON_SCHEMA_TYPES: Final = frozenset(
    {"string", "number", "integer", "boolean", "array", "object", "null"}
)
_SWAGGER2_TYPE_FIXES: Final = {
    "int": "integer",
    "long": "integer",
    "float": "number",
    "double": "number",
    "bool": "boolean",
    "str": "string",
}
_SWAGGER2_SCHEMA_KEYS: Final = (
    "format",
    "enum",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
)
_COLLECTION_FORMATS: Final[dict[str, tuple[str, bool]]] = {
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "multi": ("form", True),
}
_STATUS_RE: Final = re.compile(r"[1-5]\d\d")
_VARIABLE_RE: Final = re.compile(r"\{([^{}]+)\}")
_VERSION_SEGMENT_RE: Final = re.compile(r"v\d+(?:\.\d+)*", re.IGNORECASE)
_SEARCH_VERB_RE: Final = re.compile(r"search|find", re.IGNORECASE)
_ACTION_VERB_RE: Final = re.compile(
    r"search|find|create|update|delete|remove|add|set|get|list|upload|download", re.IGNORECASE
)
"""Segments like ``createWithList`` or ``getUser`` name an action, not a collection."""
_SEARCH_PARAMS: Final = frozenset({"q", "query", "search"})
_COLLECTION_ROLES: Final = frozenset(
    {"list", "get", "create", "replace", "update", "delete", "search"}
)
_BINARY_TYPES: Final = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
    }
)
_BINARY_FAMILIES: Final = ("image/", "audio/", "video/", "font/")
_NO_BODY_STATUSES: Final = frozenset({204, 205, 304})


class Operation(NamedTuple):
    """One operation of a spec, as written (its ``$ref``s are not resolved)."""

    path: str
    method: HttpMethod
    operation: dict[str, JsonValue]
    path_item: dict[str, JsonValue]


class _Missing(Enum):
    TOKEN = 0


_MISSING: Final = _Missing.TOKEN


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _text(value: JsonValue) -> str | None:
    """A non-empty, stripped string, or ``None``."""
    return (value.strip() or None) if isinstance(value, str) else None


def _strings(value: JsonValue) -> list[str]:
    return [item for item in _as_list(value) if isinstance(item, str)]


def _version_text(value: JsonValue) -> str:
    return value.strip() if isinstance(value, str) else str(value)


def _spec_version(spec: Mapping[str, JsonValue]) -> str:
    if "openapi" in spec:
        version = _version_text(spec["openapi"])
        if version.startswith("3."):
            return version
        raise SpecError(
            f"unsupported OpenAPI version {version!r}: FauxCall reads OpenAPI 2.0 (Swagger), "
            "3.0 and 3.1"
        )
    if "swagger" in spec:
        version = _version_text(spec["swagger"])
        if version in {"2", "2.0"}:
            return "2.0"
        raise SpecError(f"unsupported Swagger version {version!r}: only Swagger 2.0 is supported")
    raise SpecError("not an OpenAPI/Swagger document: it has no 'openapi' or 'swagger' field")


def _pointer(document: JsonValue, pointer: str) -> JsonValue | _Missing:
    """Follow a JSON pointer (RFC 6901, possibly percent-encoded as in a URI fragment)."""
    if not pointer:
        return document
    if not pointer.startswith("/"):
        return _MISSING  # e.g. "#Pet": a named anchor, not a pointer
    node = document
    for raw in pointer[1:].split("/"):
        token = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return _MISSING
    return node


def _note(notes: list[str], text: str) -> None:
    if text not in notes:
        notes.append(text)


class _Resolver:
    """One resolution pass: expands internal ``$ref``s, cutting cycles and runaway expansion."""

    def __init__(self, root: dict[str, JsonValue], notes: list[str]) -> None:
        self.root = root
        self.notes = notes
        self.expansions = 0

    def resolve(self, node: JsonValue, stack: tuple[str, ...]) -> JsonValue:
        if isinstance(node, list):
            return [self.resolve(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not isinstance(ref, str):
            return {key: self.resolve(value, stack) for key, value in node.items()}
        siblings = {key: self.resolve(value, stack) for key, value in node.items() if key != "$ref"}
        target = self.target(ref, stack)
        if target is _MISSING:
            return {"$ref": ref, **siblings}
        resolved = self.resolve(target, (*stack, ref))
        if isinstance(resolved, dict) and siblings:
            return {**resolved, **siblings}  # OpenAPI 3.1: summary/description override
        return resolved

    def target(self, ref: str, stack: tuple[str, ...]) -> JsonValue | _Missing:
        if not ref.startswith("#"):
            _note(self.notes, f"external $ref {ref!r} is not resolved")
            return _MISSING
        if ref in stack:
            return _MISSING  # a cycle: keep the reference instead of recursing forever
        if self.expansions >= MAX_REF_EXPANSIONS:
            _note(self.notes, f"stopped expanding $refs after {MAX_REF_EXPANSIONS} (huge schema)")
            return _MISSING
        target = _pointer(self.root, ref[1:])
        if target is _MISSING:
            _note(self.notes, f"$ref {ref!r} points at nothing; left as-is")
            return _MISSING
        self.expansions += 1
        return target


def _normalize_base_path(path: str) -> str:
    segments = [segment for segment in path.split("/") if segment and segment != "."]
    return "/" + "/".join(segments) if segments else ""


def _expand_server_variables(url: str, variables: JsonValue) -> str:
    known = _as_dict(variables)

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        variable = _as_dict(known.get(name))
        default = variable.get("default")
        if default is not None:
            return str(default)
        choices = _as_list(variable.get("enum"))
        if choices:
            return str(choices[0])
        raise SpecError(f"server URL {url!r} uses {{{name}}} but the spec gives it no default")

    return _VARIABLE_RE.sub(substitute, url)


def _http_url(url: str | None) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


class OpenApiDocument:
    """A parsed OpenAPI 2.0 / 3.x document and where it came from.

    Args:
        spec: The parsed document (see :func:`fauxcall.spec.loader.parse_spec_text`).
        spec_url: Where the document was fetched from; relative server URLs resolve against it.

    Attributes:
        version: ``"2.0"`` for Swagger, else the ``openapi`` field as written (``"3.1.2"``).
        notes: Document-level findings: extra servers, unresolvable references, skipped items.

    Raises:
        SpecError: The document is not OpenAPI 2.0 or 3.x.
    """

    def __init__(self, spec: Mapping[str, JsonValue], *, spec_url: str | None) -> None:
        self.spec: dict[str, JsonValue] = dict(spec)
        self.spec_url = spec_url
        self.version: str = _spec_version(self.spec)
        self.notes: list[str] = []
        self._location: tuple[str | None, str] | None = None
        webhooks = _as_dict(self.spec.get("webhooks"))
        if webhooks:
            _note(self.notes, f"webhooks (calls the API makes; not faked): {', '.join(webhooks)}")

    @property
    def is_swagger2(self) -> bool:
        """Whether this is a Swagger 2.0 document."""
        return self.version == "2.0"

    @property
    def title(self) -> str | None:
        """``info.title``."""
        return _text(_as_dict(self.spec.get("info")).get("title"))

    @property
    def api_version(self) -> str | None:
        """``info.version`` (the API's version, not the spec format's)."""
        version = _as_dict(self.spec.get("info")).get("version")
        return None if version is None else _version_text(version) or None

    # -- references ----------------------------------------------------------------------------

    def resolve(self, node: JsonValue, *, notes: list[str] | None = None) -> JsonValue:
        """A copy of ``node`` with internal ``$ref``s replaced by what they point at.

        * JSON pointers are unescaped (``~1`` → ``/``, ``~0`` → ``~``, percent-encoding);
        * a ``$ref`` that would recurse into itself (a cycle) is kept as ``{"$ref": ...}``;
        * external (``other.yaml#/...``) and dangling refs are kept as they are, and noted;
        * keys next to a ``$ref`` (OpenAPI 3.1 ``summary``/``description``) override the target's;
        * at most :data:`MAX_REF_EXPANSIONS` refs are expanded per call.

        Args:
            node: Any part of the document (or the whole of it).
            notes: Where to record problems; defaults to :attr:`notes`.
        """
        return _Resolver(self.spec, self.notes if notes is None else notes).resolve(node, ())

    # -- location ------------------------------------------------------------------------------

    def real_target(self) -> RealTarget:
        """Where the real API lives.

        OpenAPI 3: the first server, its variables set to their defaults, resolved against
        :attr:`spec_url` when relative (``"/api/v3"`` served from ``petstore3.swagger.io`` →
        ``https://petstore3.swagger.io`` + ``/api/v3``). Swagger 2: ``schemes`` (https preferred) +
        ``host`` + ``basePath``. No servers: the origin of :attr:`spec_url`.

        Raises:
            SpecError: The location is relative and :attr:`spec_url` is not an http(s) URL.
        """
        base_url, base_path = self._server_location()
        if base_url is None:
            raise SpecError(
                "cannot tell where the real API lives: the spec gives only a relative location "
                f"(base path {base_path or '/'!r}) and it was not fetched over HTTP; "
                "provide the API's base URL"
            )
        return RealTarget(base_url=base_url, base_path=base_path)

    @property
    def base_path(self) -> str:
        """The prefix of every endpoint path (``""`` for none); known even without a spec URL."""
        return self._server_location()[1]

    def endpoint_id(self, method: HttpMethod, path: str) -> str:
        """The FauxCall endpoint id of an operation: ``"GET /api/v3/pet/{petId}"``."""
        return f"{method.value} {self.base_path}{path}"

    def _server_location(self) -> tuple[str | None, str]:
        if self._location is None:
            if self.is_swagger2:
                self._location = self._swagger2_location()
            else:
                self._location = self._openapi3_location()
        return self._location

    def _openapi3_location(self) -> tuple[str | None, str]:
        servers = [
            server
            for server in _as_list(self.spec.get("servers"))
            if isinstance(server, dict) and isinstance(server.get("url"), str)
        ]
        if not servers:
            return self._locate("/")
        first = servers[0]
        if len(servers) > 1:
            others = ", ".join(str(server["url"]) for server in servers[1:])
            _note(self.notes, f"using the first of {len(servers)} servers; others: {others}")
        return self._locate(_expand_server_variables(str(first["url"]), first.get("variables")))

    def _locate(self, server_url: str) -> tuple[str | None, str]:
        parts = urlsplit(server_url)
        if not parts.scheme and _http_url(self.spec_url):
            parts = urlsplit(urljoin(str(self.spec_url), server_url or "/"))
        if parts.netloc:
            scheme = (parts.scheme or "https").lower()
            return f"{scheme}://{parts.netloc.lower()}", _normalize_base_path(parts.path)
        return None, _normalize_base_path(parts.path)

    def _swagger2_location(self) -> tuple[str | None, str]:
        spec_parts = urlsplit(self.spec_url or "")
        known = _http_url(self.spec_url)
        host = _text(self.spec.get("host")) or (spec_parts.netloc if known else None)
        schemes = [scheme.lower() for scheme in _strings(self.spec.get("schemes"))]
        if "https" in schemes or not schemes:
            scheme = spec_parts.scheme if known and not schemes else "https"
        else:
            scheme = schemes[0]
        base_path = _normalize_base_path(_text(self.spec.get("basePath")) or "/")
        return (f"{scheme}://{host.lower()}" if host else None), base_path

    # -- contents ------------------------------------------------------------------------------

    def operations(self) -> Iterator[Operation]:
        """Every operation in ``paths``, in document order (path-item ``$ref``s resolved)."""
        for path, raw_item in _as_dict(self.spec.get("paths")).items():
            item = raw_item
            if isinstance(raw_item, dict) and "$ref" in raw_item:
                item = self.resolve(raw_item)
            if not isinstance(item, dict):
                continue
            for key, operation in item.items():
                if key.lower() in HTTP_METHODS and isinstance(operation, dict):
                    yield Operation(path, HttpMethod(key.upper()), operation, item)
                elif key not in _PATH_ITEM_FIELDS and not key.startswith("x-"):
                    _note(self.notes, f"{path}: unsupported operation {key!r} skipped")

    def security_schemes(self) -> dict[str, dict[str, JsonValue]]:
        """Security schemes by name (``components.securitySchemes`` / ``securityDefinitions``)."""
        if self.is_swagger2:
            raw = self.spec.get("securityDefinitions")
        else:
            raw = _as_dict(self.spec.get("components")).get("securitySchemes")
        schemes: dict[str, dict[str, JsonValue]] = {}
        for name, scheme in _as_dict(raw).items():
            resolved = self.resolve(scheme)
            if isinstance(resolved, dict):
                schemes[name] = resolved
        return schemes


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _summary(operation: dict[str, JsonValue]) -> str | None:
    summary = _text(operation.get("summary"))
    if summary is not None:
        return summary
    description = _text(operation.get("description")) or ""
    return next((line.strip() for line in description.splitlines() if line.strip()), None)


def _json_rank(content_type: str) -> int:
    media = media_type(content_type)
    if media == JSON:
        return 0
    if media.endswith("+json"):
        return 1
    return 2 if "json" in media else 3


def _preferred_content_type(content_types: Sequence[str]) -> str | None:
    """The most JSON-like content type (``application/json`` first), else the first one."""
    return min(content_types, key=_json_rank) if content_types else None


def _schema_default(schema: JsonValue) -> JsonValue:
    schema = _as_dict(schema)
    if "default" in schema:
        return schema["default"]
    for member in _as_list(schema.get("allOf")):
        default = _schema_default(member)
        if default is not None:
            return default
    return None


def _media_schema(content: JsonValue) -> tuple[str | None, JsonValue]:
    """The preferred media type of a ``content`` map and its schema."""
    media = _as_dict(content)
    chosen = _preferred_content_type(list(media))
    if chosen is None:
        return None, None
    return chosen, _as_dict(media[chosen]).get("schema")


class _EndpointBuilder:
    """Turns one operation into an :class:`EndpointModel`, collecting notes along the way."""

    def __init__(self, doc: OpenApiDocument, op: Operation) -> None:
        self.doc = doc
        self.op = op
        self.notes: list[str] = []

    def resolve(self, node: JsonValue) -> JsonValue:
        return self.doc.resolve(node, notes=self.notes)

    def build(self) -> EndpointModel:
        operation = self.op.operation
        path = self.doc.base_path + self.op.path
        params, body_params = self.parameters()
        if self.doc.is_swagger2:
            request_body = self.swagger2_body(body_params)
        else:
            request_body = self.request_body()
        responses = self.responses()
        self.extra_notes()
        successes = [r.status for r in responses if 200 <= r.status < 300]  # noqa: PLR2004
        return EndpointModel(
            id=f"{self.op.method.value} {path}",
            method=self.op.method,
            path=path,
            spec_path=self.op.path,
            operation_id=_text(operation.get("operationId")),
            summary=_summary(operation),
            tags=_strings(operation.get("tags")),
            params=params,
            request_body=request_body,
            responses=responses,
            success_status=min(successes, default=200),
            deprecated=operation.get("deprecated") is True,
            notes=self.notes,
        )

    # -- parameters ----------------------------------------------------------------------------

    def parameters(self) -> tuple[list[ParamModel], list[dict[str, JsonValue]]]:
        """Path-item parameters merged with the operation's (which win on name + location)."""
        merged: dict[tuple[str, str], dict[str, JsonValue]] = {}
        for source in (self.op.path_item.get("parameters"), self.op.operation.get("parameters")):
            for raw in _as_list(source):
                param = _as_dict(self.resolve(raw))
                name, location = param.get("name"), param.get("in")
                if isinstance(name, str) and isinstance(location, str):
                    merged[name, location] = param
                elif "$ref" not in param:  # unresolvable refs are noted already
                    self.notes.append("a parameter without 'name' and 'in' was skipped")
        params: list[ParamModel] = []
        body_params: list[dict[str, JsonValue]] = []
        for (name, location), param in merged.items():
            known = _LOCATIONS.get(location)
            if self.doc.is_swagger2 and location in {"body", "formData"}:
                body_params.append(param)
            elif known is None:
                self.notes.append(
                    f"parameter {name!r} in unsupported location {location!r} skipped"
                )
            elif (
                known == "header" and not self.doc.is_swagger2 and name.lower() in _IGNORED_HEADERS
            ):
                self.notes.append(
                    f"header parameter {name!r} ignored (OpenAPI leaves it to content "
                    "negotiation and auth)"
                )
            else:
                params.append(self.param_model(name, known, param))
        return params, body_params

    def param_model(
        self, name: str, location: ParamLocation, param: dict[str, JsonValue]
    ) -> ParamModel:
        style, explode = _text(param.get("style")), param.get("explode")
        if self.doc.is_swagger2 and "schema" not in param:
            schema = self.swagger2_schema(param, name)
            default = param.get("default")
            if style is None:
                style, explode = self.collection_format(name, location, param, explode)
        else:
            schema = (
                param["schema"] if "schema" in param else _media_schema(param.get("content"))[1]
            )
            default = _schema_default(schema)
        return ParamModel(
            name=name,
            location=location,
            description=_text(param.get("description")),
            required=location == "path" or param.get("required") is True,
            param_schema=schema,
            style=style,
            explode=explode if isinstance(explode, bool) else None,
            default=default,
        )

    def swagger2_schema(self, item: dict[str, JsonValue], name: str) -> JsonValue:
        """A JSON schema for a Swagger 2.0 non-body parameter (or one of its ``items``)."""
        raw_type = item.get("type")
        if not isinstance(raw_type, str):
            return None
        schema: dict[str, JsonValue] = {}
        if raw_type == "file":
            schema.update(type="string", format="binary")
        elif raw_type not in _JSON_SCHEMA_TYPES and raw_type.lower() in _SWAGGER2_TYPE_FIXES:
            schema["type"] = _SWAGGER2_TYPE_FIXES[raw_type.lower()]
            self.notes.append(
                f"parameter {name!r} declares non-standard type {raw_type!r} "
                f"(read as {schema['type']!r})"
            )
        else:
            schema["type"] = raw_type
        for key in _SWAGGER2_SCHEMA_KEYS:
            if key in item and key not in schema:
                schema[key] = item[key]
        items = item.get("items")
        if isinstance(items, dict):
            schema["items"] = self.swagger2_schema(items, name)
        return schema

    def collection_format(
        self, name: str, location: ParamLocation, param: dict[str, JsonValue], explode: JsonValue
    ) -> tuple[str | None, JsonValue]:
        """Swagger 2.0 ``collectionFormat`` as an OpenAPI 3 style/explode pair."""
        if param.get("type") != "array":
            return None, explode
        fmt = _text(param.get("collectionFormat")) or "csv"
        if fmt == "csv":
            return ("simple" if location in {"path", "header"} else "form"), False
        if fmt in _COLLECTION_FORMATS:
            return _COLLECTION_FORMATS[fmt]
        self.notes.append(f"parameter {name!r}: collectionFormat {fmt!r} has no OpenAPI 3 style")
        return None, explode

    # -- request bodies ------------------------------------------------------------------------

    def request_body(self) -> RequestBodyModel | None:
        raw = self.op.operation.get("requestBody")
        if raw is None:
            return None
        body = _as_dict(self.resolve(raw))
        content = _as_dict(body.get("content"))
        _, schema = _media_schema(content)
        return RequestBodyModel(
            content_types=list(content) or [JSON],
            body_schema=schema,
            required=body.get("required") is True,
        )

    def swagger2_body(self, params: list[dict[str, JsonValue]]) -> RequestBodyModel | None:
        consumes = _strings(self.op.operation.get("consumes")) or _strings(
            self.doc.spec.get("consumes")
        )
        body = next((param for param in params if param.get("in") == "body"), None)
        if body is not None:
            return RequestBodyModel(
                content_types=consumes or [JSON],
                body_schema=body.get("schema"),
                required=body.get("required") is True,
            )
        form = [param for param in params if param.get("in") == "formData"]
        if not form:
            return None
        properties: dict[str, JsonValue] = {}
        required: list[JsonValue] = []
        for param in form:
            name = str(param.get("name"))
            properties[name] = self.swagger2_schema(param, name) or {}
            if param.get("required") is True:
                required.append(name)
        schema: dict[str, JsonValue] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        has_file = any(param.get("type") == "file" for param in form)
        form_types = [ct for ct in consumes if media_type(ct) in {_FORM_URLENCODED, _MULTIPART}]
        return RequestBodyModel(
            content_types=form_types or [_MULTIPART if has_file else _FORM_URLENCODED],
            body_schema=schema,
            required=bool(required),
        )

    # -- responses -----------------------------------------------------------------------------

    def responses(self) -> list[ResponseVariant]:
        variants: list[ResponseVariant] = []
        for key, raw in _as_dict(self.op.operation.get("responses")).items():
            response = _as_dict(self.resolve(raw))
            description = _text(response.get("description"))
            if not _STATUS_RE.fullmatch(key):
                detail = f": {description}" if description else ""
                self.notes.append(f"documented {key!r} response{detail} (not a single status)")
                continue
            status = int(key)
            if self.doc.is_swagger2:
                content_type, schema = self.swagger2_content(status, response)
            else:
                content_type, schema = _media_schema(response.get("content"))
            headers = _as_dict(response.get("headers"))
            if headers:
                self.notes.append(f"{status} response documents headers: {', '.join(headers)}")
            variants.append(
                ResponseVariant(
                    status=status,
                    description=description,
                    content_type=content_type,
                    documented_schema=schema,
                )
            )
        return variants

    def swagger2_content(
        self, status: int, response: dict[str, JsonValue]
    ) -> tuple[str | None, JsonValue]:
        schema = response.get("schema")
        is_success = 200 <= status < 300 and status not in _NO_BODY_STATUSES  # noqa: PLR2004
        if schema is None and not is_success:
            return None, None
        produces = _strings(self.op.operation.get("produces")) or _strings(
            self.doc.spec.get("produces")
        )
        if produces:
            return _preferred_content_type(produces), schema
        return (JSON if schema is not None else None), schema

    def extra_notes(self) -> None:
        operation, item = self.op.operation, self.op.path_item
        servers = _as_list(operation.get("servers")) or _as_list(item.get("servers"))
        urls = [str(s["url"]) for s in servers if isinstance(s, dict) and "url" in s]
        if urls:
            self.notes.append(f"operation-specific servers: {', '.join(urls)}")
        callbacks = _as_dict(operation.get("callbacks"))
        if callbacks:
            self.notes.append(f"callbacks (not faked): {', '.join(callbacks)}")


def endpoints_from_spec(doc: OpenApiDocument) -> list[EndpointModel]:
    """One endpoint skeleton per operation, in document order.

    ``id`` is ``"<METHOD> <base path><path>"``; ``role`` is ``"replay"`` (roles are decided later
    from evidence; :func:`suggest_role` offers a first guess). Parameters are merged from the path
    item and the operation, ``$ref``s are resolved, and Swagger 2.0 ``body``/``formData``
    parameters become the request body. Anything without a model field — ``default`` and ``2XX``
    responses, documented response headers, callbacks, per-operation servers, unresolvable
    references — is listed in the endpoint's ``notes``.
    """
    return [_EndpointBuilder(doc, op).build() for op in doc.operations()]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _oauth_scopes(scheme: dict[str, JsonValue]) -> list[str]:
    containers = [scheme.get("scopes")]  # Swagger 2.0
    containers += [_as_dict(flow).get("scopes") for flow in _as_dict(scheme.get("flows")).values()]
    scopes: list[str] = []
    for container in containers:
        for scope in _as_dict(container):
            if scope not in scopes:
                scopes.append(scope)
    return scopes


def _auth_scheme(scheme: dict[str, JsonValue]) -> AuthScheme | None:
    kind = str(scheme.get("type", "")).lower()
    http_scheme = str(scheme.get("scheme", "")).lower()
    if kind == "apikey":
        location = _API_KEY_LOCATIONS.get(str(scheme.get("in")))
        name = _text(scheme.get("name"))
        if location is None or name is None:
            return None
        return AuthScheme(kind="api_key", location=location, name=name)
    if kind == "basic" or (kind == "http" and http_scheme == "basic"):
        return AuthScheme(kind="basic", prefix="Basic ")
    if kind == "http" and http_scheme == "bearer":
        return AuthScheme(kind="bearer", prefix="Bearer ")
    if kind in {"oauth2", "openidconnect"}:
        return AuthScheme(
            kind="oauth2", prefix="Bearer ", oauth=OAuthModel(scopes=_oauth_scopes(scheme))
        )
    return None


def _requires_credentials(security: JsonValue) -> bool:
    """Whether a ``security`` value demands credentials (``[]`` or a ``{}`` alternative: no)."""
    requirements = _as_list(security)
    return bool(requirements) and all(isinstance(r, dict) and r for r in requirements)


def auth_from_spec(doc: OpenApiDocument) -> AuthModel | None:
    """The API's authentication skeleton, or ``None`` if it declares no usable scheme.

    Schemes: ``apiKey`` (header/query/cookie), HTTP ``bearer`` (prefix ``"Bearer "``) and
    ``basic``, ``oauth2``/``openIdConnect`` (kind ``oauth2``, scopes collected from every flow).
    ``required_for`` is ``["*"]`` when the top-level ``security`` demands credentials — operations
    opting out with ``security: []`` (or an empty ``{}`` alternative) are then ``exempt`` — and
    otherwise lists the operations whose own ``security`` demands them.
    """
    schemes: list[AuthScheme] = []
    for scheme in doc.security_schemes().values():
        converted = _auth_scheme(scheme)
        if converted is not None and converted not in schemes:
            schemes.append(converted)
    if not schemes:
        return None
    globally = _requires_credentials(doc.spec.get("security"))
    required_for: list[str] = ["*"] if globally else []
    exempt: list[str] = []
    for op in doc.operations():
        if "security" not in op.operation:
            continue
        endpoint_id = doc.endpoint_id(op.method, op.path)
        needed = _requires_credentials(op.operation["security"])
        if needed and not globally:
            required_for.append(endpoint_id)
        elif not needed and globally:
            exempt.append(endpoint_id)
    return AuthModel(schemes=schemes, required_for=required_for, exempt=exempt)


# ---------------------------------------------------------------------------
# Role suggestions
# ---------------------------------------------------------------------------


def _is_parameter(segment: str) -> bool:
    return "{" in segment


def _starts_with_verb(segment: str, verbs: re.Pattern[str]) -> bool:
    """``search``, ``findByStatus``, ``find_by_tags`` — but not ``searches`` or ``finders``."""
    match = verbs.match(segment)
    return match is not None and not segment[match.end() : match.end() + 1].islower()


def _is_search_segment(segment: str) -> bool:
    return _starts_with_verb(segment, _SEARCH_VERB_RE)


def _collection_name(path: str) -> str | None:
    """The last path segment that names things (not a parameter, version, ``api`` or action)."""
    for segment in reversed([s for s in path.split("/") if s]):
        if _is_parameter(segment) or _starts_with_verb(segment, _ACTION_VERB_RE):
            continue
        if _VERSION_SEGMENT_RE.fullmatch(segment) or segment.lower() == "api":
            continue
        return segment.lower().replace("-", "_")
    return None


def _is_binary(content_type: str | None) -> bool:
    media = media_type(content_type)
    return media in _BINARY_TYPES or media.startswith(_BINARY_FAMILIES)


def _schema_types(schema: dict[str, JsonValue]) -> list[str]:
    value = schema.get("type")
    return [value] if isinstance(value, str) else _strings(value)


def _is_listish(schema: dict[str, JsonValue]) -> bool:
    """An array, or an object with an array property (an envelope like ``{"data": [...]}``)."""
    if "array" in _schema_types(schema) or "items" in schema:
        return True
    members = [_as_dict(m) for m in _as_list(schema.get("allOf"))]
    properties = [
        _as_dict(p) for m in [schema, *members] for p in _as_dict(m.get("properties")).values()
    ]
    return any("array" in _schema_types(p) for p in properties) or any(map(_is_listish, members))


def _success_variant(endpoint: EndpointModel) -> ResponseVariant | None:
    variants = [r for r in endpoint.responses if 200 <= r.status < 300]  # noqa: PLR2004
    return next((r for r in variants if r.status == endpoint.success_status), None) or next(
        iter(variants), None
    )


def _looks_like_list(endpoint: EndpointModel, all_endpoints: Sequence[EndpointModel]) -> bool:
    """A GET on a collection path lists, unless its documented body is clearly not a list."""
    success = _success_variant(endpoint)
    schema = success.documented_schema if success is not None else None
    if not isinstance(schema, dict) or not schema or _is_listish(schema):
        return True
    prefix = (endpoint.spec_path or endpoint.path).rstrip("/") + "/"
    return any(
        other.method == HttpMethod.GET
        and (other.spec_path or other.path).startswith(prefix)
        and _is_parameter((other.spec_path or other.path).rstrip("/").rsplit("/", 1)[-1])
        for other in all_endpoints
    )


def suggest_role(
    endpoint: EndpointModel, all_endpoints: Sequence[EndpointModel]
) -> tuple[EndpointRole, str | None]:
    """A first guess at an endpoint's role and collection, for the agent to confirm from evidence.

    In order: a ``text/event-stream`` response → ``sse``; a multipart or binary request body →
    ``upload``; a binary success response (octet-stream, images, PDFs …) → ``download``. Then by
    method, with the collection named by the last path segment that is not a parameter (lowercased,
    kebab → snake; version segments, ``api`` and search verbs are skipped): GET with a
    ``search``/``find`` segment or a ``q``/``query``/``search`` parameter → ``search``; GET
    ``/things/{id}`` → ``get``; GET ``/things`` → ``list`` (unless its documented body is clearly
    not a list and no ``/things/{id}`` exists among ``all_endpoints``: then ``replay``); POST →
    ``create`` (``update`` on ``/things/{id}``); PUT → ``replace``; PATCH → ``update``; DELETE →
    ``delete``. Anything else → ``("replay", None)``. Only collection roles carry a collection.
    """
    payload_role = _payload_role(endpoint)
    if payload_role is not None:
        return payload_role, None
    path = endpoint.spec_path or endpoint.path
    collection = _collection_name(path)
    if collection is None:
        return "replay", None
    on_item = _is_parameter(path.rstrip("/").rsplit("/", 1)[-1])
    writes: dict[HttpMethod, EndpointRole] = {
        HttpMethod.PUT: "replace",
        HttpMethod.PATCH: "update",
        HttpMethod.DELETE: "delete",
        HttpMethod.POST: "update" if on_item else "create",
    }
    if endpoint.method == HttpMethod.GET:
        role = _get_role(endpoint, all_endpoints, on_item=on_item)
    else:
        role = writes.get(endpoint.method, "replay")
    return role, (collection if role in _COLLECTION_ROLES else None)


def _payload_role(endpoint: EndpointModel) -> EndpointRole | None:
    """``sse``, ``upload`` or ``download`` when the payloads say so."""
    if any(media_type(r.content_type) == "text/event-stream" for r in endpoint.responses):
        return "sse"
    request_types = endpoint.request_body.content_types if endpoint.request_body else []
    if any(media_type(ct).startswith("multipart/") or _is_binary(ct) for ct in request_types):
        return "upload"
    success = _success_variant(endpoint)
    if success is not None and _is_binary(success.content_type):
        return "download"
    return None


def _get_role(
    endpoint: EndpointModel, all_endpoints: Sequence[EndpointModel], *, on_item: bool
) -> EndpointRole:
    path = endpoint.spec_path or endpoint.path
    searching = any(_is_search_segment(s) for s in path.split("/")) or any(
        p.location == "query" and p.name.lower() in _SEARCH_PARAMS for p in endpoint.params
    )
    if searching:
        return "search"
    if on_item:
        return "get"
    return "list" if _looks_like_list(endpoint, all_endpoints) else "replay"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_MAX_ENUM_SHOWN: Final = 6


def _schema_text(schema: JsonValue) -> str:
    """A compact type description: ``integer(int64)``, ``string|null``, ``Pet[]``."""
    schema = _as_dict(schema)
    types = _schema_types(schema)
    text = ""
    if types:
        text = "|".join(types)
    elif isinstance(schema.get("$ref"), str):
        text = str(schema["$ref"]).rsplit("/", 1)[-1]
    for key, joiner in (("allOf", " & "), ("oneOf", " | "), ("anyOf", " | ")):
        members = [m for m in map(_schema_text, _as_list(schema.get(key))) if m]
        if not text and members:
            text = joiner.join(members)
    if text == "array":
        text = f"{_schema_text(schema.get('items')) or 'any'}[]"
    fmt = schema.get("format")
    if isinstance(fmt, str) and text:
        text += f"({fmt})"
    enum = _as_list(schema.get("enum"))
    if enum:
        shown = enum if len(enum) <= _MAX_ENUM_SHOWN else [*enum[:_MAX_ENUM_SHOWN], "…"]
        choices = "{" + "|".join(str(value) for value in shown) + "}"
        text = f"{text} {choices}" if text else choices
    return text


def _param_text(param: ParamModel) -> str:
    text = f"{param.name}{'*' if param.required else ''}: {param.location}"
    schema = _schema_text(param.param_schema)
    if schema:
        text += f" {schema}"
    if param.default is not None:
        text += f" = {param.default}"
    return text


def _body_text(body: RequestBodyModel) -> str:
    chosen = _preferred_content_type(body.content_types) or JSON
    more = f" (+{len(body.content_types) - 1} more)" if len(body.content_types) > 1 else ""
    return f"body{'*' if body.required else ''}: {chosen}{more}"


def _scheme_text(name: str, scheme: dict[str, JsonValue]) -> str:
    kind = str(scheme.get("type", "unknown"))
    if kind.lower() == "apikey":
        return f"{name} (API key in {scheme.get('in')} {scheme.get('name')!r})"
    if kind.lower() == "http":
        detail = (
            f", {scheme['bearerFormat']}" if isinstance(scheme.get("bearerFormat"), str) else ""
        )
        return f"{name} (HTTP {scheme.get('scheme')}{detail})"
    if kind.lower() in {"oauth2", "openidconnect"}:
        scopes = _oauth_scopes(scheme)
        return f"{name} ({kind}" + (f"; scopes: {', '.join(scopes)})" if scopes else ")")
    return f"{name} ({kind})"


def _location_text(doc: OpenApiDocument) -> str:
    try:
        target = doc.real_target()
    except SpecError:
        return f"unknown host (relative server; base path {doc.base_path or '/'})"
    return target.base_url + target.base_path


def summarize(doc: OpenApiDocument, endpoints: Sequence[EndpointModel]) -> str:
    """A compact markdown overview of the API for the agent to read.

    Title, spec version, base URL and auth schemes, then one line per endpoint:
    ``METHOD path — summary [params; body] -> statuses``. Required parameters are starred.
    """
    title = doc.title or "Untitled API"
    lines = [f"# {title}" + (f" (version {doc.api_version})" if doc.api_version else ""), ""]
    lines.append(f"- Spec: {'Swagger' if doc.is_swagger2 else 'OpenAPI'} {doc.version}")
    lines.append(f"- Base URL: {_location_text(doc)}")
    schemes = [_scheme_text(name, scheme) for name, scheme in doc.security_schemes().items()]
    auth = "; ".join(schemes) if schemes else "none documented"
    if schemes and _requires_credentials(doc.spec.get("security")):
        auth += " — required unless an endpoint opts out"
    lines.append(f"- Auth: {auth}")
    lines += ["", f"## Endpoints ({len(endpoints)})", ""]
    documented = {
        (op.method, op.path): list(_as_dict(op.operation.get("responses")))
        for op in doc.operations()
    }
    for endpoint in endpoints:
        statuses = documented.get((endpoint.method, endpoint.spec_path or "")) or [
            str(response.status) for response in endpoint.responses
        ]
        line = f"- `{endpoint.method.value} {endpoint.path}`"
        if endpoint.summary:
            line += f" — {' '.join(endpoint.summary.split())}"
        details = [_param_text(param) for param in endpoint.params]
        if endpoint.request_body is not None:
            details.append(_body_text(endpoint.request_body))
        if details:
            line += f" [{'; '.join(details)}]"
        line += f" -> {', '.join(statuses) or 'no documented responses'}"
        if endpoint.deprecated:
            line += " (deprecated)"
        lines.append(line)
    return "\n".join(lines) + "\n"
