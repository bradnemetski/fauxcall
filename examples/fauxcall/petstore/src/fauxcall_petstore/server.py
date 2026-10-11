"""What Jersey/Jetty do around the Petstore's resources (recorded after review, 000426-000431).

- ``HEAD`` on a GET resource answers like GET without a body (000429).
- ``OPTIONS`` answers 200 with a generated WADL document and an ``Allow`` header (000430). The
  ``Allow`` order (HEAD, the resource's methods in reverse, OPTIONS) is taken from that one sample.
- The framework's own 404/405 errors are ``ApiError`` XML when ``Accept`` is exactly
  ``application/xml`` (000426, 000428); FauxCall renders them as JSON, so they are re-spelled here.
- Every response carries the contract headers (CORS, ``Server``), errors and spec files included.
- ``/api/v3/openapi.yaml`` serves the YAML spec as recorded (000408).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from fastapi import Request, Response
from fauxcall.runtime.app import runtime_of_app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI

ADMIN_PREFIX = "/__fauxcall"
WADL = "application/vnd.sun.wadl+xml"
_REPRESENTATION = """
                <response>
                    <representation mediaType="*/*"/>
                </response>"""


def _template_regex(template: str) -> re.Pattern[str]:
    parts = [re.escape(p) if not p.startswith("{") else "[^/]+" for p in template.split("/")]
    return re.compile("^" + "/".join(parts) + "/?$")


def _wadl(host: str, path: str, methods: list[str]) -> str:
    resource = path.removeprefix("/api/").rstrip("/")
    blocks = "".join(
        f'\n            <method id="apply" name="{m}">{_REPRESENTATION}\n            </method>'
        for m in methods
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<application xmlns="http://wadl.dev.java.net/2009/02">\n'
        '    <doc xmlns:jersey="http://jersey.java.net/" '
        'jersey:generatedBy="Jersey: 2.40 2023-06-22 08:11:52"/>\n'
        "    <grammars/>\n"
        f'    <resources base="http://{escape(host)}/api/">\n'
        f'        <resource path="{escape(resource)}">{blocks}\n'
        "        </resource>\n"
        "    </resources>\n"
        "</application>\n"
    )


def install(app: FastAPI) -> None:
    """Add the server-level behavior to the fake's app."""
    runtime = runtime_of_app(app)
    model = runtime.model
    defaults = {k.lower(): v for k, v in model.default_headers.items()}
    resources: dict[str, list[str]] = {}
    for endpoint in model.endpoints:
        resources.setdefault(endpoint.path, []).append(endpoint.method)
    patterns = [(_template_regex(path), methods) for path, methods in resources.items()]
    yaml_spec = runtime.settings.data_dir / "source" / "openapi.yaml"

    async def openapi_yaml(_request: Request) -> Response:
        return Response(content=yaml_spec.read_bytes(), media_type="application/yaml")

    app.add_api_route(
        "/api/v3/openapi.yaml", openapi_yaml, methods=["GET"], include_in_schema=False
    )

    def methods_for(path: str) -> list[str] | None:
        return next((methods for pattern, methods in patterns if pattern.match(path)), None)

    @app.middleware("http")
    async def jersey(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith(ADMIN_PREFIX):
            return await call_next(request)
        methods = methods_for(path)
        if request.method == "OPTIONS" and methods is not None:
            response = _options(request, path, methods)
        elif request.method == "HEAD" and methods is not None and "GET" in methods:
            request.scope["method"] = "GET"
            full = await call_next(request)
            body = b"".join([chunk async for chunk in full.body_iterator])  # type: ignore[attr-defined]
            headers = dict(full.headers)
            headers["content-length"] = str(len(body))
            response = Response(status_code=full.status_code, headers=headers)
        else:
            response = await _negotiated(request, await call_next(request))
        for name, value in defaults.items():
            if name not in response.headers:
                response.headers[name] = value
        return response


def _options(request: Request, path: str, methods: list[str]) -> Response:
    host = request.headers.get("host", "petstore3.swagger.io")
    allow = ",".join(["HEAD", *reversed(methods), "OPTIONS"])
    modified = datetime.now(tz=UTC).strftime("%a, %d %b %Y %H:%M:%S UTC")
    return Response(
        content=_wadl(host, path, methods).encode(),
        media_type=WADL,
        headers={"allow": allow, "last-modified": modified},
    )


async def _negotiated(request: Request, response: Response) -> Response:
    """Re-spell the framework's JSON 404/405 as ``ApiError`` XML when XML was asked for."""
    accept = request.headers.getlist("accept")
    if not accept or accept[0] != "application/xml" or response.status_code not in {404, 405}:
        return response
    if not response.headers.get("content-type", "").startswith("application/json"):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
    headers = {k: v for k, v in response.headers.items() if k != "content-length"}
    try:
        error = json.loads(body)
        code, message = error["code"], error["message"]
    except (ValueError, KeyError, TypeError):
        return Response(content=body, status_code=response.status_code, headers=headers)
    xml = f"<ApiError><code>{code}</code><message>{escape(str(message))}</message></ApiError>"
    headers["content-type"] = "application/xml"
    return Response(content=xml.encode(), status_code=response.status_code, headers=headers)
