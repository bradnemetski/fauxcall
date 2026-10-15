"""The consistent world: collections of records, the relations between them, and invariants."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from fauxcall.model.common import Model, RawJson

KeyType = Literal["int", "str", "uuid"]


class ExpansionSpec(Model):
    """Build-time world expansion: generate extra records from the recorded profile, then freeze.

    Used when the real list is larger than what was recorded (e.g. the real API reports
    ``total: 10342``) so pagination and totals behave like the real thing.
    """

    target_count: int = Field(ge=1, description="Total records after expansion.")
    seed: int = 0
    vary: list[str] = Field(
        default_factory=list, description="Field paths to vary (others are copied from a template)."
    )
    note: str | None = None


class CollectionModel(Model):
    """A set of records of one entity type (pets, tickers, exchanges, jobs …)."""

    name: str
    key: str = Field(default="id", description="Field path of the primary key.")
    key_type: KeyType = "int"
    file: str | None = Field(default=None, description="Defaults to 'world/<name>.jsonl'.")
    sources: list[str] = Field(default_factory=list, description="Endpoint ids it was built from.")
    extract: dict[str, str] = Field(
        default_factory=dict,
        description="Endpoint id -> JSON path of its records, e.g. '$[*]', '$.data[*]', '$'.",
    )
    purposes: list[str] = Field(
        default_factory=lambda: [
            "sample",
            "variation",
            "edge",
            "pagination",
            "time_profile",
            "series",
        ],
        description=(
            "Recording purposes that feed the world (add write_readback/job_poll when the "
            "real server keeps resources created while recording)."
        ),
    )
    record_schema: RawJson = Field(default=None, description="Inferred JSON schema of a record.")
    order: Literal["recorded", "key_asc", "key_desc"] = "recorded"
    expansion: ExpansionSpec | None = None
    description: str | None = None

    @property
    def path(self) -> str:
        """Relative path of the collection's JSONL file inside the data directory."""
        return self.file or f"world/{self.name}.jsonl"


class RelationModel(Model):
    """A reference from one collection's field to another collection's key, in both directions.

    Example: every ``tickers[].exchange`` must exist in ``exchanges[].code`` (forward), and every
    exchange lists at least one ticker (``min_per_target: 1``, the reverse).

    Three decisions say how the real API keeps it; ``null`` means not decided yet (the fake is
    lenient, as if accepting, and ``fauxcall model check`` lists it). Decide each from recorded
    probes and domain knowledge, and say why in ``rules_evidence``.
    """

    name: str
    from_collection: str
    from_field: str = Field(description="Path in the referencing record; '[*]' for arrays.")
    to_collection: str
    to_field: str | None = Field(default=None, description="Defaults to the target's key.")
    required: bool = Field(default=True, description="Every non-null reference must resolve.")
    on_missing: Literal["refuse", "accept"] | None = Field(
        default=None,
        description=(
            "A write that points at a target that doesn't exist: 'refuse' (the endpoint's "
            "missing_reference error case) or 'accept' (the real API stores it anyway)."
        ),
    )
    on_delete: Literal["ignore", "restrict", "cascade", "set_null"] | None = Field(
        default=None,
        description=(
            "Deleting a target that is still referenced: 'restrict' (refused with the "
            "endpoint's still_referenced error case), 'cascade' (its referrers are deleted too), "
            "'set_null' (their references are cleared) or 'ignore' (they are left dangling)."
        ),
    )
    min_per_target: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The reverse: every target is referenced by at least this many records (1: every "
            "exchange has a company; 0: cities need no team, unless it's a sports API). Holds "
            "for the world as built and expanded; writes may break it, as on the real API."
        ),
    )
    evidence: str | None = Field(default=None, description="How the relation was established.")
    rules_evidence: str | None = Field(
        default=None,
        description="Why on_missing, on_delete and min_per_target are what they are.",
    )


class InvariantModel(Model):
    """A cross-record rule the world must satisfy (checked by tests and after every build)."""

    name: str
    kind: Literal["unique", "not_null", "custom"]
    collection: str
    field: str | None = None
    description: str
    function: str | None = Field(
        default=None, description="For 'custom': dotted path of a check function in the fake."
    )


class WorldRecord(Model):
    """One line of a collection's JSONL file."""

    key: RawJson
    record: RawJson
    recorded_at: AwareDatetime | None = Field(
        default=None, description="When the source response was received (anchors 'relative')."
    )
    source: str | None = Field(default=None, description="Exchange id the record came from.")
    synthetic: bool = Field(default=False, description="Generated by world expansion.")
