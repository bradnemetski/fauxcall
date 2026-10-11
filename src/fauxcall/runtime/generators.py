"""Seeded generators for values that are new on every response (ids, trace ids, nonces).

Everything takes an explicit :class:`random.Random` so a session replays identically when the same
calls are made in the same order — repeatable evaluations, realistic-looking values. Lengths of
zero or less give an empty string.
"""

from __future__ import annotations

import base64
import string
import uuid
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

    from fauxcall.types import JsonValue

_ALNUM: Final = string.ascii_letters + string.digits
_NS_PER_MS: Final = 1_000_000
_MILLIS_MASK: Final = (1 << 48) - 1


def uuid4(rng: random.Random) -> str:
    """A version-4 UUID drawn from ``rng``."""
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def uuid7(rng: random.Random, now_ns: int) -> str:
    """A version-7 (RFC 9562) UUID for ``now_ns``: time-ordered across milliseconds."""
    millis = (now_ns // _NS_PER_MS) & _MILLIS_MASK
    value = (millis << 80) | (0x7 << 76) | (rng.getrandbits(12) << 64)
    value |= (0b10 << 62) | rng.getrandbits(62)
    return str(uuid.UUID(int=value))


def hex_string(rng: random.Random, length: int) -> str:
    """``length`` lowercase hexadecimal characters."""
    if length <= 0:
        return ""
    return f"{rng.getrandbits(4 * length):0{length}x}"


def traceparent(rng: random.Random) -> str:
    """A W3C ``traceparent`` value: ``00-<32 hex trace id>-<16 hex span id>-00``."""
    return f"00-{hex_string(rng, 32)}-{hex_string(rng, 16)}-00"


def digits(rng: random.Random, length: int) -> str:
    """``length`` decimal digits (no leading zero, so it parses back to the same number)."""
    if length <= 0:
        return ""
    first = str(rng.randint(1, 9))
    return first + "".join(str(rng.randint(0, 9)) for _ in range(length - 1))


def alnum(rng: random.Random, length: int) -> str:
    """``length`` ASCII letters and digits."""
    return "".join(rng.choice(_ALNUM) for _ in range(max(0, length)))


def base64url(rng: random.Random, length: int) -> str:
    """``length`` characters of unpadded URL-safe base64 (like opaque tokens and cursors)."""
    if length <= 0:
        return ""
    raw = rng.getrandbits(8 * (length + 3)).to_bytes(length + 3, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")[:length]


def choice(rng: random.Random, choices: Sequence[JsonValue]) -> JsonValue:
    """One of ``choices``."""
    if not choices:
        raise ValueError("choice needs at least one option")
    return rng.choice(list(choices))
