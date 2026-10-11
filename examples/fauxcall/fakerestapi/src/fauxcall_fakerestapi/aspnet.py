"""ASP.NET Core request handling rules the real API exposes (route binding, media types).

From recordings: ``/Books/+5``, ``/Books/01``, ``/Books/%201``, ``/Books/1%20``, ``/Books/%C2%A01``
(any Unicode white space is trimmed), ``/Books/0x10`` (= 16), ``/Books/0x+1`` and
``/Books/0x000000010`` all bind; ``1e2``, ``1.5``, ``abc``, ``€`` and values outside int32 are
refused with ``The value '<raw>' is not valid.`` — the behavior of .NET's ``Int32Converter``.
"""

from __future__ import annotations

import re
from typing import Final

from fauxcall_fakerestapi.stj import INT32_MAX, INT32_MIN

_DECIMAL: Final = re.compile(r"[+-]?[0-9]+")
_HEX: Final = re.compile(r"\+?0*([0-9a-fA-F]{1,8})|\+?0+")
_JSON_TYPES: Final = frozenset({"application/json", "text/json"})
SUPPORTED_VERSIONS: Final = frozenset({"1.0", "1"})


def bind_int32(raw: str | None) -> int | None:
    """A route value bound to ``int`` (``Int32Converter``), or ``None`` when it is not valid."""
    if raw is None:
        return None
    text = raw.strip()
    hex_digits = _hex_digits(text)
    if hex_digits is not None:
        match = _HEX.fullmatch(hex_digits)
        if match is None:
            return None
        value = int(match.group(1) or "0", 16)
        return value - 2**32 if value > INT32_MAX else value
    if not _DECIMAL.fullmatch(text):
        return None
    value = int(text)
    return value if INT32_MIN <= value <= INT32_MAX else None


def _hex_digits(text: str) -> str | None:
    """The digits after a hex prefix (``#``, ``0x``, ``&h``), or ``None`` without one."""
    if text.startswith("#"):
        return text[1:]
    if text[:2].lower() in {"0x", "&h"}:
        return text[2:]
    return None


def invalid_value(raw: str) -> str:
    """The model-binding message for a route value that isn't a valid int.

    Format and overflow errors name the value; any other converter failure (a hex prefix with no
    digits, or a signed hex number: ``Convert.ToInt32`` throws an ``ArgumentException``) gets the
    generic message — ``/CoverPhotos/0x`` answered ``The input was not valid.`` in the parity run.
    """
    digits = _hex_digits(raw.strip())
    if digits is not None and (not digits or digits.startswith("-")):
        return "The input was not valid."
    return f"The value '{raw}' is not valid."


def media_type(content_type: str | None) -> tuple[str, dict[str, str]]:
    """The media type (lower case) and its parameters."""
    if not content_type:
        return "", {}
    main, *params = content_type.split(";")
    parsed: dict[str, str] = {}
    for param in params:
        name, _, value = param.partition("=")
        parsed[name.strip().lower()] = value.strip().strip('"')
    return main.strip().lower(), parsed


def accepts_json(media: str) -> bool:
    """Whether the JSON input formatter reads this media type (else 415)."""
    return media in _JSON_TYPES or (media.startswith("application/") and media.endswith("+json"))


_VERSION: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
_UNREADABLE_CHARSETS: Final = ("utf-16", "utf-32", "unicode")


def unsupported_version(params: dict[str, str]) -> str | None:
    """The API version requested through the media type's ``v`` parameter, when unsupported.

    Recorded: ``v=2.0`` in Content-Type or Accept is refused on GET, PUT and DELETE alike; an
    unparseable ``v=abc`` is ignored.
    """
    version = params.get("v")
    if version is None or not _VERSION.fullmatch(version) or version in SUPPORTED_VERSIONS:
        return None
    return version


def requested_version(content_type: str | None, accept: str | None) -> str | None:
    """The first unsupported API version named by the Content-Type or Accept media types."""
    for header in (content_type, *(accept or "").split(",")):
        version = unsupported_version(media_type(header)[1])
        if version is not None:
            return version
    return None


def prefers_text_json(accept: str | None) -> bool:
    """Whether the client asked for ``text/json`` before ``application/json`` (recorded P1)."""
    for item in (accept or "").split(","):
        media = media_type(item)[0]
        if media == "text/json":
            return True
        if media == "application/json":
            return False
    return False


def unreadable_charset(params: dict[str, str]) -> bool:
    """A body declared as UTF-16/32: the real API fails with an empty 500 (recorded P15)."""
    return params.get("charset", "").lower().startswith(_UNREADABLE_CHARSETS)
