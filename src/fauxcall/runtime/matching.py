"""Answer a request no recording covers with the closest recorded one.

Order of preference: an exact match on every input; otherwise Jev (when a key is configured or the
judgment is cached) picks the best stand-in among recordings that agree on the ``exact`` inputs;
otherwise a deterministic lexical similarity; otherwise the spec's fallback.

When Jev judges that *no* recording fits, ``MatchSpec.no_match`` decides: ``closest`` (the
default) still answers with the deterministic choice; ``error``/``empty`` report no match so the
handler can respond accordingly.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fauxcall.model.semantic import MatchSpec
    from fauxcall.runtime.jev import JevGateway
    from fauxcall.types import JsonValue

MAX_CANDIDATES: Final = 60
LEXICAL_THRESHOLD: Final = 0.2
_TOKEN: Final = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Candidate:
    """A recording that could answer the request, described by its inputs."""

    id: str
    inputs: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Which candidate was chosen and how."""

    candidate: Candidate | None
    method: str
    confidence: float | None = None


def _text(value: JsonValue) -> str:
    return "" if value is None else str(value)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def similarity(left: Mapping[str, JsonValue], right: Mapping[str, JsonValue]) -> float:
    """0..1 similarity of two input sets (token Jaccard blended with character similarity)."""
    keys = sorted(set(left) | set(right))
    if not keys:
        return 1.0
    total = 0.0
    for key in keys:
        a, b = _text(left.get(key)), _text(right.get(key))
        if a == b:
            total += 1.0
            continue
        ta, tb = _tokens(a), _tokens(b)
        jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
        ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
        total += 0.6 * jaccard + 0.4 * ratio
    return total / len(keys)


def _agrees(candidate: Candidate, inputs: Mapping[str, JsonValue], exact: Sequence[str]) -> bool:
    return all(_text(candidate.inputs.get(key)) == _text(inputs.get(key)) for key in exact)


async def match(
    inputs: Mapping[str, JsonValue],
    candidates: Sequence[Candidate],
    spec: MatchSpec | None,
    jev: JevGateway | None,
) -> MatchResult:
    """Choose the recording that best answers ``inputs``."""
    for candidate in candidates:
        if all(_text(candidate.inputs.get(k)) == _text(v) for k, v in inputs.items()) and set(
            candidate.inputs
        ) <= set(inputs):
            return MatchResult(candidate, "exact", 1.0)
    exact_keys = list(spec.exact) if spec else []
    pool = [c for c in candidates if _agrees(c, inputs, exact_keys)]
    if not pool:
        return MatchResult(None, "no_candidates")
    ranked = sorted(pool, key=lambda c: (-similarity(inputs, c.inputs), c.id))[:MAX_CANDIDATES]
    if jev is not None and spec is not None:
        chosen = await _jev_choice(inputs, ranked, spec, jev)
        if chosen is not None and (chosen.candidate is not None or spec.no_match != "closest"):
            return chosen
    best = ranked[0]
    score = similarity(inputs, best.inputs)
    if score >= LEXICAL_THRESHOLD:
        return MatchResult(best, "lexical", score)
    fallback = spec.fallback if spec else "lexical"
    if fallback == "first":
        return MatchResult(pool[0], "first", None)
    if fallback == "lexical":
        return MatchResult(best, "lexical_weak", score)
    return MatchResult(None, "below_threshold", score)


async def _jev_choice(
    inputs: Mapping[str, JsonValue],
    ranked: Sequence[Candidate],
    spec: MatchSpec,
    jev: JevGateway,
) -> MatchResult | None:
    options = {
        candidate.id: "; ".join(f"{k} = {_text(v)}" for k, v in candidate.inputs.items())
        for candidate in ranked
    }
    options["none"] = "None of the recorded requests is a reasonable stand-in."
    state: JsonValue = {"new_request": dict(inputs)}
    answer = await jev.choose(instructions=spec.instructions, options=options, state=state)
    if answer is None or not isinstance(answer.value, str):
        return None
    confidence = answer.confidence if answer.confidence is not None else 0.0
    if answer.value == "none":
        return MatchResult(None, "jev_no_match", confidence)
    if confidence < spec.min_confidence:
        return None  # not sure enough: use the deterministic choice
    chosen = next((c for c in ranked if c.id == answer.value), None)
    return MatchResult(chosen, "jev", confidence) if chosen else None
