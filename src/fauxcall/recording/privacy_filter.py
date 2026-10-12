"""The recorder's privacy layer: redact credentials, pseudonymize personal data, before disk.

Configured per fake in ``data/privacy.json`` (committed — it holds rules, not data)::

    {
      "include": {"$.owner.email": "email"},     // force a field to be treated as PII
      "exclude": ["$.name"],                     // a field that looks personal but isn't
      "path_params": {"username": "username"},   // PII inside URLs
      "query_params": {"email": "email"},
      "extra_secret_names": ["X-Session"],       // custom credential headers/fields
      "allow_names": ["token_count"],            // secret-looking names that aren't secrets
      "public_values": ["user1"],                // personal-looking values that are public
      "identifiers": ["ws_[a-z0-9]{12}"]         // the service's account/tenant ids
    }

Account and tenant ids are pseudonymized wherever they appear, in any field or text: Stripe's
``acct_…`` always, plus the regular expressions under ``identifiers`` (a named group ``id`` limits
the replacement to that part, for an account id embedded in other ids).

The pseudonym key (``FAUXCALL_PSEUDONYM_KEY``) lives in the fake's gitignored ``.env``. With
``"enabled": false`` nothing is pseudonymized (and no key is needed), but credentials are still
redacted: secrets never reach disk.

Personal values are replaced consistently everywhere: a value pseudonymized in any body (the
recorder processes response bodies first) is also replaced inside URLs, paths, path and query
parameters, headers and text bodies, raw or percent-encoded.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, field_validator

from fauxcall.errors import ConfigurationError
from fauxcall.jsonpath import WILDCARD, format_path, parse
from fauxcall.model.common import Model
from fauxcall.privacy import (
    Pseudonymizer,
    detect_pii,
    redact_headers,
    redact_json,
    redact_query,
    redact_url,
    scrub_text,
)
from fauxcall.privacy.pii import PiiCategory
from fauxcall.privacy.pseudonym import DEFAULT_IDENTIFIERS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fauxcall.jsonpath import Segment
    from fauxcall.types import JsonValue

_SEGMENT_SAFE: Final = "!$&'()*+,;=:@"
"""Characters a path segment may carry unencoded (RFC 3986 ``pchar`` minus unreserved)."""


class PrivacyConfig(Model):
    """``data/privacy.json``."""

    enabled: bool = True
    include: dict[str, PiiCategory] = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)
    path_params: dict[str, PiiCategory] = Field(default_factory=dict)
    query_params: dict[str, PiiCategory] = Field(default_factory=dict)
    extra_secret_names: list[str] = Field(default_factory=list)
    allow_names: list[str] = Field(default_factory=list)
    public_values: list[str] = Field(
        default_factory=list,
        description=(
            "Exact values that look personal or secret but are public (a sandbox's documented demo "
            "users, a demo's generated passwords): kept wherever they appear, while other values "
            "of the same fields are pseudonymized or redacted. Real credentials never are."
        ),
    )
    identifiers: list[str] = Field(
        default_factory=list,
        description=(
            "Regular expressions for the service's account or tenant ids, pseudonymized wherever "
            "they appear (Stripe's acct_ ids always are). A named group 'id' limits the "
            "replacement to that part."
        ),
    )

    @field_validator("identifiers")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                message = f"identifiers: {pattern!r} is not a regular expression: {error}"
                raise ValueError(message) from error
        return patterns

    @property
    def identifier_patterns(self) -> list[str]:
        """The built-in account id patterns, then this fake's."""
        return [*DEFAULT_IDENTIFIERS, *self.identifiers]


def _replace_segments(path: str, real: str, fake: str) -> str:
    """Replace whole path segments equal to ``real`` (raw or percent-encoded) with ``fake``."""
    spellings = {real, quote(real, safe=_SEGMENT_SAFE), quote(real, safe="")}
    encoded = quote(fake, safe=_SEGMENT_SAFE)
    return "/".join(encoded if segment in spellings else segment for segment in path.split("/"))


def _under(trail: tuple[Segment, ...], patterns: Sequence[tuple[Segment, ...]]) -> bool:
    """Whether a concrete path lies at or below one of the (generalized) ``patterns``."""
    return any(
        len(pattern) <= len(trail)
        and all(part is WILDCARD or part == trail[i] for i, part in enumerate(pattern))
        for pattern in patterns
    )


def _replace_known(
    pseudonymizer: Pseudonymizer,
    node: JsonValue,
    trail: tuple[Segment, ...],
    excluded: Sequence[tuple[Segment, ...]],
    changed: list[str],
) -> JsonValue:
    """``node`` with already-known personal values replaced in its strings (paths noted).

    Excluded subtrees (``exclude`` in the config) are left alone.
    """
    if _under(trail, excluded):
        return node
    if isinstance(node, str):
        new = pseudonymizer.pseudonymize_text(node)
        if new != node:
            changed.append(format_path(trail))
        return new
    if isinstance(node, list):
        return [
            _replace_known(pseudonymizer, item, (*trail, index), excluded, changed)
            for index, item in enumerate(node)
        ]
    if isinstance(node, dict):
        return {
            key: _replace_known(pseudonymizer, item, (*trail, key), excluded, changed)
            for key, item in node.items()
        }
    return node


def _replace_url_segments(url: str, real: str, fake: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=_replace_segments(parts.path, real, fake)))


class RecordingPrivacy:
    """Implements the recorder's :class:`~fauxcall.recording.recorder.PrivacyFilter`."""

    def __init__(
        self, config: PrivacyConfig, *, secret_values: Sequence[str], pseudonym_key: str | None
    ) -> None:
        self.config = config
        self.secrets = [value for value in secret_values if value]
        if config.enabled and not pseudonym_key:
            raise ConfigurationError(
                "FAUXCALL_PSEUDONYM_KEY is missing from the fake's .env "
                "(run `fauxcall env key <name>`)"
            )
        self.pseudonymizer = (
            Pseudonymizer(
                pseudonym_key,
                public=config.public_values,
                identifiers=config.identifier_patterns,
            )
            if config.enabled and pseudonym_key
            else None
        )

    def _names(self) -> dict[str, list[str]]:
        return {
            "extra_names": self.config.extra_secret_names,
            "allow_names": self.config.allow_names,
        }

    def request(
        self,
        *,
        url: str,
        path: str,
        path_params: Mapping[str, str],
        query: Sequence[tuple[str, str]],
        headers: Sequence[tuple[str, str]],
        json_body: JsonValue,
    ) -> tuple[
        str, str, dict[str, str], list[tuple[str, str]], list[tuple[str, str]], JsonValue, list[str]
    ]:
        """Redact and pseudonymize every part of a request.

        The JSON body goes first, so personal values it introduces are also replaced in the URL.
        """
        notes: list[str] = []
        safe_body = json_body
        if json_body is not None:
            safe_body, redacted = redact_json(
                json_body,
                extra_keys=self.config.extra_secret_names,
                secret_values=self.secrets,
                allow_keys=self.config.allow_names,
                public_values=self.config.public_values,
            )
            notes += [f"request:{p}" for p in redacted]
            safe_body, pseudonymized = self._pseudonymize(safe_body)
            notes += [f"request.pii:{p}" for p in pseudonymized]
        safe_headers, header_names = redact_headers(
            headers, secret_values=self.secrets, **self._names()
        )
        notes += [f"request.header:{name}" for name in header_names]
        safe_query, query_names = redact_query(query, secret_values=self.secrets, **self._names())
        notes += [f"request.query:{name}" for name in query_names]
        safe_url = redact_url(url, secret_values=self.secrets, **self._names())
        safe_path = scrub_text(path, self.secrets)
        safe_params = {name: scrub_text(value, self.secrets) for name, value in path_params.items()}
        if self.pseudonymizer is None:
            return safe_url, safe_path, safe_params, safe_query, safe_headers, safe_body, notes
        pseudonymizer = self.pseudonymizer
        for name, category in self.config.path_params.items():
            real = safe_params.get(name)
            if not real:
                continue
            fake = str(pseudonymizer.pseudonym(real, category))
            if fake == real:  # a public value: it stays
                continue
            safe_params[name] = fake
            safe_path = _replace_segments(safe_path, real, fake)
            safe_url = _replace_url_segments(safe_url, real, fake)
            notes.append(f"request.path:{name}")
        pseudo_query: list[tuple[str, str]] = []
        for key, value in safe_query:
            query_category = self.config.query_params.get(key)
            if query_category is not None:
                value = str(pseudonymizer.pseudonym(value, query_category))  # noqa: PLW2901
            pseudo_query.append((key, value))
        # Whatever is known by now (this body, earlier bodies, the parameters above) is replaced
        # everywhere it appears as a whole word.
        text = pseudonymizer.pseudonymize_text
        return (
            text(safe_url),
            text(safe_path),
            {name: text(value) for name, value in safe_params.items()},
            [(key, text(value)) for key, value in pseudo_query],
            [(name, text(value)) for name, value in safe_headers],
            safe_body,
            notes,
        )

    def response_headers(
        self, headers: Sequence[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Redact credentials from response headers (``Set-Cookie`` …).

        Known personal values are replaced too (a ``Location`` pointing at ``/users/<name>``).
        """
        safe, names = redact_headers(headers, secret_values=self.secrets, **self._names())
        if self.pseudonymizer is None:
            return safe, names
        text = self.pseudonymizer.pseudonymize_text
        return [(name, text(value)) for name, value in safe], names

    def _pseudonymize(self, value: JsonValue) -> tuple[JsonValue, list[str]]:
        """Pseudonymize detected fields, then known values inside every other string.

        The second pass catches personal data embedded in longer strings that field detection
        can't classify (``"html_url": "https://example.com/users/<username>"``).
        """
        if self.pseudonymizer is None:
            return value, []
        categories = detect_pii([value], include=self.config.include, exclude=self.config.exclude)
        changed: list[str] = []
        if categories:
            value, changed = self.pseudonymizer.pseudonymize_json(value, categories)
        excluded = [parse(path) for path in self.config.exclude]
        return _replace_known(self.pseudonymizer, value, (), excluded, changed), changed

    def json(self, value: JsonValue) -> tuple[JsonValue, list[str], list[str]]:
        """Redact then pseudonymize a JSON body."""
        redacted_value, redacted = redact_json(
            value,
            extra_keys=self.config.extra_secret_names,
            secret_values=self.secrets,
            allow_keys=self.config.allow_names,
            public_values=self.config.public_values,
        )
        pseudonymized_value, pseudonymized = self._pseudonymize(redacted_value)
        return pseudonymized_value, redacted, pseudonymized

    def text(self, value: str) -> str:
        """Scrub secrets and already-seen personal values from free text."""
        scrubbed = scrub_text(value, self.secrets)
        return self.pseudonymizer.pseudonymize_text(scrubbed) if self.pseudonymizer else scrubbed
