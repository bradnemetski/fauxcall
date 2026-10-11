"""Shared type aliases and small enums used across FauxCall."""

from __future__ import annotations

from enum import StrEnum

type JsonScalar = bool | int | float | str | None
"""A JSON scalar value."""

type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
"""Any JSON value. Objects keep insertion order (it mirrors the real API's key order)."""

type JsonObject = dict[str, JsonValue]
"""A JSON object."""


class HttpMethod(StrEnum):
    """HTTP methods FauxCall understands."""

    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"

    @property
    def is_safe(self) -> bool:
        """Whether the method is safe (read-only) per RFC 9110."""
        return self in {HttpMethod.GET, HttpMethod.HEAD, HttpMethod.OPTIONS, HttpMethod.TRACE}
