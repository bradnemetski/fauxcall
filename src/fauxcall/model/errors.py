"""Error templates captured from the real API, and when to use them.

Templates may contain ``{{placeholder}}`` markers inside JSON strings or text bodies:

* ``{{uuid}}``, ``{{traceparent}}``, ``{{hex:16}}``, ``{{digits:6}}`` — fresh random values;
* ``{{now:<format>}}`` — the clock; ``<format>`` is ``iso``, ``iso_ms``, ``http_date`` or ``epoch``;
* ``{{method}}``, ``{{path}}``, ``{{url}}`` — from the request;
* ``{{param.<name>}}`` — a path/query parameter value; ``{{value}}`` — the offending value;
* ``{{header.<name>}}`` — a request header.

A JSON string that is exactly one placeholder may render a non-string (e.g. ``{{status}}``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fauxcall.model.common import Model, RawJson

ErrorCondition = Literal[
    "not_found",
    "invalid_param",
    "missing_param",
    "invalid_body",
    "unauthorized",
    "forbidden",
    "expired",
    "insufficient_scope",
    "conflict",
    "method_not_allowed",
    "unknown_route",
    "rate_limited",
    "payload_too_large",
    "unsupported_media_type",
    "missing_reference",
    "still_referenced",
    "custom",
]


class ErrorTemplate(Model):
    """A real error response, generalized with placeholders."""

    name: str
    status: int = Field(ge=100, le=599)
    content_type: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_json: RawJson = None
    body_text: str | None = None
    examples: list[str] = Field(default_factory=list, description="Exchange ids it came from.")

    @model_validator(mode="after")
    def _one_body(self) -> ErrorTemplate:
        if self.body_json is not None and self.body_text is not None:
            raise ValueError("an error template has either body_json or body_text, not both")
        return self


class ErrorCase(Model):
    """Which template an endpoint uses for a given failure."""

    when: ErrorCondition
    template: str
    param: str | None = Field(default=None, description="Restrict to one parameter.")
    note: str | None = None
