"""Asynchronous jobs: state machines whose timing was measured against the real API.

Example: ``POST /uploads`` creates a job; ``GET /uploads/{id}`` reports ``queued`` for ~1s, then
``processing`` for ~8s, then ``done`` (97%) or ``failed`` (3%). The fake derives the state from the
time elapsed on its clock since creation, deterministically per job id, so freezing or accelerating
the clock freezes or accelerates every job.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fauxcall.model.common import Model


class DurationModel(Model):
    """How long a state lasts. ``empirical`` resamples measured durations."""

    kind: Literal["fixed", "uniform", "lognormal", "empirical"] = "fixed"
    seconds: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    high: float | None = Field(default=None, ge=0)
    mu: float | None = None
    sigma: float | None = Field(default=None, ge=0)
    samples: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> DurationModel:
        required = {
            "fixed": ("seconds",),
            "uniform": ("low", "high"),
            "lognormal": ("mu", "sigma"),
            "empirical": ("samples",),
        }[self.kind]
        missing = [name for name in required if getattr(self, name) in (None, [])]
        if missing:
            raise ValueError(f"{self.kind} duration needs {', '.join(missing)}")
        uniform = self.kind == "uniform" and self.low is not None and self.high is not None
        if uniform and self.high < self.low:  # type: ignore[operator]
            raise ValueError("uniform duration needs low <= high")
        return self


class JobStateModel(Model):
    """One state of a job, as it appears in the state field."""

    name: str
    duration: DurationModel | None = Field(default=None, description="None for terminal states.")
    terminal: bool = False
    progress: tuple[float, float] | None = Field(
        default=None, description="Progress range (e.g. 0-100) reported while in this state."
    )


class JobOutcome(Model):
    """A terminal state reached after the last non-terminal state, with its probability."""

    state: str
    probability: float = Field(ge=0.0, le=1.0)


class JobModel(Model):
    """A job state machine bound to a collection of job records."""

    name: str
    collection: str
    id_field: str = "id"
    state_field: str = "status"
    progress_field: str | None = None
    states: list[JobStateModel] = Field(min_length=1)
    outcomes: list[JobOutcome] = Field(default_factory=list)
    outcome_decider: Literal["seeded", "jev"] = Field(
        default="seeded",
        description="'jev' asks Jev (if a key is set) via the endpoint's semantic outcome.",
    )
    timestamps: dict[str, Literal["created", "started", "updated", "completed"]] = Field(
        default_factory=dict, description="Record field -> lifecycle event it reports."
    )
    measured_from: list[str] = Field(
        default_factory=list, description="Exchange ids the timings were measured from."
    )

    @model_validator(mode="after")
    def _consistent(self) -> JobModel:
        names = {state.name for state in self.states}
        for outcome in self.outcomes:
            if outcome.state not in names:
                raise ValueError(f"outcome {outcome.state!r} is not a declared state")
        if self.outcomes and abs(sum(o.probability for o in self.outcomes) - 1.0) > 1e-6:  # noqa: PLR2004
            raise ValueError("outcome probabilities must sum to 1")
        return self
