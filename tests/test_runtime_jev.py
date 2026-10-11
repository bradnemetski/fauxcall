from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from structlog.testing import capture_logs
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from fauxcall.config import FakeSettings
from fauxcall.jsonfmt import JsonFloat
from fauxcall.runtime.jev import JevAnswer, JevGateway, cache_key

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fauxcall.types import JsonValue

MODEL = "jev-1.13.0"
OPTIONS = {"rec-1": "q = reconciler", "rec-2": "q = hooks", "none": None}
STATE: JsonValue = {"new_request": {"q": "how do hooks work", "price": JsonFloat("1.50")}}

CHOICE = {
    "type": "choice",
    "choice": "rec-2",
    "confidence": 0.9,
    "probabilities": {"rec-1": 0.05, "rec-2": 0.9, "none": 0.05},
}
NOUL = {"type": "noul", "noul": 0.8}
SCORE = {
    "type": "score",
    "score": 1.7,
    "confidence": 0.75,
    "legend": {"0": "low", "1": "medium", "2": "high"},
    "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
}


class FakeJev:
    """A TypeSafe API double behind ``httpx2.MockTransport`` (``/v1/systemone``)."""

    def __init__(self, answers: dict[str, Any] | None = None, *, status: int = 200) -> None:
        self.answers = answers
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.clients: list[tuple[str, str, float]] = []
        self.fail_network = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == "Bearer sk-test"
        self.requests.append(json.loads(request.content))
        if self.fail_network:
            raise httpx2.ConnectError("connection refused", request=request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "overloaded"}})
        return httpx2.Response(
            200,
            json={
                "model": MODEL,
                "answers": self.answers or {},
                "usage": {"input_tokens": 120, "output_tokens": 1},
            },
            headers={"x-typesafe-request-id": "req-1"},
        )

    def factory(self) -> Callable[[str, str, float], AsyncTypeSafeClient]:
        def make(api_key: str, model: str, timeout: float) -> AsyncTypeSafeClient:
            self.clients.append((api_key, model, timeout))
            return AsyncTypeSafeClient(
                api_key=api_key,
                model=model,
                timeout=timeout,
                transport=httpx2.MockTransport(self.handle),
                retry=RetryPolicy(max_retries=0),
            )

        return make


def settings(**fields: Any) -> FakeSettings:
    """Settings from explicit values only (no environment, ``.env`` or ``fauxcall.toml``)."""
    return FakeSettings.model_validate(fields)


KEYED = {"typesafe_api_key": "sk-test"}


def gateway(path: Path | None, fake: FakeJev | None = None, **fields: Any) -> JevGateway:
    factory = fake.factory() if fake is not None else None
    return JevGateway(settings(**(fields or KEYED)), path, client_factory=factory)


def cache_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_live_choice_is_cached_in_memory_and_on_disk(tmp_path: Path) -> None:
    cache = tmp_path / "data" / "jev-cache.jsonl"
    fake = FakeJev({"q": CHOICE})
    jev = gateway(cache, fake)
    assert jev.active
    answer = await jev.choose(instructions="Closest recording?", options=OPTIONS, state=STATE)
    assert answer == JevAnswer(
        kind="choice",
        value="rec-2",
        confidence=0.9,
        probabilities={"rec-1": 0.05, "rec-2": 0.9, "none": 0.05},
        model=MODEL,
    )
    (sent,) = fake.requests
    assert sent == {
        "state": {"new_request": {"q": "how do hooks work", "price": 1.5}},
        "model": MODEL,
        "questions": {
            "q": {"type": "choice", "instructions": "Closest recording?", "criteria": OPTIONS}
        },
    }
    assert fake.clients == [("sk-test", MODEL, 5.0)]
    again = await jev.choose(instructions="Closest recording?", options=OPTIONS, state=STATE)
    assert again == answer
    assert len(fake.requests) == 1
    (line,) = cache_lines(cache)
    assert line["key"] == cache_key(MODEL, "choice", "Closest recording?", dict(OPTIONS), STATE)
    assert line["answer"]["value"] == "rec-2"
    assert jev.cached_answers == 1
    await jev.close()
    await jev.close()


async def test_cached_judgments_replay_without_a_key(tmp_path: Path) -> None:
    cache = tmp_path / "jev-cache.jsonl"
    live = gateway(cache, FakeJev({"q": CHOICE}))
    recorded = await live.choose(instructions="Closest?", options=OPTIONS, state=STATE)
    await live.close()
    offline = JevGateway(settings(), cache)
    assert not offline.active
    assert offline.cached_answers == 1
    assert await offline.choose(instructions="Closest?", options=OPTIONS, state=STATE) == recorded
    assert await offline.choose(instructions="Other?", options=OPTIONS, state=STATE) is None
    other_model = JevGateway(settings(jev={"model": "jev-2.0.0"}), cache)
    assert await other_model.choose(instructions="Closest?", options=OPTIONS, state=STATE) is None


async def test_without_a_key_nothing_is_called(tmp_path: Path) -> None:
    fake = FakeJev({"q": CHOICE})
    for fields in ({}, {"typesafe_api_key": "sk-test", "jev": {"enabled": False}}):
        jev = JevGateway(
            settings(**fields), tmp_path / "cache.jsonl", client_factory=fake.factory()
        )
        assert not jev.active
        assert await jev.probability(instructions="Spam?", state="buy now") is None
        await jev.close()
    assert fake.clients == []


async def test_noul_and_score_answers(tmp_path: Path) -> None:
    fake = FakeJev({"q": NOUL})
    jev = gateway(tmp_path / "cache.jsonl", fake)
    noul = await jev.probability(instructions="Is this about billing?", state="charged twice")
    assert noul == JevAnswer(kind="noul", value=0.8, model=MODEL)
    assert fake.requests[-1]["questions"] == {
        "q": {"type": "noul", "instructions": "Is this about billing?"}
    }
    fake.answers = {"q": SCORE}
    score = await jev.score(instructions="Urgency?", levels=["low", "medium", "high"], state={})
    assert score == JevAnswer(
        kind="score",
        value=1.7,
        confidence=0.75,
        probabilities={"0": 0.1, "1": 0.1, "2": 0.8},
        model=MODEL,
    )
    assert fake.requests[-1]["questions"]["q"]["criteria"] == ["low", "medium", "high"]
    assert jev.cached_answers == 2


@pytest.mark.parametrize(
    ("status", "network"),
    [(500, False), (429, False), (401, False), (200, True)],
)
async def test_failures_fall_back_and_are_not_cached(
    tmp_path: Path, status: int, *, network: bool
) -> None:
    fake = FakeJev({"q": CHOICE}, status=status)
    fake.fail_network = network
    jev = gateway(tmp_path / "cache.jsonl", fake)
    with capture_logs() as logs:
        assert await jev.choose(instructions="Closest?", options=OPTIONS, state=STATE) is None
    assert [entry["event"] for entry in logs] == ["jev_unavailable"]
    assert jev.cached_answers == 0
    fake.status, fake.fail_network = 200, False
    assert await jev.choose(instructions="Closest?", options=OPTIONS, state=STATE) is not None
    assert len(fake.requests) == 2  # retried once the service recovered


@pytest.mark.parametrize(
    "answers",
    [
        {"q": NOUL},  # the wrong kind of answer
        {"other": CHOICE},  # no answer to our question
    ],
)
async def test_unusable_answers_are_ignored(tmp_path: Path, answers: dict[str, Any]) -> None:
    jev = gateway(tmp_path / "cache.jsonl", FakeJev(answers))
    assert await jev.choose(instructions="Closest?", options=OPTIONS, state=STATE) is None
    assert jev.cached_answers == 0


async def test_questions_the_sdk_rejects_fall_back(tmp_path: Path) -> None:
    fake = FakeJev({"q": SCORE})
    jev = gateway(tmp_path / "cache.jsonl", fake)
    with capture_logs() as logs:
        assert await jev.score(instructions="Urgency?", levels=[], state={}) is None
        assert await jev.choose(instructions="Pick", options={}, state={}) is None
    assert [entry["event"] for entry in logs] == ["jev_unavailable"] * 2
    assert fake.requests == []


def test_a_damaged_cache_file_keeps_its_good_lines(tmp_path: Path) -> None:
    cache = tmp_path / "jev-cache.jsonl"
    good = {
        "key": "k-good",
        "answer": {"kind": "noul", "value": 0.25, "model": MODEL},
        "at": "2026-10-10T00:00:00+00:00",
    }
    cache.write_bytes(
        b"\n".join(
            [
                json.dumps(good).encode(),
                b"",
                b'{"key": "k-torn", "answer": {"kind": "no',
                json.dumps({**good, "key": "k-extra", "unknown": 1}).encode(),
                b"\xff\xfe not utf-8",
            ]
        )
        + b"\n"
    )
    with capture_logs() as logs:
        jev = JevGateway(settings(), cache)
    assert jev.cached_answers == 1
    skipped = [entry["line"] for entry in logs if entry["event"] == "jev_cache_line_skipped"]
    assert skipped == [3, 4, 5]


async def test_an_unwritable_cache_still_answers(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    fake = FakeJev({"q": NOUL})
    jev = gateway(blocker / "jev-cache.jsonl", fake)
    with capture_logs() as logs:
        assert await jev.probability(instructions="Spam?", state="hi") is not None
    assert [entry["event"] for entry in logs] == ["jev_cache_write_failed"]
    assert await jev.probability(instructions="Spam?", state="hi") is not None
    assert len(fake.requests) == 1


async def test_memory_only_cache() -> None:
    fake = FakeJev({"q": NOUL})
    jev = gateway(None, fake)
    assert await jev.probability(instructions="Spam?", state="hi") is not None
    assert await jev.probability(instructions="Spam?", state="hi") is not None
    assert len(fake.requests) == 1
    assert jev.cached_answers == 1


def test_cache_keys_are_canonical_and_specific() -> None:
    base = cache_key(MODEL, "choice", "Pick", {"a": "x", "b": None}, {"q": 1, "r": [1, 2]})
    assert base == cache_key(MODEL, "choice", "Pick", {"b": None, "a": "x"}, {"r": [1, 2], "q": 1})
    spelled = cache_key(MODEL, "noul", "Cheap?", None, {"price": JsonFloat("1.50")})
    assert spelled == cache_key(MODEL, "noul", "Cheap?", None, {"price": JsonFloat("1.5")})
    variants = [
        cache_key("jev-2.0.0", "choice", "Pick", {"a": "x", "b": None}, {"q": 1, "r": [1, 2]}),
        cache_key(MODEL, "score", "Pick", {"a": "x", "b": None}, {"q": 1, "r": [1, 2]}),
        cache_key(MODEL, "choice", "Pick!", {"a": "x", "b": None}, {"q": 1, "r": [1, 2]}),
        cache_key(MODEL, "choice", "Pick", {"a": "y", "b": None}, {"q": 1, "r": [1, 2]}),
        cache_key(MODEL, "choice", "Pick", {"a": "x", "b": None}, {"q": 2, "r": [1, 2]}),
    ]
    assert base not in variants
    assert len(set(variants)) == len(variants)
    assert len(base) == 64
