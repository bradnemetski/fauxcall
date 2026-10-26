"""Remove credentials from recordings before they are written (recordings are committed to git).

Rules, applied to headers, query parameters, URLs, JSON bodies and text:

1. **Secret names.** A header, parameter or JSON key holds a credential when
   :func:`fauxcall.logs.is_secret_key` says so (``Authorization``, ``X-API-Key``,
   ``access_token``, ``password``, ``Cookie``, ``session_id`` …), when it is a signature-style
   name (``sig``, ``signature``, ``jwt``, ``access_key``, ``app_key``, ``subscription_key``), a
   bare ``auth`` (``X-Auth``, Firebase's ``?auth=``), a password spelling (``pass``, ``pwd``,
   ``passphrase``), or when the caller lists it in ``extra_names``/``extra_keys``. Names that
   only *describe* credentials stay readable (:func:`is_public_name`): ``token_type``,
   ``expires_in``, ``scope``, ``password_policy`` and anything ending in ``_type``,
   ``_endpoint``, ``_supported``, ``_servers``, ``_policy``, ``_method(s)``,
   ``_expires_in``/``_expires_at`` or ``_scope(s)`` (OAuth metadata such as
   ``authorization_endpoint`` keeps working); pagination cursors
   (``nextPageToken``, ``continuation_token`` …); rate-limit and CORS headers
   (``X-RateLimit-Remaining-Tokens``, ``Access-Control-Allow-Credentials``). Callers add their own
   exceptions with ``allow_names``/``allow_keys`` (e.g. ``token0`` in a DEX API); ``extra_*``
   always wins over ``allow_*``.
2. **The shape survives.** ``Bearer abc`` becomes ``Bearer [REDACTED]``, cookies keep their names
   and attributes, JSON keeps its structure: only string leaves change, while numbers, booleans and
   ``null`` under secret keys (token *counts*, ``password_set`` flags) are kept. A UUID under a
   secret key becomes :data:`REDACTED_UUID`, still a UUID.
3. **Embedded credentials** are removed from every other value: URL userinfo (``user:pass@``),
   ``?``/``&``/``#``/``;`` parameters with secret names, and JWTs.
4. **Known secret values** — the real credentials from ``.env``, at least
   :data:`MIN_SECRET_LENGTH` characters long — are replaced wherever they appear, raw or
   percent-encoded.

Every function returns a redacted *copy* and never mutates its input. Redaction is idempotent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, quote_plus, unquote_plus

from fauxcall import logs
from fauxcall.jsonpath import format_path
from fauxcall.privacy.pii import normalize_key

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from fauxcall.jsonpath import Segment
    from fauxcall.types import JsonValue

REDACTED: Final = logs.REDACTED
REDACTED_UUID: Final = "00000000-0000-4000-8000-000000000000"
"""What a UUID under a secret key becomes, so it stays a UUID (an MCP ``widgetSessionId``)."""
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
"""What a credential is replaced with (the same marker the log pipeline uses)."""

MIN_SECRET_LENGTH: Final = 4
"""Shorter ``secret_values`` are ignored: scrubbing them would mangle unrelated text."""

_EXTRA_SECRET_RE: Final = re.compile(
    r"(?:^|_)(?:auth|auth_?key|sig|signature|jwt|access_?key|app_?key|subscription_?key|"
    r"pass|passphrase|pwd)$"
)
_PUBLIC_SUFFIX_RE: Final = re.compile(
    r"(?:^|_)(?:types?|endpoint|supported|servers|policy|methods?|expires_(?:in|at)|scopes?)$"
)
_PAGINATION_RE: Final = re.compile(
    r"(?:page|next|prev|previous|continuation|sync|resume|cursor|progress)_?token$"
)
_TOKEN_COUNT_RE: Final = re.compile(
    r"(?:^|_)(?:max|min|total|input|output|prompt|completion|cached|reasoning)_?tokens?$"
    r"|tokens?_(?:count|used|limit)$"
)
"""Counts of a language model's tokens (``maxTokens``, ``prompt_tokens``): numbers, not keys."""
_PUBLIC_HEADER_RE: Final = re.compile(r"rate_?limit|^access_control_")
_AUTH_HEADERS: Final = frozenset({"authorization", "proxy-authorization"})
_COOKIE_HEADERS: Final = frozenset({"cookie", "cookie2"})
_SET_COOKIE_HEADERS: Final = frozenset({"set-cookie", "set-cookie2"})
_KNOWN_SCHEMES: Final = frozenset(
    {
        "basic",
        "bearer",
        "digest",
        "token",
        "apikey",
        "api-key",
        "jwt",
        "oauth",
        "negotiate",
        "ntlm",
        "hoba",
        "mutual",
        "dpop",
        "gnap",
        "aws4-hmac-sha256",
        "ssws",
        "key",
    }
)
"""Authentication schemes kept in front of ``[REDACTED]`` in any secret header."""

_SCHEME_RE: Final = re.compile(
    r"^(?P<lead>\s*)(?P<scheme>[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*)(?P<space>\s+)(?P<rest>\S.*)$",
    re.DOTALL,
)
_CUSTOM_SCHEME_RE: Final = re.compile(r"[A-Za-z][A-Za-z-]{1,31}")
"""A plausible custom scheme in ``Authorization`` (``GenieKey``, ``Klaviyo-API-Key``); no digits."""

_JWT_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){2,4}(?![A-Za-z0-9_.-])"
)
_PARAM_RE: Final = re.compile(
    r"(?P<lead>[?&#;])(?P<name>[^=&#?;\s\"'<>]+)=(?P<value>[^&#?;\s\"'<>]*)"
)
_USERINFO_RE: Final = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^\s/?#@\"'<>]+)@"
)
_FOLDED_COOKIES_RE: Final = re.compile(r"(,\s*)(?=[^;,=\s]+=)")


def is_public_name(name: str) -> bool:
    """Whether a name is exempt from redaction by name (``token_type``, ``pageToken``).

    These names describe credentials rather than hold them, or count a model's tokens
    (``maxTokens``), so they stay readable even when
    :func:`fauxcall.logs.is_secret_key` matches. The rules are suffix-based, so unrelated names
    such as ``Content-Type`` qualify too (harmlessly).
    """
    normalized = normalize_key(name)
    return bool(
        _PUBLIC_SUFFIX_RE.search(normalized)
        or _PAGINATION_RE.search(normalized)
        or _TOKEN_COUNT_RE.search(normalized)
        or _PUBLIC_HEADER_RE.search(normalized)
    )


def is_secret_name(name: str) -> bool:
    """Whether a header, parameter or key name holds a credential (built-in heuristics only)."""
    if is_public_name(name):
        return False
    return logs.is_secret_key(name) or bool(_EXTRA_SECRET_RE.search(normalize_key(name)))


class _Names:
    """The built-in name heuristics plus a caller's ``extra`` and ``allow`` lists."""

    __slots__ = ("_allow", "_extra")

    def __init__(self, extra: Iterable[str], allow: Iterable[str]) -> None:
        self._extra = frozenset(name.casefold() for name in extra)
        self._allow = frozenset(name.casefold() for name in allow)

    def is_secret(self, name: str, *, inside_secret: bool = False) -> bool:
        """Whether ``name`` holds a credential (``inside_secret``: its parent already does)."""
        folded = name.casefold()
        if folded in self._extra:
            return True
        if folded in self._allow or is_public_name(name):
            return False
        return inside_secret or is_secret_name(name)


class _Scrubber:
    """Replaces known secret values (raw and percent-encoded) with :data:`REDACTED`."""

    __slots__ = ("_pattern",)

    def __init__(self, secret_values: Iterable[str]) -> None:
        variants = {
            variant
            for value in secret_values
            if len(value) >= MIN_SECRET_LENGTH
            for variant in (value, quote(value, safe=""), quote_plus(value), quote(value))
        }
        ordered = sorted(variants, key=lambda text: (-len(text), text))
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def __call__(self, text: str) -> str:
        return text if self._pattern is None else self._pattern.sub(REDACTED, text)


def scrub_text(text: str, secret_values: Iterable[str]) -> str:
    """Replace every occurrence of every secret value in ``text`` with :data:`REDACTED`.

    Values shorter than :data:`MIN_SECRET_LENGTH` are ignored; longer values win over shorter ones
    they contain, and percent-encoded spellings (``quote`` with and without ``safe="/"``,
    ``quote_plus``) are replaced too.
    """
    return _Scrubber(secret_values)(text)


# ---------------------------------------------------------------------------
# Embedded credentials (URLs, parameters, JWTs) inside any text value
# ---------------------------------------------------------------------------


def _mask(part: str) -> str:
    return REDACTED if part else ""


def _redact_userinfo(match: re.Match[str]) -> str:
    user, colon, password = match["userinfo"].partition(":")
    return f"{match['scheme']}{_mask(user)}{colon}{_mask(password)}@"


def _redact_embedded(text: str, names: _Names) -> str:
    """Remove URL userinfo, secret-named parameters and JWTs from free-form text."""

    def redact_param(match: re.Match[str]) -> str:
        value = match["value"]
        if not value or unquote_plus(value) == REDACTED:
            return match[0]
        if not names.is_secret(unquote_plus(match["name"])):
            return match[0]
        return f"{match['lead']}{match['name']}={REDACTED}"

    if "@" in text:
        text = _USERINFO_RE.sub(_redact_userinfo, text)
    if "=" in text:
        text = _PARAM_RE.sub(redact_param, text)
    if "eyJ" in text:
        text = _JWT_RE.sub(REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def _leading_space(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _redact_cookie_pair(pair: str) -> str:
    name, equals, value = pair.partition("=")
    if equals:
        current = value.strip()
        return pair if not current or current == REDACTED else f"{name}={REDACTED}"
    current = pair.strip()
    return pair if not current or current == REDACTED else _leading_space(pair) + REDACTED


def _redact_cookie(value: str) -> str:
    """``a=1; b=2`` → ``a=[REDACTED]; b=[REDACTED]``."""
    return ";".join(_redact_cookie_pair(pair) for pair in value.split(";"))


def _redact_set_cookie(value: str) -> str:
    """``sid=abc; Path=/; HttpOnly`` → ``sid=[REDACTED]; Path=/; HttpOnly`` (folded lists too)."""
    pieces = _FOLDED_COOKIES_RE.split(value)
    redacted: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2:  # the ", " between two folded cookies
            redacted.append(piece)
            continue
        first, separator, attributes = piece.partition(";")
        redacted.append(_redact_cookie_pair(first) + separator + attributes)
    return "".join(redacted)


def _redact_credentials(value: str, *, auth_header: bool) -> str:
    """``Bearer abc`` → ``Bearer [REDACTED]``; anything without a recognizable scheme → all."""
    current = value.strip()
    if not current or current == REDACTED or current.casefold() in _KNOWN_SCHEMES:
        return value
    match = _SCHEME_RE.match(value)
    if match is not None:
        scheme = match["scheme"]
        known = scheme.casefold() in _KNOWN_SCHEMES
        if known or (auth_header and _CUSTOM_SCHEME_RE.fullmatch(scheme)):
            if match["rest"].strip() == REDACTED:
                return value
            return f"{match['lead']}{scheme}{match['space']}{REDACTED}"
    return REDACTED


def _redact_secret_header(name: str, value: str) -> str:
    lowered = name.strip().casefold()
    if lowered in _COOKIE_HEADERS:
        return _redact_cookie(value)
    if lowered in _SET_COOKIE_HEADERS:
        return _redact_set_cookie(value)
    return _redact_credentials(value, auth_header=lowered in _AUTH_HEADERS)


def _append_once(names: list[str], name: str) -> None:
    if name not in names:
        names.append(name)


def redact_headers(
    headers: Sequence[tuple[str, str]],
    *,
    extra_names: Iterable[str] = (),
    secret_values: Iterable[str] = (),
    allow_names: Iterable[str] = (),
) -> tuple[list[tuple[str, str]], list[str]]:
    """Redact credentials from HTTP headers, keeping each value's shape.

    ``Authorization: Bearer abc`` → ``Bearer [REDACTED]`` (``Basic``, ``Digest`` and custom schemes
    alike); ``Cookie: a=1; b=2`` → ``a=[REDACTED]; b=[REDACTED]``;
    ``Set-Cookie: sid=abc; Path=/; HttpOnly`` → ``sid=[REDACTED]; Path=/; HttpOnly``; other secret
    headers → ``[REDACTED]``. Other headers only lose embedded credentials (e.g. a ``Location`` URL
    with ``?access_token=…``) and known secret values.

    Args:
        headers: ``(name, value)`` pairs in their original order (repeated names allowed).
        extra_names: More header names to treat as secret (case-insensitive).
        secret_values: The real credentials; any header value containing one is scrubbed.
        allow_names: Header names never treated as secret by name (case-insensitive).

    Returns:
        The redacted headers (same order and names) and the names whose value changed, once each.
    """
    names = _Names(extra_names, allow_names)
    scrub = _Scrubber(secret_values)
    result: list[tuple[str, str]] = []
    redacted: list[str] = []
    for name, value in headers:
        if names.is_secret(name):
            new_value = _redact_secret_header(name, value)
        else:
            new_value = _redact_embedded(value, names)
        new_name, new_value = scrub(name), scrub(new_value)
        if (new_name, new_value) != (name, value):
            _append_once(redacted, new_name)
        result.append((new_name, new_value))
    return result, redacted


# ---------------------------------------------------------------------------
# Query strings and URLs
# ---------------------------------------------------------------------------


def redact_query(
    query: Sequence[tuple[str, str]],
    *,
    extra_names: Iterable[str] = (),
    secret_values: Iterable[str] = (),
    allow_names: Iterable[str] = (),
) -> tuple[list[tuple[str, str]], list[str]]:
    """Redact credentials from decoded query parameters (``[("api_key", "abc"), …]``).

    Secret-named parameters become ``[REDACTED]``; other values lose embedded credentials (a
    ``redirect_uri`` with ``?token=…``, a JWT) and known secret values.

    Args:
        query: Decoded ``(name, value)`` pairs in their original order.
        extra_names: More parameter names to treat as secret (case-insensitive).
        secret_values: The real credentials; any name or value containing one is scrubbed.
        allow_names: Parameter names never treated as secret by name (case-insensitive).

    Returns:
        The redacted pairs (same order) and the names of the parameters that changed, once each.
    """
    names = _Names(extra_names, allow_names)
    scrub = _Scrubber(secret_values)
    result: list[tuple[str, str]] = []
    redacted: list[str] = []
    for name, value in query:
        if value and value != REDACTED and names.is_secret(name):
            new_value = REDACTED
        else:
            new_value = scrub(_redact_embedded(value, names))
        new_name = scrub(name)
        if (new_name, new_value) != (name, value):
            _append_once(redacted, new_name)
        result.append((new_name, new_value))
    return result, redacted


def redact_url(
    url: str,
    *,
    extra_names: Iterable[str] = (),
    secret_values: Iterable[str] = (),
    allow_names: Iterable[str] = (),
) -> str:
    """Redact credentials from a URL, leaving every other byte as it was.

    ``https://user:pass@host/p?api_key=abc&page=2`` →
    ``https://[REDACTED]:[REDACTED]@host/p?api_key=[REDACTED]&page=2``. Parameter order, encoding
    and separators are untouched. Secret-named parameters in the fragment (OAuth implicit flow:
    ``#access_token=…``) and matrix parameters (``;jsessionid=…``) are redacted as well, as are
    JWTs and known secret values anywhere in the URL (e.g. a bot token in the path).

    Args:
        url: An absolute or relative URL.
        extra_names: More parameter names to treat as secret (case-insensitive).
        secret_values: The real credentials, scrubbed raw and percent-encoded.
        allow_names: Parameter names never treated as secret by name (case-insensitive).
    """
    names = _Names(extra_names, allow_names)
    return _Scrubber(secret_values)(_redact_embedded(url, names))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class _JsonRedactor:
    """Walks a JSON value, building a redacted copy and recording the concrete paths it changed."""

    __slots__ = ("_names", "_public", "_scrub", "paths")

    def __init__(self, names: _Names, scrub: _Scrubber, public: frozenset[str]) -> None:
        self._names = names
        self._scrub = scrub
        self._public = public
        self.paths: list[str] = []

    def walk(self, node: JsonValue, trail: tuple[Segment, ...], *, secret: bool) -> JsonValue:
        """Return the redacted copy of ``node`` (``secret``: it sits under a secret key)."""
        if isinstance(node, dict):
            copy: dict[str, JsonValue] = {}
            for key, child in node.items():
                new_key = self._scrub(key)
                child_secret = self._names.is_secret(key, inside_secret=secret)
                copy[new_key] = self.walk(child, (*trail, new_key), secret=child_secret)
                if new_key != key:
                    self.paths.append(format_path((*trail, new_key)))
            return copy
        if isinstance(node, list):
            return [
                self.walk(child, (*trail, index), secret=secret) for index, child in enumerate(node)
            ]
        if not isinstance(node, str):
            return node  # numbers, booleans and null are never credentials
        if secret and node and node not in {REDACTED, REDACTED_UUID} and node not in self._public:
            new = REDACTED_UUID if _UUID.fullmatch(node) else REDACTED
        else:
            new = self._scrub(_redact_embedded(node, self._names))
        if new != node:
            self.paths.append(format_path(trail))
        return new


def redact_json(
    value: JsonValue,
    *,
    extra_keys: Iterable[str] = (),
    secret_values: Iterable[str] = (),
    allow_keys: Iterable[str] = (),
    public_values: Iterable[str] = (),
) -> tuple[JsonValue, list[str]]:
    """Redact credentials from a JSON value (a request/response body, an MCP message …).

    * A non-empty string under a secret-looking key becomes :data:`REDACTED`; when the key holds
      an object or array, every string leaf below it is redacted (public names below it, such as
      ``token_type``, are kept). Numbers, booleans and ``null`` are kept.
    * Every other string loses embedded credentials (URL userinfo, secret-named URL parameters,
      JWTs) and known secret values. Keys containing a secret value are scrubbed too.

    Args:
        value: The JSON value; it is not modified.
        extra_keys: More key names to treat as secret (case-insensitive).
        secret_values: The real credentials, scrubbed wherever they appear in strings.
        allow_keys: Key names never treated as secret by name (case-insensitive).
        public_values: Exact values kept under secret keys (a public demo's generated passwords);
            a real credential (``secret_values``) is redacted even when listed here.

    Returns:
        The redacted copy and the concrete JSON paths (``$.data[0].token``) that changed.
    """
    secrets = list(secret_values)
    public = frozenset(public_values) - set(secrets)
    redactor = _JsonRedactor(_Names(extra_keys, allow_keys), _Scrubber(secrets), public)
    redacted = redactor.walk(value, (), secret=False)
    return redacted, redactor.paths
