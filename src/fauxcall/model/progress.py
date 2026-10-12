"""Build checkpoints (``data/progress.json``) so ``/fauxcall`` resumes exactly where it stopped."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from fauxcall.model.common import Model

Phase = Literal[
    "discover",
    "gate",
    "record",
    "model",
    "world",
    "scaffold",
    "implement",
    "unit_tests",
    "e2e_tests",
    "parity",
    "review",
    "report",
]
PHASES: tuple[Phase, ...] = (
    "discover",
    "gate",
    "record",
    "model",
    "world",
    "scaffold",
    "implement",
    "unit_tests",
    "e2e_tests",
    "parity",
    "review",
    "report",
)
EndpointStep = Literal["recorded", "analyzed", "modeled", "implemented", "tested"]
ENDPOINT_STEPS: tuple[EndpointStep, ...] = (
    "recorded",
    "analyzed",
    "modeled",
    "implemented",
    "tested",
)
Status = Literal["pending", "in_progress", "done", "skipped", "blocked"]


def _pending_steps() -> dict[EndpointStep, Status]:
    steps: dict[EndpointStep, Status] = dict.fromkeys(ENDPOINT_STEPS, "pending")
    return steps


class PhaseState(Model):
    """Status of one build phase."""

    status: Status = "pending"
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    notes: list[str] = Field(default_factory=list)


class EndpointProgress(Model):
    """Per-endpoint (or per-tool) progress through the endpoint loop."""

    endpoint: str
    steps: dict[EndpointStep, Status] = Field(default_factory=_pending_steps)
    notes: list[str] = Field(default_factory=list)

    def next_step(self) -> EndpointStep | None:
        """The first step not done or skipped."""
        for step in ENDPOINT_STEPS:
            if self.steps.get(step, "pending") not in {"done", "skipped"}:
                return step
        return None


class GateAnswers(Model):
    """What the user decided at the up-front gate."""

    credentials: Literal["none_needed", "provided", "missing"] = "none_needed"
    jev_key: bool = False
    mutations: Literal["approved", "declined", "none_planned"] = "none_planned"
    notes: list[str] = Field(default_factory=list)
    answered_at: AwareDatetime | None = None


class Progress(Model):
    """The whole build's progress."""

    fake: str
    source: str
    started_at: AwareDatetime
    updated_at: AwareDatetime
    phases: dict[Phase, PhaseState] = Field(
        default_factory=lambda: {phase: PhaseState() for phase in PHASES}
    )
    endpoints: list[EndpointProgress] = Field(default_factory=list)
    gate: GateAnswers | None = None
    log: list[str] = Field(default_factory=list, description="Short notes, newest last.")

    def endpoint(self, endpoint_id: str) -> EndpointProgress:
        """Progress entry for an endpoint, created on first use."""
        for entry in self.endpoints:
            if entry.endpoint == endpoint_id:
                return entry
        entry = EndpointProgress(endpoint=endpoint_id)
        self.endpoints.append(entry)
        return entry

    def next_action(self) -> str:
        """A one-line description of what to do next (used to resume)."""
        for phase in PHASES:
            state = self.phases.get(phase, PhaseState())
            if state.status in {"done", "skipped"}:
                continue
            if phase in {"record", "model"}:
                pending = [
                    e
                    for e in self.endpoints
                    if e.next_step() in {"recorded", "analyzed", "modeled"}
                ]
                if pending:
                    entry = pending[0]
                    return f"{phase}: endpoint {entry.endpoint!r} -> {entry.next_step()}"
            if state.status == "blocked":
                note = state.notes[-1] if state.notes else "see notes"
                return f"{phase}: blocked ({note})"
            if phase == "review" and state.status == "in_progress":
                since = f" since {state.started_at:%Y-%m-%d %H:%M} UTC" if state.started_at else ""
                return (
                    f"review: in progress{since}; if no reviewer is running now, it was "
                    "interrupted: run it again (in the foreground)"
                )
            return f"{phase}: {state.status}"
        return "done"
