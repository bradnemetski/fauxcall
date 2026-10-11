"""Where a coordinate or an IP address is, time-zone wise.

Coordinates: the real API resolves land (and territorial waters) with the timezone-boundary-builder
polygons — the same data ``timezonefinder`` ships, offline — and the open sea with nautical zones
``Etc/GMT±n`` (``n`` = longitude / 15 rounded half toward zero; 0 is reported as ``UTC``). A
``NaN`` latitude is accepted and lands on the south pole (``Antarctica/McMurdo``).

The real server's polygons differ from timezonefinder's in one probed place: the Antarctic sector
south of 85°S between 170°W and 180°W is open sea for it (``Etc/GMT+11``/``+12``; recordings
pole-*), not ``Antarctica/McMurdo``. Elsewhere the two data sets may differ near borders.

IP addresses: there is no GeoIP database here, so the fake answers from the addresses the real API
was asked about (``ip_zones.json``, built from recordings by ``scripts/build_ip_table.py``). An
unknown public IPv4 address takes the zone of a recorded neighbour in the same /24, else the same
/16; everything else (private, loopback, IPv6, unknown) is ``UTC``, as the real API answers for
addresses it cannot place.
"""

from __future__ import annotations

import ipaddress
import json
import math
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from timezonefinder import TimezoneFinder

UTC: Final = "UTC"
_HOURS_OF_LONGITUDE: Final = 15.0
_SEA_SECTORS: Final = ((-90.0, -85.0, -180.0, -170.0),)
"""(south, north, west, east) boxes the real API treats as open sea."""


@cache
def _finder() -> TimezoneFinder:
    from timezonefinder import TimezoneFinder  # noqa: PLC0415 - heavy import, only when needed

    return TimezoneFinder(in_memory=True)


def nautical_zone(longitude: float) -> str:
    """The nautical time zone of a longitude, as the real API names it."""
    hours = longitude / _HOURS_OF_LONGITUDE
    n = int(math.copysign(math.ceil(abs(hours) - 0.5), hours))
    if n == 0:
        return UTC
    return f"Etc/GMT{'-' if n > 0 else '+'}{abs(n)}"


def zone_at(latitude: float, longitude: float) -> str:
    """The zone at a (valid) coordinate."""
    if math.isnan(latitude):
        latitude = -90.0
    if math.isnan(longitude):
        longitude = 0.0
    at_sea = any(
        south <= latitude <= north and west <= longitude <= east
        for south, north, west, east in _SEA_SECTORS
    )
    found = None if at_sea else _finder().timezone_at(lat=latitude, lng=longitude)
    if found is None or found.startswith("Etc/"):
        return nautical_zone(longitude)
    return found


@cache
def _ip_table() -> dict[str, str]:
    text = files("fauxcall_timeapi").joinpath("ip_zones.json").read_text(encoding="utf-8")
    table: dict[str, str] = json.loads(text)
    return table


def _neighbour(address: ipaddress.IPv4Address) -> str | None:
    table = _ip_table()
    for prefix in (24, 16):
        network = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
        zones = {
            zone
            for known, zone in table.items()
            if ":" not in known and ipaddress.IPv4Address(known) in network
        }
        if len(zones) == 1:
            return zones.pop()
    return None


def zone_of_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The zone the real API reports for an address."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is None:
            return _ip_table().get(str(address), UTC)
        address = address.ipv4_mapped
    known = _ip_table().get(str(address))
    if known is not None:
        return known
    if not address.is_global:
        return UTC
    return _neighbour(address) or UTC
