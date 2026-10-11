from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from fauxcall.model.semantic import MatchSpec
from fauxcall.runtime.jev import JevAnswer
from fauxcall.runtime.matching import (
    LEXICAL_THRESHOLD,
    MAX_CANDIDATES,
    Candidate,
    MatchResult,
    match,
    similarity,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fauxcall.runtime.jev import JevGateway
    from fauxcall.types import JsonValue

REACT_RECONCILER = Candidate(
    "rec-1", {"args.repo": "facebook/react", "args.question": "How does the reconciler work?"}
)
REACT_HOOKS = Candidate(
    "rec-2", {"args.repo": "facebook/react", "args.question": "What is a hook?"}
)
NEXT_ROUTING = Candidate(
    "rec-3", {"args.repo": "vercel/next.js", "args.question": "How does routing work?"}
)
CANDIDATES = [REACT_RECONCILER, REACT_HOOKS, NEXT_ROUTING]
NEW_QUESTION: dict[str, JsonValue] = {
    "args.repo": "facebook/react",
    "args.question": "How does the reconciler work in React 19?",
}


@dataclass
class StubJev:
    """Stands in for :class:`JevGateway`: returns a fixed answer and records the question."""

    answer: JevAnswer | None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def choose(
        self, *, instructions: str, options: Mapping[str, str | None], state: JsonValue
    ) -> JevAnswer | None:
        self.calls.append({"instructions": instructions, "options": dict(options), "state": state})
        return self.answer


def jev(value: JsonValue, confidence: float | None = 0.9) -> StubJev:
    return StubJev(JevAnswer(kind="choice", value=value, confidence=confidence))


def spec(**fields: Any) -> MatchSpec:
    return MatchSpec.model_validate({"inputs": ["args.repo", "args.question"], **fields})


async def run(
    inputs: Mapping[str, JsonValue],
    candidates: list[Candidate] = CANDIDATES,
    match_spec: MatchSpec | None = None,
    gateway: StubJev | None = None,
) -> MatchResult:
    return await match(inputs, candidates, match_spec, cast("JevGateway | None", gateway))


async def test_an_exact_match_wins_without_asking_jev() -> None:
    gateway = jev("rec-1")
    result = await run(dict(REACT_HOOKS.inputs), match_spec=spec(), gateway=gateway)
    assert result == MatchResult(REACT_HOOKS, "exact", 1.0)
    assert gateway.calls == []


async def test_exact_means_every_input_agrees_and_nothing_extra_was_recorded() -> None:
    narrow = Candidate("narrow", {"q": "x"})
    wide = Candidate("wide", {"q": "x", "page": "2"})
    assert (await run({"q": "x", "lang": None}, [wide, narrow])).candidate is narrow
    result = await run({"q": "x"}, [wide])
    assert (result.candidate, result.method) == (wide, "lexical")


async def test_exact_inputs_restrict_the_pool() -> None:
    inputs = {"args.repo": "vercel/next.js", "args.question": "What is a hook?"}
    result = await run(inputs, match_spec=spec(exact=["args.repo"]))
    assert result.candidate is NEXT_ROUTING
    nobody = await run(
        {"args.repo": "nobody/else", "args.question": "?"}, match_spec=spec(exact=["args.repo"])
    )
    assert nobody == MatchResult(None, "no_candidates")
    assert await run(NEW_QUESTION, []) == MatchResult(None, "no_candidates")


async def test_lexical_similarity_picks_the_closest_recording() -> None:
    result = await run(NEW_QUESTION)
    assert (result.candidate, result.method) == (REACT_RECONCILER, "lexical")
    assert result.confidence is not None
    assert result.confidence >= LEXICAL_THRESHOLD


async def test_ties_are_broken_by_recording_id() -> None:
    twins = [Candidate("b", {"q": "apple pie"}), Candidate("a", {"q": "apple pie"})]
    assert (await run({"q": "apple tart"}, twins)).candidate is twins[1]


FAR = [Candidate("second", {"q": "bbbb"}), Candidate("first", {"q": "aaaa"})]  # recording order


@pytest.mark.parametrize(
    ("match_spec", "expected"),
    [
        (None, MatchResult(FAR[1], "lexical_weak", 0.0)),  # best ranked: equal scores, by id
        (MatchSpec(inputs=["q"], fallback="lexical"), MatchResult(FAR[1], "lexical_weak", 0.0)),
        (MatchSpec(inputs=["q"], fallback="first"), MatchResult(FAR[0], "first", None)),
        (MatchSpec(inputs=["q"], fallback="none"), MatchResult(None, "below_threshold", 0.0)),
    ],
)
async def test_fallbacks_below_the_threshold(
    match_spec: MatchSpec | None, expected: MatchResult
) -> None:
    assert await run({"q": "zzzz"}, FAR, match_spec) == expected


async def test_jev_picks_the_stand_in() -> None:
    gateway = jev("rec-2")
    result = await run(NEW_QUESTION, match_spec=spec(), gateway=gateway)
    assert result == MatchResult(REACT_HOOKS, "jev", 0.9)
    (call,) = gateway.calls
    assert call["instructions"] == spec().instructions
    assert list(call["options"]) == ["rec-1", "rec-2", "rec-3", "none"]
    assert call["options"]["rec-2"] == "args.repo = facebook/react; args.question = What is a hook?"
    assert call["state"] == {"new_request": NEW_QUESTION}


@pytest.mark.parametrize(
    "gateway",
    [
        jev("rec-2", confidence=0.3),  # not confident enough
        jev("rec-9"),  # not one of the options
        jev(0.7),  # not a choice
        StubJev(None),  # unavailable
        jev("none"),  # nothing fits, but no_match="closest" still answers
    ],
)
async def test_deterministic_choice_when_jev_does_not_decide(gateway: StubJev) -> None:
    result = await run(NEW_QUESTION, match_spec=spec(), gateway=gateway)
    assert (result.candidate, result.method) == (REACT_RECONCILER, "lexical")
    assert len(gateway.calls) == 1


async def test_a_confidence_of_zero_is_enough_when_the_spec_allows_it() -> None:
    result = await run(
        NEW_QUESTION, match_spec=spec(min_confidence=0.0), gateway=jev("rec-3", None)
    )
    assert result == MatchResult(NEXT_ROUTING, "jev", 0.0)


@pytest.mark.parametrize("no_match", ["error", "empty"])
async def test_jev_can_say_nothing_fits(no_match: str) -> None:
    result = await run(NEW_QUESTION, match_spec=spec(no_match=no_match), gateway=jev("none", 0.8))
    assert result == MatchResult(None, "jev_no_match", 0.8)


async def test_jev_needs_a_spec() -> None:
    gateway = jev("rec-2")
    assert (await run(NEW_QUESTION, gateway=gateway)).method == "lexical"
    assert gateway.calls == []


async def test_jev_sees_at_most_the_best_candidates() -> None:
    many = [Candidate(f"rec-{n:03d}", {"args.question": f"question number {n}"}) for n in range(70)]
    gateway = jev("rec-005")
    result = await run({"args.question": "question number 5?"}, many, spec(), gateway)
    assert result.method == "jev"
    assert len(gateway.calls[0]["options"]) == MAX_CANDIDATES + 1


def test_similarity() -> None:
    assert similarity({"q": "apple"}, {"q": "apple"}) == 1.0
    assert similarity({}, {}) == 1.0
    assert similarity({"q": None}, {}) == 1.0
    assert similarity({"q": "apple"}, {"q": "zebra"}) < LEXICAL_THRESHOLD
    close = similarity({"q": "apple pie recipe"}, {"q": "apple pie"})
    assert LEXICAL_THRESHOLD < close < 1.0
    assert close == similarity({"q": "apple pie"}, {"q": "apple pie recipe"})
    assert similarity({"q": "!!!"}, {"q": "???"}) == pytest.approx(0.0)
    assert similarity({"a": "x"}, {"b": "x"}) == 0.0
