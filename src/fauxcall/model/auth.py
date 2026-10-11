"""How the real API authenticates, and the error responses it uses when that fails.

Which *users* the fake accepts is runtime configuration (:class:`fauxcall.config.AuthSettings`),
so the same fake can be run with different allowed/denied users per evaluation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fauxcall.model.common import Model, RawJson

AuthKind = Literal["api_key", "bearer", "basic", "oauth2", "none"]


class OAuthModel(Model):
    """OAuth 2 details.

    For MCP servers the fake runs a built-in, browserless authorization server that serves these
    (rewritten) metadata documents and approves configured users.
    """

    protected_resource_metadata: RawJson = None
    authorization_server_metadata: RawJson = None
    scopes: list[str] = Field(default_factory=list)
    token_ttl_seconds: int = Field(default=3600, ge=1)
    refresh_tokens: bool = True


class AuthScheme(Model):
    """One accepted way to authenticate."""

    kind: AuthKind
    location: Literal["header", "query", "cookie"] | None = None
    name: str | None = Field(default=None, description="Header/query/cookie name for api_key.")
    prefix: str | None = Field(default=None, description="E.g. 'Bearer ' or 'Token '.")
    format_regex: str | None = Field(
        default=None, description="What a well-formed credential looks like (else 'invalid')."
    )
    oauth: OAuthModel | None = None

    @model_validator(mode="after")
    def _complete(self) -> AuthScheme:
        if self.kind == "api_key" and (self.location is None or self.name is None):
            raise ValueError("api_key schemes need a location and a name")
        return self


class AuthModel(Model):
    """Authentication for the whole API."""

    schemes: list[AuthScheme] = Field(default_factory=list)
    required_for: list[str] = Field(default_factory=lambda: ["*"], description="Endpoint globs.")
    exempt: list[str] = Field(default_factory=list, description="Endpoint globs needing no auth.")
    missing_template: str | None = None
    invalid_template: str | None = None
    forbidden_template: str | None = None
    expired_template: str | None = None
    insufficient_scope_template: str | None = None
