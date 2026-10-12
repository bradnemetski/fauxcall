"""What the credentials may do: which endpoints and tools the real service refused for permission.

A narrow key (a restricted API key, a token with few scopes) makes a narrow fake: everything the key
can't use is recorded as a refusal, and the fake refuses it too. Run a cheap read-only probe of each
endpoint or tool early, then show the user what was refused before planning the build around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from fauxcall import jsonfmt

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fauxcall.model.recording import Exchange
    from fauxcall.types import JsonValue

REFUSED_STATUSES: Final = frozenset({401, 403})
PERMISSION_WORDS: Final = re.compile(
    r"permission|not (?:allowed|authori[sz]ed|permitted)|forbidden|insufficient[_ ]scope"
    r"|access denied|unauthori[sz]ed|restricted key|does not have access",
    re.IGNORECASE,
)
_ERROR_PARAM: Final = re.compile(r'error="?([A-Za-z_]+)"?')
_EXCERPT: Final = 80
_EXAMPLES: Final = 3
_PROBE_TAGS: Final = frozenset({"auth:none", "auth:invalid"})


@dataclass(slots=True)
class Access:
    """How one endpoint or tool fared with the configured credentials."""

    endpoint: str
    calls: int = 0
    refused: int = 0
    reasons: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    """Arguments (MCP) or paths of refused calls, to tell apart what a broad tool may do."""

    @property
    def all_refused(self) -> bool:
        """Every call was refused: the credentials can't use it at all."""
        return self.calls > 0 and self.refused == self.calls


def refusal(exchange: Exchange) -> str | None:
    """Why the service refused this call for permission (``None``: it didn't)."""
    if _PROBE_TAGS & set(exchange.tags):  # sent without (or with bad) credentials on purpose
        return None
    response = exchange.response
    if response is None:
        return None
    if response.status in REFUSED_STATUSES:
        challenge = next((v for k, v in response.headers if k.lower() == "www-authenticate"), "")
        found = _ERROR_PARAM.search(challenge)
        return f"HTTP {response.status}" + (f" {found[1]}" if found else "")
    text = _error_text(response.body.parsed())
    if text is not None and PERMISSION_WORDS.search(text):
        return _excerpt(text)
    return None


def access_report(exchanges: Iterable[Exchange]) -> list[Access]:
    """Per endpoint or tool, in order of first appearance: calls made and refusals."""
    report: dict[str, Access] = {}
    for exchange in exchanges:
        if _PROBE_TAGS & set(exchange.tags) or exchange.response is None:
            continue
        entry = report.setdefault(exchange.endpoint, Access(exchange.endpoint))
        entry.calls += 1
        reason = refusal(exchange)
        if reason is None:
            continue
        entry.refused += 1
        if reason not in entry.reasons:
            entry.reasons.append(reason)
        example = _example(exchange)
        if example and example not in entry.examples and len(entry.examples) < _EXAMPLES:
            entry.examples.append(example)
    return list(report.values())


def _error_text(body: JsonValue) -> str | None:
    """The error message of a JSON-RPC error or a failed tool result, else of an error body."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    result = body.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        content = result.get("content")
        texts = (
            [str(part.get("text", "")) for part in content if isinstance(part, dict)]
            if isinstance(content, list)
            else []
        )
        return " ".join(texts)
    if isinstance(error, str):
        return error
    message = body.get("message")
    return str(message) if isinstance(message, str) else None


def _example(exchange: Exchange) -> str | None:
    body = exchange.request.body.parsed()
    if isinstance(body, dict) and body.get("method") == "tools/call":
        params = body.get("params")
        arguments = params.get("arguments") if isinstance(params, dict) else None
        return _excerpt(jsonfmt.dumps(arguments)) if arguments else None
    return exchange.request.path


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _EXCERPT else flat[: _EXCERPT - 1] + "…"
