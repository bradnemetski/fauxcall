r"""Lone UTF-16 surrogates, which JavaScript strings carry and Python's UTF-8 can't write.

A client may send ``"\ud800"`` (half an emoji, cut by a slice): ``JSON.parse`` keeps it in the
string, and ``JSON.stringify`` writes it back escaped, so ``echo`` answers ``Echo: \ud800``. The
fake's answers are written as UTF-8, where a lone surrogate can't be encoded. So the front hands
the runtime a stand-in for each (a private-use character, one per surrogate, still one character
long as in JavaScript) and writes the escape back in the answer.
"""

from __future__ import annotations

import re
from typing import Final

_ESCAPE: Final = re.compile(r"\\(?:u([0-9a-fA-F]{4})|.)", re.DOTALL)
_STAND_INS: Final = 0xF0000 - 0xD800
"""Surrogate U+D800-U+DFFF stands in as U+F0000-U+F07FF (plane 15, private use)."""
_STAND_IN: Final = re.compile("[\U000f0000-\U000f07ff]")
_PLANE_15: Final = b"\xf3\xb0"
"""How the stand-ins start in UTF-8 (a quick way to tell an answer holds none)."""


def _surrogate(code: str | None) -> int | None:
    value = int(code, 16) if code is not None else None
    return value if value is not None and 0xD800 <= value <= 0xDFFF else None  # noqa: PLR2004


def hide(text: str) -> str:
    """JSON text with each lone surrogate escape replaced by its stand-in (pairs are kept)."""
    escapes = list(_ESCAPE.finditer(text))
    lone: list[re.Match[str]] = []
    for index, escape in enumerate(escapes):
        unit = _surrogate(escape.group(1))
        if unit is None:
            continue
        if unit < 0xDC00:  # noqa: PLR2004 - a high surrogate pairs with the low one right after
            after = escapes[index + 1] if index + 1 < len(escapes) else None
            low = _surrogate(after.group(1)) if after is not None else None
            if after is not None and after.start() == escape.end() and low and low >= 0xDC00:  # noqa: PLR2004
                continue
        else:
            before = escapes[index - 1] if index > 0 else None
            high = _surrogate(before.group(1)) if before is not None else None
            if before is not None and before.end() == escape.start() and high and high < 0xDC00:  # noqa: PLR2004
                continue
        lone.append(escape)
    for escape in reversed(lone):
        stand_in = chr(int(escape.group(1), 16) + _STAND_INS)
        text = text[: escape.start()] + stand_in + text[escape.end() :]
    return text


def reveal(data: bytes) -> bytes:
    """Whole SSE events with every stand-in written back as the escape ``JSON.stringify`` writes."""
    if _PLANE_15 not in data:
        return data
    text = data.decode("utf-8", errors="replace")
    return _STAND_IN.sub(lambda found: f"\\u{ord(found.group(0)) - _STAND_INS:04x}", text).encode()
