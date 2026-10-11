"""Routing quirks of real servers, applied before FastAPI routes the request.

* Case-insensitive paths (common on .NET servers): ``/api/v1/books/1`` is served by the
  ``/api/v1/Books/{id}`` route, exactly like the real API. Literal segments are re-spelled as
  declared; parameter values are kept as sent. An exact (case-sensitive) match always wins, then
  the most specific route (most literal characters), like the route order itself.
* Trailing slashes: ``ignore`` treats ``/pets/`` like ``/pets``; ``redirect`` lets Starlette
  redirect; ``strict`` leaves the path alone (unknown routes get the real 404).

Paths under the admin prefix are never rewritten.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

    from fauxcall.model.api import ApiModel

_PARAM: Final = re.compile(r"\{[^/{}]+\}")
_NOT_IDENTIFIER: Final = re.compile(r"[^A-Za-z0-9_]")
_PATH_SAFE: Final = "/:@!$&'()*+,;="  # RFC 3986 pchar characters kept as-is in raw_path


def route_identifier(name: str) -> str:
    """A path-parameter name as Starlette routes it (``pet-id`` -> ``pet_id``).

    Starlette only recognizes ASCII identifiers in route templates, so every other character
    becomes ``_``; a name that is empty or starts with a digit gets a ``p_`` prefix.
    """
    ident = _NOT_IDENTIFIER.sub("_", name)
    return f"p_{ident}" if not ident or ident[0].isdigit() else ident


def path_value(path_params: Mapping[str, object], name: str) -> object:
    """Look a path parameter up by its spec name or its routed identifier."""
    if name in path_params:
        return path_params[name]
    return path_params.get(route_identifier(name))


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment != ""]


def _specificity(template: str) -> tuple[int, int, str]:
    """Most literal characters first, then fewer parameters (the generated routes' order)."""
    return (-len(_PARAM.sub("", template)), len(_PARAM.findall(template)), template)


def _respell(template: list[str], incoming: list[str], *, fold: bool) -> list[str] | None:
    """``incoming`` spelled like ``template`` (``None`` if it doesn't match)."""
    if len(template) != len(incoming):
        return None
    rebuilt: list[str] = []
    for expected, actual in zip(template, incoming, strict=True):
        if _PARAM.fullmatch(expected):
            rebuilt.append(actual)
        elif expected == actual or (fold and expected.lower() == actual.lower()):
            rebuilt.append(expected)
        else:
            return None
    return rebuilt


class RoutingQuirks:
    """Pure ASGI middleware that rewrites ``scope["path"]`` to the canonical route spelling."""

    def __init__(self, app: ASGIApp, *, model: ApiModel, admin_prefix: str) -> None:
        self.app = app
        self.admin_prefix = admin_prefix
        self.case_insensitive = model.routing.case_insensitive
        self.ignore_trailing = model.routing.trailing_slash == "ignore"
        templates = {endpoint.path for endpoint in model.endpoints} | set(model.routing.spec_paths)
        if model.mcp is not None:
            templates.add(model.mcp.transport.path)
        self._templates = [_segments(t) for t in sorted(templates, key=_specificity)]

    def canonical(self, path: str) -> str:
        """The path re-spelled to match a declared route (unchanged if none matches)."""
        if path.startswith(self.admin_prefix):
            return path
        if self.ignore_trailing and len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        if not self.case_insensitive:
            return path
        incoming = _segments(path)
        for fold in (False, True):
            for template in self._templates:
                rebuilt = _respell(template, incoming, fold=fold)
                if rebuilt is not None:
                    trailing = "/" if path.endswith("/") and path != "/" else ""
                    return "/" + "/".join(rebuilt) + trailing
        return path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite the path for HTTP and WebSocket scopes."""
        if scope["type"] in {"http", "websocket"}:
            path = scope["path"]
            canonical = self.canonical(path)
            if canonical != path:
                raw_path = quote(canonical, safe=_PATH_SAFE).encode("ascii")
                scope = {**scope, "path": canonical, "raw_path": raw_path}
        await self.app(scope, receive, send)
