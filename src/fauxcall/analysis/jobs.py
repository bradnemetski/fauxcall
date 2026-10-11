"""Measure async job timings from polling recordings.

The recorder polls a job's status until it is terminal (``purpose: job_poll``, tagged
``poll:<call>``). Each state was observed between two polls, so its duration is bracketed: at least
the time between its first and last sighting, at most the time between the sightings of the states
around it. The midpoint estimate per job feeds a :class:`~fauxcall.model.jobs.DurationModel`.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Final

from pydantic import Field

from fauxcall import jsonpath
from fauxcall.model.common import Model
from fauxcall.model.jobs import DurationModel, JobOutcome

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from fauxcall.model.recording import Exchange

NS: Final = 1_000_000_000
POLL_TAG: Final = "poll:"
FIXED_SPREAD: Final = 0.1
"""Durations whose standard deviation is below this share of their mean become ``fixed``."""
_PROBABILITY_DIGITS: Final = 4


class StateTiming(Model):
    """Observed duration bounds of one state of one job."""

    state: str
    min_s: float
    max_s: float | None

    @property
    def estimate_s(self) -> float:
        """Best single estimate (midpoint of the bounds)."""
        return self.min_s if self.max_s is None else (self.min_s + self.max_s) / 2


class JobTimeline(Model):
    """One job's observed progression."""

    job: str
    observations: list[tuple[float, str]] = Field(description="(seconds since first poll, state)")
    states: list[StateTiming]
    terminal: str | None


class JobTimingSummary(Model):
    """Suggested durations and outcome probabilities across jobs."""

    states: list[str]
    durations: dict[str, DurationModel]
    outcomes: list[JobOutcome]
    jobs: int
    note: str


def timelines(
    exchanges: Sequence[Exchange], *, state_path: str, terminal: Sequence[str]
) -> list[JobTimeline]:
    """Group poll exchanges by their ``poll:<call>`` tag and derive state timings.

    Exchanges without a ``poll:`` tag or without a response are ignored. A terminal state ends a
    job, so when the same plan ran again (same tag) the next non-terminal sighting starts a new
    job, named ``<call>#2``, ``<call>#3`` …
    """
    finals = {state.strip() for state in terminal}
    grouped: defaultdict[str, list[tuple[int, str, str | None]]] = defaultdict(list)
    for exchange in exchanges:
        tag = next((t for t in exchange.tags if t.startswith(POLL_TAG)), None)
        if tag is None or exchange.response is None:
            continue
        state = jsonpath.get_one(exchange.response.body.parsed(), state_path)
        grouped[tag].append(
            (exchange.started_ns, exchange.id, None if state is None else str(state))
        )
    result: list[JobTimeline] = []
    for tag, polls in grouped.items():
        polls.sort(key=lambda poll: (poll[0], poll[1]))
        job = tag.removeprefix(POLL_TAG)
        for number, observations in enumerate(_runs(polls, finals), start=1):
            name = job if number == 1 else f"{job}#{number}"
            result.append(_timeline(name, observations, finals))
    return result


def _runs(
    polls: Sequence[tuple[int, str, str | None]], terminal: Collection[str]
) -> list[list[tuple[float, str]]]:
    """Split time-ordered polls into jobs; observations are seconds since each job's first poll."""
    runs: list[list[tuple[float, str]]] = []
    current: list[tuple[float, str]] = []
    origin = polls[0][0]
    finished = False
    for started_ns, _, state in polls:
        if state is None:
            continue
        if finished and state not in terminal:
            runs.append(current)
            current, origin, finished = [], started_ns, False
        current.append(((started_ns - origin) / NS, state))
        finished = finished or state in terminal
    runs.append(current)
    return runs


def _timeline(
    job: str, observations: list[tuple[float, str]], terminal: Collection[str]
) -> JobTimeline:
    spans: list[tuple[str, float, float]] = []
    for at, state in observations:
        if spans and spans[-1][0] == state:
            spans[-1] = (state, spans[-1][1], at)
        else:
            spans.append((state, at, at))
    states: list[StateTiming] = []
    for index, (state, first, last) in enumerate(spans):
        if state in terminal:
            continue
        previous_last = spans[index - 1][2] if index > 0 else None
        next_first = spans[index + 1][1] if index + 1 < len(spans) else None
        low = last - first
        high = (
            None
            if next_first is None
            else next_first - (previous_last if previous_last is not None else first)
        )
        states.append(
            StateTiming(
                state=state, min_s=round(low, 3), max_s=None if high is None else round(high, 3)
            )
        )
    final = spans[-1][0] if spans and spans[-1][0] in terminal else None
    return JobTimeline(job=job, observations=observations, states=states, terminal=final)


def _duration(values: Sequence[float]) -> DurationModel:
    """``fixed`` when the samples agree (one sample, or a small spread), else ``empirical``."""
    mean = statistics.fmean(values)
    if len(values) == 1 or statistics.pstdev(values) < FIXED_SPREAD * max(mean, 0.01):
        return DurationModel(kind="fixed", seconds=round(mean, 3))
    return DurationModel(kind="empirical", samples=list(values))


def _outcomes(found: Sequence[JobTimeline]) -> list[JobOutcome]:
    """Terminal states by frequency; probabilities rounded but summing to 1."""
    counts = Counter(t.terminal for t in found if t.terminal is not None)
    total = sum(counts.values())
    outcomes = [
        JobOutcome(state=state, probability=round(count / total, _PROBABILITY_DIGITS))
        for state, count in counts.most_common()
    ]
    if outcomes:
        drift = 1.0 - sum(o.probability for o in outcomes)
        outcomes[0] = JobOutcome(
            state=outcomes[0].state,
            probability=round(outcomes[0].probability + drift, _PROBABILITY_DIGITS),
        )
    return outcomes


def summarize(found: Sequence[JobTimeline]) -> JobTimingSummary:
    """Suggest a duration model per state and outcome probabilities."""
    estimates: defaultdict[str, list[float]] = defaultdict(list)
    order: list[str] = []
    for timeline in found:
        for timing in timeline.states:
            if timing.state not in order:
                order.append(timing.state)
            if timing.max_s is not None:
                estimates[timing.state].append(round(timing.estimate_s, 3))
    durations = {state: _duration(values) for state, values in estimates.items()}
    note = (
        f"{len(found)} jobs observed; bounds come from polling, so durations are accurate to "
        "about one poll interval — poll faster for tighter estimates."
    )
    return JobTimingSummary(
        states=order, durations=durations, outcomes=_outcomes(found), jobs=len(found), note=note
    )
