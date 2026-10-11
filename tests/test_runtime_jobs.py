from __future__ import annotations

import random
from typing import Any

import pytest

from fauxcall.model.jobs import DurationModel, JobModel
from fauxcall.runtime.jobs import JobEngine, JobView

S = 1_000_000_000
CREATED = 1_791_000_000 * S


def job(states: list[dict[str, Any]], outcomes: list[dict[str, Any]] | None = None) -> JobModel:
    return JobModel.model_validate(
        {"name": "upload", "collection": "uploads", "states": states, "outcomes": outcomes or []}
    )


UPLOAD = job(
    [
        {"name": "queued", "duration": {"kind": "fixed", "seconds": 2}, "progress": [0, 0]},
        {"name": "processing", "duration": {"kind": "fixed", "seconds": 6}, "progress": [0, 100]},
        {"name": "done", "terminal": True, "progress": [100, 100]},
        {"name": "failed", "terminal": True},
    ],
    [{"state": "done", "probability": 0.97}, {"state": "failed", "probability": 0.03}],
)


def at(
    engine: JobEngine,
    model: JobModel,
    elapsed_ns: int,
    job_id: str = "j1",
    outcome: str | None = None,
) -> JobView:
    now = CREATED + elapsed_ns
    return engine.view(model, job_id, created_ns=CREATED, now_ns=now, outcome=outcome)


def test_states_change_exactly_at_the_boundaries() -> None:
    engine = JobEngine(seed=0)
    assert at(engine, UPLOAD, 0).state == "queued"
    assert at(engine, UPLOAD, 2 * S - 1).state == "queued"
    assert at(engine, UPLOAD, 2 * S).state == "processing"
    assert at(engine, UPLOAD, 8 * S - 1).state == "processing"
    final = at(engine, UPLOAD, 8 * S, outcome="done")
    assert (final.state, final.terminal) == ("done", True)


def test_before_creation_the_job_is_in_its_first_state() -> None:
    view = at(JobEngine(seed=0), UPLOAD, -5 * S)
    assert view.state == "queued"
    assert view.state_started_ns == CREATED


def test_progress_is_interpolated_with_exact_integer_floors() -> None:
    engine = JobEngine(seed=0)
    assert at(engine, UPLOAD, 1 * S).progress == 0.0
    assert at(engine, UPLOAD, 5 * S).progress == 50.0
    # floor((3.74 s - 2 s) * 100 / 6 s) is 29; computing x/d first gave 28.999999999999996.
    assert at(engine, UPLOAD, 3_740_000_000).progress == 29.0
    assert int(at(engine, UPLOAD, 8 * S - 1).progress or 0) == 99
    for elapsed in range(2 * S, 8 * S, 60_000_000):
        progress = at(engine, UPLOAD, elapsed).progress
        assert progress is not None
        assert int(progress) == (elapsed - 2 * S) * 100 // (6 * S)


def test_lifecycle_timestamps() -> None:
    engine = JobEngine(seed=0)
    queued = at(engine, UPLOAD, 1 * S)
    assert (queued.started_ns, queued.completed_ns, queued.state_started_ns) == (
        None,
        None,
        CREATED,
    )
    running = at(engine, UPLOAD, 3 * S)
    assert running.started_ns == CREATED + 2 * S
    assert running.state_started_ns == CREATED + 2 * S
    assert running.completed_ns is None
    final = at(engine, UPLOAD, 60 * S, outcome="done")
    assert final.started_ns == CREATED + 2 * S
    assert final.completed_ns == CREATED + 8 * S
    assert final.state_started_ns == CREATED + 8 * S
    assert final.progress == 100


def test_outcome_override_and_terminal_progress() -> None:
    engine = JobEngine(seed=0)
    failed = at(engine, UPLOAD, 9 * S, outcome="failed")
    assert (failed.state, failed.terminal, failed.progress) == ("failed", True, None)
    unknown = at(engine, UPLOAD, 9 * S, outcome="exploded")
    assert (unknown.state, unknown.progress) == ("exploded", None)


def test_outcomes_are_seeded_per_job_and_statistically_sane() -> None:
    engine = JobEngine(seed=1)
    outcomes = [engine.outcome(UPLOAD, f"job-{n}") for n in range(4000)]
    assert set(outcomes) == {"done", "failed"}
    assert 0.015 < outcomes.count("failed") / len(outcomes) < 0.05
    assert [engine.outcome(UPLOAD, f"job-{n}") for n in range(50)] == outcomes[:50]
    assert at(engine, UPLOAD, 9 * S, job_id="job-7").state == outcomes[7]
    other = JobEngine(seed=2)
    assert [other.outcome(UPLOAD, f"job-{n}") for n in range(400)] != outcomes[:400]


def test_outcome_without_declared_outcomes() -> None:
    engine = JobEngine(seed=0)
    two_terminals = job(
        [
            {"name": "running", "duration": {"kind": "fixed", "seconds": 1}},
            {"name": "ok", "terminal": True},
            {"name": "error", "terminal": True},
        ]
    )
    assert engine.outcome(two_terminals, "x") == "ok"
    no_terminal = job([{"name": "a", "duration": {"kind": "fixed", "seconds": 1}}, {"name": "b"}])
    assert engine.outcome(no_terminal, "x") == "b"


def test_rounding_leftovers_fall_back_to_the_last_outcome() -> None:
    class Unlucky(JobEngine):
        def _rng(self, job: JobModel, job_id: str) -> random.Random:  # noqa: ARG002 - base signature
            class Roll(random.Random):
                def random(self) -> float:
                    return 0.99999999

            return Roll()

    nearly_one = job(
        [{"name": "ok", "terminal": True}, {"name": "error", "terminal": True}],
        [{"state": "ok", "probability": 0.5}, {"state": "error", "probability": 0.4999999}],
    )
    assert Unlucky(seed=0).outcome(nearly_one, "x") == "error"


def test_single_running_state_starts_at_once() -> None:
    engine = JobEngine(seed=0)
    simple = job(
        [
            {"name": "processing", "duration": {"kind": "fixed", "seconds": 4}, "progress": [0, 1]},
            {"name": "done", "terminal": True},
        ]
    )
    view = at(engine, simple, 0)
    assert (view.state, view.started_ns, view.progress) == ("processing", CREATED, 0.0)
    assert at(engine, simple, 1 * S).progress == 0.25


def test_zero_duration_states_are_skipped() -> None:
    engine = JobEngine(seed=0)
    instant_queue = job(
        [
            {"name": "queued", "duration": {"kind": "fixed", "seconds": 0}},
            {"name": "checking"},
            {"name": "processing", "duration": {"kind": "fixed", "seconds": 5}},
            {"name": "done", "terminal": True},
        ]
    )
    assert engine.durations_ns(instant_queue, "x") == [0, 0, 5 * S]
    view = at(engine, instant_queue, 0)
    assert (view.state, view.started_ns, view.progress) == ("processing", CREATED, None)
    assert at(engine, instant_queue, 5 * S).state == "done"


def test_a_job_without_running_states_is_done_immediately() -> None:
    engine = JobEngine(seed=0)
    terminal_only = job([{"name": "done", "terminal": True, "progress": [100, 100]}])
    view = at(engine, terminal_only, 0)
    assert (view.state, view.terminal, view.progress) == ("done", True, 100)
    assert view.started_ns == view.completed_ns == CREATED


def test_sampled_durations_are_deterministic_per_job_id() -> None:
    engine = JobEngine(seed=3)
    sampled = job(
        [
            {"name": "a", "duration": {"kind": "uniform", "low": 1, "high": 3}},
            {"name": "b", "duration": {"kind": "lognormal", "mu": 0.5, "sigma": 0.3}},
            {"name": "c", "duration": {"kind": "empirical", "samples": [1.5, 2.5, 4.0]}},
            {"name": "done", "terminal": True},
        ]
    )
    first = engine.durations_ns(sampled, "job-1")
    assert first == engine.durations_ns(sampled, "job-1")
    assert first != engine.durations_ns(sampled, "job-2")
    many = [engine.durations_ns(sampled, f"job-{n}") for n in range(300)]
    assert all(S <= a <= 3 * S for a, _, _ in many)
    assert all(b > 0 for _, b, _ in many)
    assert {c for _, _, c in many} == {1_500_000_000, 2_500_000_000, 4 * S}


def test_negative_measured_durations_are_clamped() -> None:
    engine = JobEngine(seed=0)
    noisy = job(
        [
            {"name": "a", "duration": {"kind": "empirical", "samples": [-2.0]}},
            {"name": "b", "duration": {"kind": "fixed", "seconds": 1}},
            {"name": "done", "terminal": True},
        ]
    )
    assert engine.durations_ns(noisy, "x") == [0, S]
    assert at(engine, noisy, 0).state == "b"


@pytest.mark.parametrize(
    ("kind", "fields"),
    [("fixed", {"seconds": 0.1}), ("uniform", {"low": 0.3, "high": 0.3})],
)
def test_decimal_seconds_become_exact_nanoseconds(kind: str, fields: dict[str, float]) -> None:
    duration = DurationModel.model_validate({"kind": kind, **fields})
    model = job([{"name": "a", "duration": duration.model_dump()}, {"name": "z", "terminal": True}])
    expected = round(next(iter(fields.values())) * S)
    assert JobEngine(seed=0).durations_ns(model, "x") == [expected]
