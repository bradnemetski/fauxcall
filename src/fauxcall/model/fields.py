"""Field policies: how each response field behaves over time and across calls.

A policy is attached to a JSON path inside a record (or a whole response body). Fields without a
policy are *static*: returned exactly as recorded. The policies capture what makes fakes feel real:

* ``now`` — tracks the clock (``GET /current_time``).
* ``relative`` — keeps its recorded distance from "now" (``dueDate`` = now + 1h; ``last_login`` =
  2 days ago).
* ``calendar`` — follows a trading/publication calendar: live during market hours, frozen at the
  last tick overnight; or the date of the last publication.
* ``periodic`` — snaps to a period (``updated_at`` every 5 minutes).
* ``random`` / ``counter`` — new value per response (request ids, trace ids, sequence numbers).
* ``echo`` — copied from the request (``base`` currency, ``amount``).
* ``series`` — replays a recorded sequence of values aligned to the clock (opt-in for prices).
* ``volatile`` — frozen at the last recording (prices by default); parity compares type only.
* ``pseudonym`` — PII replaced at recording time; parity compares via the pseudonym key.
* ``computed`` — filled by custom code.
* ``ignore`` — returned as recorded, never compared by parity tests.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from fauxcall.model.common import Model, RawJson, ZoneSpec
from fauxcall.timefmt import TimeFormat

PolicyScope = Literal["record", "body"]


class _Policy(Model):
    path: str = Field(description="JSON path of the field, relative to the record or body.")
    scope: PolicyScope = Field(
        default="record",
        description="'record': path inside each collection record; 'body': inside the response.",
    )
    note: str | None = None


class StaticPolicy(_Policy):
    """Returned exactly as recorded (the default for fields without a policy)."""

    kind: Literal["static"] = "static"


class NowPolicy(_Policy):
    """The current time of the fake's clock, rendered in the recorded format."""

    kind: Literal["now"] = "now"
    format: TimeFormat
    zone: ZoneSpec = "UTC"
    lag_ms: int = Field(
        default=0, ge=0, description="Subtract this much (e.g. a feed's typical delay)."
    )


class RelativePolicy(_Policy):
    """Keeps the distance to 'now' observed when the record was recorded."""

    kind: Literal["relative"] = "relative"
    format: TimeFormat
    zone: ZoneSpec = "UTC"


CalendarEvent = Literal["last_tick", "last_close", "last_open", "last_publication", "session_date"]


class CalendarPolicy(_Policy):
    """Follows a named calendar.

    ``last_tick``: now (minus ``lag_ms``) while a session is open, else the last session close.
    ``last_close`` / ``last_open``: the most recent session close / open.
    ``last_publication``: the most recent scheduled publication (e.g. ECB rates at 16:00 CET).
    ``session_date``: the date of the current or most recent session.
    """

    kind: Literal["calendar"] = "calendar"
    calendar: str
    event: CalendarEvent = "last_tick"
    format: TimeFormat
    zone: ZoneSpec = "UTC"
    lag_ms: int = Field(default=0, ge=0)


class PeriodicPolicy(_Policy):
    """Snaps the clock down to a period boundary (plus an optional phase offset)."""

    kind: Literal["periodic"] = "periodic"
    period_seconds: float = Field(gt=0)
    phase_seconds: float = 0.0
    format: TimeFormat
    zone: ZoneSpec = "UTC"


RandomGenerator = Literal[
    "uuid4", "uuid7", "hex", "traceparent", "digits", "alnum", "base64url", "choice"
]


class RandomPolicy(_Policy):
    """A fresh value on every response (seeded per request, so repeatable)."""

    kind: Literal["random"] = "random"
    generator: RandomGenerator
    length: int | None = Field(default=None, ge=1, description="For hex/digits/alnum/base64url.")
    choices: list[RawJson] = Field(default_factory=list, description="For 'choice'.")
    prefix: str = ""
    as_number: bool = Field(default=False, description="Render digits as a JSON number.")


class CounterPolicy(_Policy):
    """Monotonic per-fake counter (sequence numbers, trade ids)."""

    kind: Literal["counter"] = "counter"
    start: int = 1
    step: int = 1
    as_string: bool = False


EchoTransform = Literal["as_is", "int", "float", "upper", "lower", "bool"]


class EchoPolicy(_Policy):
    """Copied from the request: ``query.<n>``, ``path.<n>``, ``header.<n>`` or ``body.<path>``."""

    kind: Literal["echo"] = "echo"
    source: str
    transform: EchoTransform = "as_is"
    default: RawJson = None


class SeriesPolicy(_Policy):
    """Replays a recorded series (``data/series/<name>.jsonl``) aligned to the clock (opt-in)."""

    kind: Literal["series"] = "series"
    series: str
    mode: Literal["loop", "hold_last"] = "loop"


class VolatilePolicy(_Policy):
    """A value that changes in reality but is frozen at its last recording (prices by default)."""

    kind: Literal["volatile"] = "volatile"


class PseudonymPolicy(_Policy):
    """PII replaced at recording time; parity tests compare through the pseudonym key."""

    kind: Literal["pseudonym"] = "pseudonym"
    category: str = Field(description="email, name, username, phone, address, ip, id, text …")


class ComputedPolicy(_Policy):
    """Filled by custom code in the fake (``function`` names it for documentation)."""

    kind: Literal["computed"] = "computed"
    function: str


class IgnorePolicy(_Policy):
    """Returned as recorded and never compared by parity tests."""

    kind: Literal["ignore"] = "ignore"


FieldPolicy = Annotated[
    StaticPolicy
    | NowPolicy
    | RelativePolicy
    | CalendarPolicy
    | PeriodicPolicy
    | RandomPolicy
    | CounterPolicy
    | EchoPolicy
    | SeriesPolicy
    | VolatilePolicy
    | PseudonymPolicy
    | ComputedPolicy
    | IgnorePolicy,
    Field(discriminator="kind"),
]
"""Any field policy, discriminated by ``kind``."""

TIME_POLICY_KINDS = frozenset({"now", "relative", "calendar", "periodic"})
"""Policies whose value is an instant rendered with a :class:`~fauxcall.timefmt.TimeFormat`."""
