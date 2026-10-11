"""Authentication: who is calling, and should they get in?

The *scheme* (where the credential goes, what it looks like, which error bodies the real API uses)
comes from the API model. The *users* come from runtime settings, so the same fake can be run with
different allowed / denied / expired users to test how agents handle auth failures.

Outcomes, in order:

* no credential (absent, or present but empty) -> ``missing``;
* a credential that is malformed (wrong prefix, bad Basic encoding, fails ``format_regex``), a
  token or key nobody owns in ``allowlist`` mode, an ``oauth2`` bearer token that the built-in
  authorization server didn't issue (or that was revoked) and no configured user owns, or a
  known Basic username with the wrong password -> ``invalid``;
* an issued token past its lifetime -> ``expired``;
* a configured user -> their ``status`` (``denied`` -> ``forbidden``, ``expired``,
  ``insufficient_scope``);
* any other well-formed static credential (API key, plain bearer token, Basic login) in ``any``
  mode -> an anonymous, allowed identity.

Failures are only reported when the endpoint requires auth; optional-auth endpoints still learn
who is calling.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import functools
import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from fauxcall.logs import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fauxcall.config import AuthSettings, IdentityConfig, IdentityStatus
    from fauxcall.model.auth import AuthModel, AuthScheme

log = get_logger(__name__)

AuthFailure = Literal["missing", "invalid", "forbidden", "expired", "insufficient_scope"]

_STATUS_FAILURE: Final[dict[str, AuthFailure | None]] = {
    "allowed": None,
    "denied": "forbidden",
    "expired": "expired",
    "insufficient_scope": "insufficient_scope",
}
_NS_PER_SECOND: Final = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Identity:
    """An authenticated (or anonymous-but-well-formed) caller."""

    name: str
    status: IdentityStatus
    scopes: tuple[str, ...] = ()
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """Result of checking one request."""

    identity: Identity | None
    failure: AuthFailure | None = None

    @property
    def ok(self) -> bool:
        """Whether the request may proceed."""
        return self.failure is None


@dataclass(frozen=True, slots=True)
class _Credential:
    kind: str
    secret: str
    username: str | None = None
    malformed: bool = False

    @property
    def empty(self) -> bool:
        """Present but blank (``X-API-Key:`` or ``Authorization: Bearer``): counts as missing."""
        return not (self.malformed or self.secret or self.username)


@dataclass(slots=True)
class _IssuedToken:
    identity: str
    expires_ns: int
    scopes: tuple[str, ...]


def _same(left: str, right: str) -> bool:
    """Constant-time comparison that also works for non-ASCII values."""
    return secrets.compare_digest(left.encode(), right.encode())


def _without_prefix(value: str, prefix: str) -> str | None:
    """The credential after its (case-insensitive) prefix.

    ``""`` when nothing follows the prefix (blank, or just ``Bearer``); ``None`` when the value
    does not start with the prefix at all.
    """
    stripped = value.strip()
    if not stripped or stripped.lower() == prefix.strip().lower():
        return ""
    if value.lower().startswith(prefix.lower()):
        return value[len(prefix) :].strip()
    return None


def _api_key(value: str, prefix: str | None) -> _Credential:
    secret = _without_prefix(value, prefix) if prefix else value
    if secret is None:
        return _Credential("api_key", "", malformed=True)  # e.g. "Bearer x" where "Token x" is due
    return _Credential("api_key", secret)


def _basic(authorization: str) -> _Credential | None:
    encoded = _without_prefix(authorization, "Basic ")
    if encoded is None:
        return None  # another scheme's credential
    if not encoded:
        return _Credential("basic", "")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return _Credential("basic", "", malformed=True)
    username, colon, password = decoded.partition(":")
    if not colon or not username:
        return _Credential("basic", "", malformed=True)
    return _Credential("basic", password, username=username)


@functools.lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error as exc:  # e.g. a JavaScript-only construct in a model-authored regex
        log.warning("auth_format_regex_invalid", pattern=pattern, error=str(exc))
        return None


class Authenticator:
    """Checks credentials against the model's schemes and the configured users."""

    def __init__(self, model: AuthModel | None, settings: AuthSettings) -> None:
        self.model = model
        self.settings = settings
        self._tokens: dict[str, _IssuedToken] = {}

    def update_users(self, users: list[IdentityConfig]) -> None:
        """Replace the configured users at runtime."""
        self.settings = self.settings.model_copy(update={"users": list(users)})

    def requires_auth(
        self,
        endpoint_id: str,
        override: bool | None = None,  # noqa: FBT001 - the pipeline passes it positionally
    ) -> bool:
        """Whether this endpoint needs credentials (``override`` is the endpoint's own setting)."""
        if self.settings.mode == "off" or self.model is None or not self.model.schemes:
            return False
        if override is not None:
            return override
        if any(fnmatch.fnmatchcase(endpoint_id, glob) for glob in self.model.exempt):
            return False
        return any(fnmatch.fnmatchcase(endpoint_id, glob) for glob in self.model.required_for)

    # -- credentials ---------------------------------------------------------------------------

    @staticmethod
    def _extract(
        scheme: AuthScheme,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        cookies: Mapping[str, str],
    ) -> _Credential | None:
        """The credential this scheme reads from the request (``headers`` have lowercase names)."""
        authorization = headers.get("authorization")
        match scheme.kind:
            case "api_key":
                name = scheme.name or ""
                sources = {"query": (query, name), "cookie": (cookies, name)}
                source, key = sources.get(scheme.location or "header", (headers, name.lower()))
                value = source.get(key)
                return None if value is None else _api_key(value, scheme.prefix)
            case "bearer" | "oauth2":
                if authorization is None:
                    return None
                token = _without_prefix(authorization, scheme.prefix or "Bearer ")
                return None if token is None else _Credential("bearer", token)
            case "basic":
                return None if authorization is None else _basic(authorization)
            case _:
                return None

    @staticmethod
    def _well_formed(scheme: AuthScheme, credential: _Credential) -> bool:
        if credential.malformed:
            return False
        pattern = _compiled(scheme.format_regex) if scheme.format_regex else None
        return pattern is None or pattern.fullmatch(credential.secret) is not None

    def _match_user(self, credential: _Credential) -> IdentityConfig | None:
        for user in self.settings.users:
            match credential.kind:
                case "api_key" if user.api_key is not None:
                    if _same(user.api_key.get_secret_value(), credential.secret):
                        return user
                case "bearer" if user.bearer_token is not None:
                    if _same(user.bearer_token.get_secret_value(), credential.secret):
                        return user
                case "basic" if user.username is not None and user.username == credential.username:
                    # A user configured without a password accepts any password.
                    if user.password is None or _same(
                        user.password.get_secret_value(), credential.secret
                    ):
                        return user
        return None

    def _known_username(self, credential: _Credential) -> bool:
        """A Basic credential naming a configured user (whose password didn't match)."""
        return credential.kind == "basic" and any(
            user.username == credential.username for user in self.settings.users
        )

    def _user(self, name: str) -> IdentityConfig | None:
        return next((user for user in self.settings.users if user.name == name), None)

    # -- tokens (built-in OAuth server) ---------------------------------------------------------

    def issue_token(
        self, identity: str, *, now_ns: int, ttl_seconds: int, scopes: tuple[str, ...] = ()
    ) -> str:
        """Mint an opaque bearer token for ``identity`` (used by the fake OAuth server)."""
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _IssuedToken(identity, now_ns + ttl_seconds * _NS_PER_SECOND, scopes)
        return token

    def revoke_token(self, token: str) -> bool:
        """Forget an issued token."""
        return self._tokens.pop(token, None) is not None

    def _issued_identity(self, token: str, now_ns: int) -> AuthOutcome | None:
        issued = self._tokens.get(token)
        if issued is None:
            return None
        if now_ns >= issued.expires_ns:
            return AuthOutcome(Identity(issued.identity, "expired", issued.scopes), "expired")
        user = self._user(issued.identity)
        if user is None and self.settings.mode == "allowlist":
            return AuthOutcome(None, "invalid")  # the user was removed after the token was issued
        status = user.status if user else "allowed"
        scopes = tuple(user.scopes) if user and user.scopes else issued.scopes
        identity = Identity(issued.identity, status, scopes, anonymous=user is None)
        return AuthOutcome(identity, _STATUS_FAILURE[status])

    # -- the check -------------------------------------------------------------------------------

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        cookies: Mapping[str, str],
        now_ns: int,
        required: bool,
    ) -> AuthOutcome:
        """Identify the caller; ``failure`` says why they are rejected (if ``required``)."""
        if self.model is None or self.settings.mode == "off":
            return AuthOutcome(None)
        lowered = {key.lower(): value for key, value in headers.items()}
        found: list[tuple[AuthScheme, _Credential]] = []
        for scheme in self.model.schemes:
            credential = self._extract(scheme, lowered, query, cookies)
            if credential is not None and not credential.empty:
                found.append((scheme, credential))
        if not found:
            return AuthOutcome(None, "missing" if required else None)
        scheme, credential = found[0]
        if not self._well_formed(scheme, credential):
            return AuthOutcome(None, "invalid" if required else None)
        if credential.kind == "bearer":
            issued = self._issued_identity(credential.secret, now_ns)
            if issued is not None:
                return issued if required else AuthOutcome(issued.identity)
        user = self._match_user(credential)
        if user is None:
            # An OAuth resource server only honors tokens its authorization server issued.
            unknown_rejected = self.settings.mode == "allowlist" or scheme.kind == "oauth2"
            if unknown_rejected or self._known_username(credential):
                return AuthOutcome(None, "invalid" if required else None)
            return AuthOutcome(Identity("anonymous", "allowed", anonymous=True))
        identity = Identity(user.name, user.status, tuple(user.scopes))
        failure = _STATUS_FAILURE[user.status] if required else None
        return AuthOutcome(identity, failure)
