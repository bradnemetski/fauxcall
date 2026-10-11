"""The error envelope and every exception handler.

All errors use one shape::

    {"error": {"code": "<snake_code>", "message": "<text>", "request_id": "<uuid4>"}}

with an extra ``"param"`` key (last) for errors about a specific parameter. FastAPI's 422 is
replaced by 400; unknown routes (404) and wrong methods (405, with an accurate ``Allow`` header)
use the same envelope.
"""

from __future__ import annotations

import logging
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Annotated, Final

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, MutableMapping

    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.routing import BaseRoute

logger = logging.getLogger(__name__)

METHOD_ORDER: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

PARAM_RULES: Final[Mapping[str, str]] = {
    "count": "must be an integer between 1 and 50",
    "file": "must be a file",
    "id": "must be an integer",
    "label": "must be a string",
    "limit": "must be an integer between 1 and 100",
    "name": "must be a string of 1 to 50 characters",
    "q": "must be a string of 2 to 64 characters",
    "symbols": "must be an array of strings",
}
"""The message suffix for an invalid value, per parameter (any validation failure)."""

_HTTP_ERROR_CODES: Final[Mapping[int, str]] = {
    HTTPStatus.BAD_REQUEST: "bad_request",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
}
_PARAM_SOURCES: Final = frozenset({"path", "query", "header", "cookie", "body"})


class ErrorDetail(BaseModel):
    """The body of an error."""

    code: str = Field(description="Stable, machine-readable snake_case error code.")
    message: str = Field(description="Human-readable explanation (deterministic text).")
    request_id: str = Field(description="UUID4 identifying this request.")
    param: Annotated[
        str | SkipJsonSchema[None],
        Field(description="The offending parameter; present only for parameter errors."),
    ] = None


class ErrorEnvelope(BaseModel):
    """Every non-2xx response body."""

    error: ErrorDetail


class ApiError(Exception):
    """An error that renders as the envelope with the given status."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        param: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.headers = dict(headers or {})


def new_request_id() -> str:
    """A fresh UUID4 string."""
    return str(uuid.uuid4())


def request_id_of(scope: MutableMapping[str, object]) -> str:
    """The request id the gateway stored in the ASGI scope (or a fresh one)."""
    state = scope.get("state")
    if isinstance(state, dict):
        request_id = state.get("request_id")
        if isinstance(request_id, str):
            return request_id
    return new_request_id()


def envelope(
    code: str, message: str, request_id: str, param: str | None = None
) -> dict[str, dict[str, str]]:
    """Build the error envelope (``param`` only when given)."""
    error = {"code": code, "message": message, "request_id": request_id}
    if param is not None:
        error["param"] = param
    return {"error": error}


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    request_id: str,
    param: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render the envelope as a JSON response."""
    return JSONResponse(
        envelope(code, message, request_id, param), status_code=status, headers=headers
    )


def describe_validation_error(error: Mapping[str, object]) -> tuple[str, str, str | None]:
    """Map one pydantic/FastAPI validation error to ``(code, message, param)``."""
    error_type = error.get("type")
    location = error.get("loc")
    loc = tuple(location) if isinstance(location, (list, tuple)) else ()
    if error_type == "json_invalid":
        return "invalid_json", "Request body is not valid JSON.", None
    if loc[:1] == ("body",) and len(loc) == 1:
        return "invalid_body", "Request body must be a JSON object.", None
    parts = loc[1:] if loc[:1] and loc[0] in _PARAM_SOURCES else loc
    param = str(parts[0]) if parts else "request"
    if error_type == "missing":
        return "missing_parameter", f"Missing required parameter '{param}'.", param
    rule = PARAM_RULES.get(param)
    detail = f": {rule}" if rule else ""
    return "invalid_parameter", f"Invalid value for parameter '{param}'{detail}.", param


def allowed_methods(routes: Iterable[BaseRoute], path: str) -> list[str]:
    """Every method some API route accepts for ``path``, in a canonical order."""
    methods: set[str] = set()
    for route in routes:
        if isinstance(route, APIRoute) and route.path_regex.match(path):
            methods |= route.methods or set()
    return sorted(methods, key=lambda method: (METHOD_ORDER.index(method), method))


async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    """Render an :class:`ApiError`."""
    return error_response(
        exc.status,
        exc.code,
        exc.message,
        request_id=request_id_of(request.scope),
        param=exc.param,
        headers=exc.headers,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render the first validation error as a 400 envelope."""
    errors = exc.errors()
    first: Mapping[str, object] = errors[0] if errors else {}
    code, message, param = describe_validation_error(first)
    return error_response(
        HTTPStatus.BAD_REQUEST,
        code,
        message,
        request_id=request_id_of(request.scope),
        param=param,
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render routing errors (404, 405) and other framework HTTP errors as envelopes."""
    method = request.method
    path = request.url.path
    headers = dict(exc.headers or {})
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    if exc.status_code == HTTPStatus.NOT_FOUND:
        message = f"No route matches {method} {path}."
    elif exc.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
        message = f"Method {method} is not allowed for {path}."
        routes: Iterable[BaseRoute] = getattr(request.app.state, "api_routes", ())
        headers["Allow"] = ", ".join(allowed_methods(routes, path))
    else:
        message = str(exc.detail)
    return error_response(
        exc.status_code, code, message, request_id=request_id_of(request.scope), headers=headers
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Render any other exception as a 500 envelope (and log it)."""
    logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "internal_error",
        "An unexpected error occurred.",
        request_id=request_id_of(request.scope),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler on ``app``."""
    app.exception_handler(ApiError)(handle_api_error)
    app.exception_handler(RequestValidationError)(handle_validation_error)
    app.exception_handler(StarletteHTTPException)(handle_http_exception)
    app.exception_handler(Exception)(handle_unexpected_error)
