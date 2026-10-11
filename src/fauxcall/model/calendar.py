"""Calendars: trading sessions and publication schedules (market hours, ECB fixings, …)."""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from fauxcall.model.common import Model


class SessionWindow(Model):
    """One open interval in local time, e.g. 09:30-16:00. Several windows model lunch breaks."""

    open: time
    close: time

    @model_validator(mode="after")
    def _ordered(self) -> SessionWindow:
        if self.close <= self.open:
            raise ValueError("session close must be after open (overnight sessions: use two days)")
        return self


class CalendarModel(Model):
    """A named calendar in one time zone.

    Trading example: ``zone="America/New_York"``, sessions 09:30-16:00, Mon-Fri, NYSE holidays.
    Publication example: ``zone="Europe/Berlin"``, ``publications=["16:00"]``, TARGET holidays.
    """

    name: str
    zone: str = Field(description="IANA time zone the times are expressed in.")
    weekdays: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], description="Open weekdays, Monday=0 … Sunday=6."
    )
    sessions: list[SessionWindow] = Field(default_factory=list)
    publications: list[time] = Field(
        default_factory=list, description="Local times at which new data is published."
    )
    holidays: list[date] = Field(default_factory=list)
    early_closes: dict[date, time] = Field(default_factory=dict)
    note: str | None = None

    @field_validator("zone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _local_times(self) -> CalendarModel:
        times = [w.open for w in self.sessions] + [w.close for w in self.sessions]
        times += list(self.publications) + list(self.early_closes.values())
        if any(t.tzinfo is not None for t in times):
            raise ValueError("calendar times are local to 'zone'; remove UTC offsets")
        return self

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):  # noqa: PLR2004 - Sunday
            raise ValueError("weekdays must be between 0 (Monday) and 6 (Sunday)")
        return sorted(set(value))
