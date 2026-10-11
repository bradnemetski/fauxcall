"""Async job state machines evaluated against the fake clock.

A job's state is a pure function of (seed, job id, creation instant, now): durations and the final
outcome are drawn from a generator seeded by the job id, so polling the same job always shows the
same progression, and freezing/accelerating the clock freezes/accelerates every job.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fauxcall.timefmt import NS_PER_SECOND

if TYPE_CHECKING:
    from fauxcall.model.jobs import DurationModel, JobModel


@dataclass(frozen=True, slots=True)
class JobView:
    """A job as seen at one instant."""

    state: str
    terminal: bool
    progress: float | None
    state_started_ns: int
    started_ns: int | None
    completed_ns: int | None


def _sample(duration: DurationModel, rng: random.Random) -> float:
    """Seconds drawn for one state (never negative)."""
    return max(0.0, _draw(duration, rng))


def _draw(duration: DurationModel, rng: random.Random) -> float:
    match duration.kind:
        case "fixed":
            return float(duration.seconds or 0.0)
        case "uniform":
            return rng.uniform(duration.low or 0.0, duration.high or 0.0)
        case "lognormal":
            return rng.lognormvariate(duration.mu or 0.0, duration.sigma or 0.0)
        case _:
            return float(rng.choice(duration.samples))


class JobEngine:
    """Computes job states deterministically."""

    def __init__(self, seed: int) -> None:
        self._seed = seed

    def _rng(self, job: JobModel, job_id: str) -> random.Random:
        digest = hashlib.sha256(f"{self._seed}:{job.name}:{job_id}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def durations_ns(self, job: JobModel, job_id: str) -> list[int]:
        """Duration of every non-terminal state, in order."""
        rng = self._rng(job, job_id)
        return [
            round(_sample(state.duration, rng) * NS_PER_SECOND) if state.duration else 0
            for state in job.states
            if not state.terminal
        ]

    def outcome(self, job: JobModel, job_id: str) -> str:
        """The terminal state this job will end in (seeded by its id)."""
        terminals = [state.name for state in job.states if state.terminal]
        if not job.outcomes:
            return terminals[0] if terminals else job.states[-1].name
        rng = self._rng(job, f"{job_id}:outcome")
        roll = rng.random()
        cumulative = 0.0
        for outcome in job.outcomes:
            cumulative += outcome.probability
            if roll < cumulative:
                return outcome.state
        return job.outcomes[-1].state

    def view(
        self,
        job: JobModel,
        job_id: str,
        *,
        created_ns: int,
        now_ns: int,
        outcome: str | None = None,
    ) -> JobView:
        """The job's state at ``now_ns``; ``outcome`` overrides the seeded terminal state."""
        elapsed = max(0, now_ns - created_ns)
        running = [state for state in job.states if not state.terminal]
        durations = self.durations_ns(job, job_id)
        # "Started" = when the job left its first (waiting) state; single-state jobs start at once.
        started_at = created_ns + durations[0] if len(running) > 1 else created_ns
        boundary = 0
        for index, (state, duration) in enumerate(zip(running, durations, strict=True)):
            if elapsed < boundary + duration:
                progress = None
                if state.progress is not None:
                    low, high = state.progress
                    # Multiply before dividing: exact whenever the true value is an integer,
                    # so integer progress fields floor exactly like the real API's arithmetic.
                    progress = low + (high - low) * (elapsed - boundary) / duration
                return JobView(
                    state=state.name,
                    terminal=False,
                    progress=progress,
                    state_started_ns=created_ns + boundary,
                    started_ns=started_at if index > 0 or len(running) == 1 else None,
                    completed_ns=None,
                )
            boundary += duration
        final = outcome or self.outcome(job, job_id)
        final_state = next((s for s in job.states if s.name == final), None)
        progress = final_state.progress[1] if final_state and final_state.progress else None
        return JobView(
            state=final,
            terminal=True,
            progress=progress,
            state_started_ns=created_ns + boundary,
            started_ns=started_at,
            completed_ns=created_ns + boundary,
        )
