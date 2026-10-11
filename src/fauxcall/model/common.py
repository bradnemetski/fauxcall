"""Building blocks shared by the model modules."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, SkipValidation

from fauxcall.types import JsonValue


class Model(BaseModel):
    """Base for every persisted model: strict about unknown keys so typos surface immediately."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)


RawJson = Annotated[JsonValue, SkipValidation]
"""Arbitrary JSON payload (bodies, records, templates).

Not validated by pydantic so :class:`fauxcall.jsonfmt.JsonFloat` spellings survive; persisted
through :func:`fauxcall.storage.write_model`, which keeps them.
"""

Purpose = Literal[
    "sample",
    "variation",
    "edge",
    "invalid",
    "auth",
    "time_profile",
    "series",
    "job_poll",
    "write_readback",
    "pagination",
    "latency",
    "rate_limit",
    "discovery",
    "cleanup",
    "parity",
]
"""Why a call was made. Analysis tools filter recordings by purpose."""

ZoneSpec = str
"""Where a field's time zone comes from.

* an IANA zone name: ``"UTC"``, ``"America/New_York"``;
* ``"param:<name>"``: the value of a request parameter (e.g. ``"param:timeZone"``);
* ``"field:<path>"``: a field of the same record (e.g. ``"field:timezone"``);
* ``"calendar"``: (calendar policies only) the calendar's own zone.
"""
