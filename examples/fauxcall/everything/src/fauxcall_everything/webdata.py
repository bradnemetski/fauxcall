"""URLs and files the way Node.js handles them: WHATWG URLs, ``data:`` fetches and gzip.

The real server parses URIs with ``new URL()`` (Node uses the Ada parser, as ``ada_url`` here
does), fetches ``data:`` URLs with undici (the Fetch standard's data-URL processor), and gzips
with zlib at level 6 on macOS (OS byte 19), byte-for-byte what Python's zlib writes once that
byte is set. A fake never reaches the network: ``http(s)`` files are served from recordings.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from typing import Final

import ada_url

GZIP_OS_MACOS: Final = 19
"""zlib's OS code for macOS (``__APPLE__``), where the recorded server ran."""
_BASE64_SUFFIX: Final = re.compile(r";\x20*base64$", re.IGNORECASE)
_ASCII_WHITESPACE: Final = "\t\n\x0c\r "
_BASE64_ALPHABET: Final = re.compile(r"^[A-Za-z0-9+/]*$")


def href(text: str) -> str | None:
    """``new URL(text).toString()``, or ``None`` where Node throws ``Invalid URL``."""
    if not ada_url.check_url(text):
        return None
    return str(ada_url.URL(text).href)


def protocol(text: str) -> str:
    """The scheme of a parsable URL, with its colon (``https:``, ``data:``)."""
    return str(ada_url.URL(text).protocol)


def _percent_decode(text: str) -> bytes:
    raw = text.encode()
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index]
        pair = raw[index + 1 : index + 3]
        if (
            byte == ord("%")
            and len(pair) == len("00")
            and all(chr(b) in "0123456789abcdefABCDEF" for b in pair)
        ):
            out.append(int(pair, 16))
            index += 3
            continue
        out.append(byte)
        index += 1
    return bytes(out)


def _forgiving_base64(data: bytes) -> bytes | None:
    """The forgiving-base64 decode of the HTML standard (``None``: failure)."""
    text = data.decode("latin-1")
    text = "".join(ch for ch in text if ch not in _ASCII_WHITESPACE)
    if len(text) % 4 == 0:
        text = (
            text.removesuffix("=").removesuffix("=")
            if text.endswith("==")
            else text.removesuffix("=")
        )
    if len(text) % 4 == 1 or not _BASE64_ALPHABET.match(text):
        return None
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error:  # pragma: no cover - the alphabet check above rules this out
        return None


def data_url_body(url: str) -> bytes | None:
    """The body a ``fetch()`` of a ``data:`` URL yields (``None``: the fetch fails)."""
    location = href(url)
    if location is None or not location.startswith("data:"):
        return None
    serialized = location.split("#", 1)[0]  # the processor ignores the fragment
    rest = serialized[len("data:") :]
    if "," not in rest:
        return None
    mime, _, encoded = rest.partition(",")
    body = _percent_decode(encoded)
    if _BASE64_SUFFIX.search(mime.strip(_ASCII_WHITESPACE)):
        return _forgiving_base64(body)
    return body


def node_gzip(data: bytes) -> bytes:
    """``zlib.gzipSync(data)`` as the recorded server produced it."""
    compressor = zlib.compressobj(level=6, method=zlib.DEFLATED, wbits=31)
    out = bytearray(compressor.compress(data) + compressor.flush())
    out[9] = GZIP_OS_MACOS
    return bytes(out)
