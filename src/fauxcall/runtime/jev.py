"""Jev (TypeSafe) at runtime: typed, calibrated judgments for inputs no recording covers.

Used only when the user configured a key. Every answer is cached in ``data/jev-cache.jsonl`` keyed
by (model, question, state) *before* the key is checked, so a committed fake replays its judgments
identically on machines without a key. Any failure (no key, network, rate limit) returns ``None``
and callers fall back to deterministic logic.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from fauxcall import jsonfmt
from fauxcall.logs import get_logger
from fauxcall.model.common import Model, RawJson
from fauxcall.storage import append_jsonl

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

    from fauxcall.config import FakeSettings
    from fauxcall.types import JsonValue

log = get_logger(__name__)

QuestionKind = Literal["choice", "noul", "score"]


class JevAnswer(Model):
    """A cached answer."""

    kind: QuestionKind
    value: RawJson = Field(description="choice: option; noul: probability; score: weighted level.")
    confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    model: str = ""


class _CacheLine(Model):
    key: str
    answer: JevAnswer
    at: str


def cache_key(
    model: str, kind: QuestionKind, instructions: str, criteria: JsonValue, state: JsonValue
) -> str:
    """Stable key for one judgment."""
    payload: JsonValue = {
        "model": model,
        "kind": kind,
        "instructions": instructions,
        "criteria": criteria,
        "state": state,
    }
    return hashlib.sha256(jsonfmt.canonical_dumps(payload).encode()).hexdigest()


class JevGateway:
    """Cached access to Jev."""

    def __init__(
        self,
        settings: FakeSettings,
        cache_path: Path | None,
        *,
        client_factory: Callable[[str, str, float], AsyncTypeSafeClient] | None = None,
    ) -> None:
        self._settings = settings
        self._cache_path = cache_path
        self._factory = client_factory
        self._client: AsyncTypeSafeClient | None = None
        self._cache = _load_cache(cache_path) if cache_path is not None else {}

    @property
    def active(self) -> bool:
        """Whether live calls are possible (enabled and a key is configured)."""
        return self._settings.jev_active

    @property
    def cached_answers(self) -> int:
        """How many judgments are cached."""
        return len(self._cache)

    def _get_client(self) -> AsyncTypeSafeClient:
        if self._client is None:
            key = self._settings.typesafe_api_key
            assert key is not None  # guarded by `active`  # noqa: S101
            api_key = key.get_secret_value()
            model = self._settings.jev.model
            timeout = self._settings.jev.timeout_seconds
            if self._factory is not None:
                self._client = self._factory(api_key, model, timeout)
            else:  # pragma: no cover - exercised only with a real key
                from typesafe_sdk import AsyncTypeSafeClient  # noqa: PLC0415

                self._client = AsyncTypeSafeClient(api_key=api_key, model=model, timeout=timeout)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _remember(self, key: str, answer: JevAnswer) -> None:
        self._cache[key] = answer
        if self._cache_path is not None:
            line = _CacheLine(key=key, answer=answer, at=datetime.now(UTC).isoformat())
            try:
                append_jsonl(self._cache_path, line.model_dump(mode="json"))
            except OSError as exc:  # a read-only checkout still answers (from memory)
                log.warning("jev_cache_write_failed", path=str(self._cache_path), error=str(exc))

    async def _ask(
        self, kind: QuestionKind, instructions: str, criteria: JsonValue, state: JsonValue
    ) -> JevAnswer | None:
        model = self._settings.jev.model
        key = cache_key(model, kind, instructions, criteria, state)
        if key in self._cache:
            return self._cache[key]
        if not self.active:
            return None
        try:
            response = await self._get_client().system_one(
                state=jsonfmt.plain(state),  # type: ignore[arg-type]
                questions={"q": _question(kind, instructions, criteria)},
            )
        except Exception as exc:  # noqa: BLE001 - any SDK/network/validation failure: fall back
            log.warning("jev_unavailable", error=type(exc).__name__, detail=str(exc)[:200])
            return None
        answer = _to_answer(kind, response.answers.get("q"), response.model)
        if answer is not None:
            self._remember(key, answer)
        return answer

    async def choose(
        self, *, instructions: str, options: Mapping[str, str | None], state: JsonValue
    ) -> JevAnswer | None:
        """Pick one of ``options`` (option -> description)."""
        criteria: JsonValue = dict(options)
        return await self._ask("choice", instructions, criteria, state)

    async def probability(self, *, instructions: str, state: JsonValue) -> JevAnswer | None:
        """Probability that a yes/no statement holds."""
        return await self._ask("noul", instructions, None, state)

    async def score(
        self, *, instructions: str, levels: list[str], state: JsonValue
    ) -> JevAnswer | None:
        """Rate on ordered levels."""
        criteria: JsonValue = list(levels)
        return await self._ask("score", instructions, criteria, state)


def _load_cache(path: Path) -> dict[str, JevAnswer]:
    """Cached answers by key; unreadable lines (a torn write, a merge conflict …) are skipped."""
    cache: dict[str, JevAnswer] = {}
    if not path.is_file():
        return cache
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = _CacheLine.model_validate(jsonfmt.loads(line))
            except ValueError as exc:  # invalid JSON and validation errors alike
                detail = str(exc)[:200]
                log.warning("jev_cache_line_skipped", path=str(path), line=number, error=detail)
                continue
            cache[parsed.key] = parsed.answer
    return cache


def _question(kind: QuestionKind, instructions: str, criteria: JsonValue) -> Choice | Noul | Score:
    from typesafe_sdk import Choice, Noul, Score  # noqa: PLC0415 - optional at import time

    if kind == "choice":
        return Choice(instructions=instructions, criteria=criteria)  # type: ignore[arg-type]
    if kind == "noul":
        return Noul(instructions=instructions)
    return Score(instructions=instructions, criteria=criteria)  # type: ignore[arg-type]


def _to_answer(kind: QuestionKind, raw: object, model: str) -> JevAnswer | None:
    """Our cached form of an SDK answer (``None`` when missing or of the wrong type)."""
    if raw is None or getattr(raw, "type", kind) != kind:
        return None
    if kind == "choice":
        choice = getattr(raw, "choice", None)
        probabilities = {
            str(k): float(v) for k, v in (getattr(raw, "probabilities", {}) or {}).items()
        }
        return JevAnswer(
            kind=kind,
            value=choice,
            confidence=getattr(raw, "confidence", None),
            probabilities=probabilities,
            model=model,
        )
    if kind == "noul":
        return JevAnswer(kind=kind, value=float(getattr(raw, "noul", 0.0)), model=model)
    probabilities = {str(k): float(v) for k, v in (getattr(raw, "probabilities", {}) or {}).items()}
    return JevAnswer(
        kind=kind,
        value=float(getattr(raw, "score", 0.0)),
        confidence=getattr(raw, "confidence", None),
        probabilities=probabilities,
        model=model,
    )
