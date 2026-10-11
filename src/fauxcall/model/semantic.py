"""Runtime judgments a fake delegates to Jev (only when the user configured a key).

Inputs are request references: ``query.<name>``, ``path.<name>``, ``header.<name>``,
``body.<path>`` and, for MCP tools, ``args.<path>``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from fauxcall.model.common import Model, RawJson


class MatchSpec(Model):
    """Answer an unseen request with the closest recorded one.

    Example: DeepWiki's ``ask_question(repoName, question)`` with a new question is answered with
    the recording whose question is most similar for the same repository.
    """

    inputs: list[str] = Field(min_length=1)
    exact: list[str] = Field(
        default_factory=list, description="Inputs that must match exactly (e.g. 'args.repoName')."
    )
    instructions: str = Field(
        default=(
            "A client sent a new request. Which recorded request is the closest stand-in, i.e. "
            "would have the most appropriate response for the new request?"
        )
    )
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    fallback: Literal["lexical", "first", "none"] = "lexical"
    no_match: Literal["closest", "error", "empty"] = "closest"
    no_match_template: str | None = None


class SemanticOutput(Model):
    """A response field whose value depends on the *meaning* of the input."""

    path: str
    kind: Literal["choice", "noul", "score"]
    instructions: str
    inputs: list[str] = Field(min_length=1)
    options: dict[str, str] = Field(
        default_factory=dict, description="choice: value -> description. score: level -> desc."
    )
    fallback: RawJson = None


class OutcomeSpec(Model):
    """Decide which outcome the real API would produce for this request."""

    instructions: str
    inputs: list[str] = Field(min_length=1)
    outcomes: dict[str, str] = Field(min_length=2, description="Outcome -> description.")
    fallback: str
    error_templates: dict[str, str] = Field(
        default_factory=dict, description="Outcome -> error template to respond with."
    )


class SemanticModel(Model):
    """All Jev-backed behavior of one endpoint or tool."""

    match: MatchSpec | None = None
    outputs: list[SemanticOutput] = Field(default_factory=list)
    outcome: OutcomeSpec | None = None
