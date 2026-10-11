"""Deterministic, format-preserving pseudonyms for personal data.

Every pseudonym is a pure function of ``(key, category, value)``: all randomness is derived from
``hmac.new(key, f"{category}:{value}".encode(), sha256)``. Hence

* the same real value becomes the same fake in every recording and endpoint, so references
  survive (``owner_email`` still matches ``users[*].email``);
* parity tests holding the same key (from ``.env``) can :meth:`Pseudonymizer.translate` a real
  value into what the fake knows;
* without the key the mapping cannot be reversed or recomputed.

Values are normalized before hashing where spelling does not change identity: emails, names and
addresses are case-insensitive, phone/card/SSN numbers are hashed by their digits (so
``+1 555-123-4567`` and ``+15551234567`` get the same digits back), IP addresses by their
canonical form and dates of birth by the calendar date. Pseudonyms keep the original's format
(separators, length, case style, timestamp spelling) so fakes look real.

Names and addresses come from `Faker <https://faker.readthedocs.io>`_ (``en_US``) seeded from the
HMAC; they are stable for a given Faker release, so pin Faker (``uv.lock``) where recordings are
made and where parity tests run.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
from datetime import UTC, datetime, timezone
from functools import cache
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, quote_plus

from fauxcall.jsonpath import format_path, iter_matches, parse
from fauxcall.logs import REDACTED
from fauxcall.privacy.pii import as_pii_category, luhn_check_digit, parse_ip
from fauxcall.timefmt import NS_PER_SECOND, TimeFormat, infer_time_format, iso_offset

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from datetime import tzinfo

    from faker import Faker

    from fauxcall.jsonpath import Segment
    from fauxcall.privacy.pii import PiiCategory
    from fauxcall.types import JsonValue

KEY_BYTES: Final = 32
"""Random bytes in a key made by :meth:`Pseudonymizer.generate_key`."""

MAX_DOB_SHIFT_DAYS: Final = 365
"""Dates of birth move by 1 to this many days, earlier or later."""

MIN_TEXT_LENGTH: Final = 2
"""Shorter real values are not replaced inside free text (a lone initial would hit everything)."""

_DIGITS: Final = frozenset("0123456789")
_SSN_DIGITS: Final = 9
_NS_PER_DAY: Final = 86_400 * NS_PER_SECOND
_COMPACT_DATE: Final = TimeFormat(kind="strftime", pattern="%Y%m%d")
_COMPACT_DATE_RE: Final = re.compile(r"\d{8}")
_WHITESPACE_RE: Final = re.compile(r"(\s+)")
_WORD_CORE_RE: Final = re.compile(r"\w(?:.*\w)?", re.DOTALL)
_SECONDARY_RE: Final = re.compile(
    r"(?:apt|apartment|suite|ste|unit|flat|room|floor|fl)\b|#", re.IGNORECASE
)
_IPV4_RE: Final = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_BRACKETED_RE: Final = re.compile(r"(?<=\[)[0-9A-Fa-f:.]+(?=\])")
_HONORIFICS: Final = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mx",
        "dr",
        "prof",
        "sir",
        "dame",
        "rev",
        "jr",
        "sr",
        "ii",
        "iii",
        "iv",
        "phd",
        "md",
        "esq",
    }
)
"""Titles and suffixes kept as they are inside a person's name."""

# ITU-T E.164 country calling codes are prefix-free: 1 and 7 are one digit, these are two digits,
# everything else is three.
_ONE_DIGIT_CODES: Final = frozenset({"1", "7"})
_TWO_DIGIT_CODES: Final = frozenset(
    {
        "20",
        "27",
        "30",
        "31",
        "32",
        "33",
        "34",
        "36",
        "39",
        "40",
        "41",
        "43",
        "44",
        "45",
        "46",
        "47",
        "48",
        "49",
        "51",
        "52",
        "53",
        "54",
        "55",
        "56",
        "57",
        "58",
        "60",
        "61",
        "62",
        "63",
        "64",
        "65",
        "66",
        "81",
        "82",
        "84",
        "86",
        "90",
        "91",
        "92",
        "93",
        "94",
        "95",
        "98",
    }
)
_MAX_CODE_DIGITS: Final = 3
_IPV4_HOSTS: Final = 254
_MIN_CARD_DIGITS: Final = 2
_BYTE: Final = 256
_IPV6_RANDOM_BITS: Final = 120
_ULA_PREFIX: Final = 0xFD << _IPV6_RANDOM_BITS


# ---------------------------------------------------------------------------
# Deterministic randomness
# ---------------------------------------------------------------------------


class _Entropy:
    """An endless, deterministic byte stream expanded from one HMAC digest (HKDF-expand style)."""

    __slots__ = ("_block", "_pool", "_seed")

    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._block = 0
        self._pool = b""

    def _take(self, count: int) -> bytes:
        while len(self._pool) < count:
            counter = self._block.to_bytes(4, "big")
            self._pool += hmac.new(self._seed, counter, hashlib.sha256).digest()
            self._block += 1
        taken, self._pool = self._pool[:count], self._pool[count:]
        return taken

    def below(self, bound: int) -> int:
        """A number in ``range(bound)`` (64 bits per draw keeps the modulo bias negligible)."""
        return int.from_bytes(self._take(8), "big") % bound

    def bits(self, count: int) -> int:
        """A number with ``count`` random bits."""
        size = (count + 7) // 8
        return int.from_bytes(self._take(size), "big") >> (size * 8 - count)

    def digit(self) -> str:
        """One decimal digit."""
        return str(self.below(10))

    def letter(self) -> str:
        """One uppercase ASCII letter."""
        return chr(ord("A") + self.below(26))


_FAKER_LOCK: Final = threading.Lock()


@cache
def _faker() -> Faker:
    from faker import Faker  # noqa: PLC0415 - importing Faker costs ~0.1 s; only names need it

    return Faker("en_US")


def _fake(seed: bytes, generate: Callable[[Faker], str]) -> str:
    """Run a Faker generator seeded with ``seed`` (seeding and generating happen atomically)."""
    with _FAKER_LOCK:
        faker = _faker()
        faker.seed_instance(int.from_bytes(seed, "big"))
        return generate(faker)


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def _digits_of(text: str) -> str:
    return "".join(char for char in text if char in _DIGITS)


def _fill_digits(template: str, digits: Iterable[str]) -> str:
    """Replace the ASCII digits of ``template``, in order, with ``digits``."""
    replacements = iter(digits)
    return "".join(next(replacements) if char in _DIGITS else char for char in template)


def _match_case(original: str, generated: str) -> str:
    """Give ``generated`` the case style of ``original`` when it is all upper or all lower case."""
    if original.isupper():
        return generated.upper()
    if original.islower():
        return generated.lower()
    return generated


def _country_code_length(digits: str) -> int:
    if digits[:1] in _ONE_DIGIT_CODES:
        return 1
    if digits[:2] in _TWO_DIGIT_CODES:
        return 2
    return min(_MAX_CODE_DIGITS, len(digits))


def _is_number(value: JsonValue) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _date_format(value: JsonValue) -> tuple[TimeFormat, tzinfo] | None:
    """How a date of birth is spelled (any format :mod:`fauxcall.timefmt` reproduces exactly)."""
    fmt = infer_time_format([value])
    if fmt is None and isinstance(value, str) and _COMPACT_DATE_RE.fullmatch(value):
        fmt = _COMPACT_DATE  # 19900115 reads as an epoch to timefmt, but is a date here
    if fmt is None or fmt.kind == "time":
        return None
    offset = iso_offset(value) if fmt.kind == "iso" and isinstance(value, str) else None
    return fmt, (UTC if offset is None else timezone(offset))


def _copy_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _copy_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json(child) for child in value]
    return value


def _scalars(
    node: JsonValue, trail: tuple[Segment, ...]
) -> Iterator[tuple[tuple[Segment, ...], JsonValue]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _scalars(child, (*trail, key))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _scalars(child, (*trail, index))
    else:
        yield trail, node


def _replace(root: JsonValue, trail: tuple[Segment, ...], new: JsonValue) -> JsonValue:
    if not trail:
        return new
    parent = root
    for segment in trail[:-1]:
        parent = parent[segment]  # type: ignore[index]
    parent[trail[-1]] = new  # type: ignore[index]
    return root


# ---------------------------------------------------------------------------
# Replacing known values inside free text
# ---------------------------------------------------------------------------

type _Trie = dict[str, _Trie]
_END: Final = ""


def _trie_regex(words: Iterable[str]) -> str:
    """One regex alternation for many literals, shaped as a trie so matching stays fast.

    Optional groups are greedy, so the longest known value wins at every position.
    """
    root: _Trie = {}
    for word in words:
        node = root
        for char in word:
            node = node.setdefault(char, {})
        node[_END] = {}
    return _node_regex(root)


def _node_regex(node: _Trie) -> str:
    prefix: list[str] = []
    while len(node) == 1 and _END not in node:  # unbranched runs need no group (or recursion)
        ((char, node),) = node.items()
        prefix.append(re.escape(char))
    branches = [re.escape(char) + _node_regex(child) for char, child in node.items() if char]
    body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    if _END in node:
        body = f"(?:{body})?" if branches else ""
    return "".join(prefix) + body


def _quote_all(text: str) -> str:
    return quote(text, safe="")


_ENCODINGS: Final[tuple[Callable[[str], str], ...]] = (_quote_all, quote_plus, quote)


class _TextIndex:
    """Every known real value (and its percent-encoded spellings) mapped to its pseudonym."""

    __slots__ = ("pattern", "table")

    def __init__(self, mapping: Mapping[str, str]) -> None:
        usable = [(real, fake) for real, fake in mapping.items() if len(real) >= MIN_TEXT_LENGTH]
        self.table: dict[str, str] = dict(usable)
        for real, fake in usable:
            for encode in _ENCODINGS:
                self.table.setdefault(encode(real), encode(fake))
        self.pattern = (
            re.compile(rf"(?<![^\W_])(?:{_trie_regex(self.table)})(?![^\W_])")
            if self.table
            else None
        )


# ---------------------------------------------------------------------------
# The pseudonymizer
# ---------------------------------------------------------------------------


class Pseudonymizer:
    """Replaces personal data with deterministic, format-preserving fakes derived from a key.

    Pseudonyms per category:

    * ``email``: ``user.<10 hex>@example.com``.
    * ``phone``: every digit replaced; ``+``, separators, spacing, length and the country code
      after ``+`` are kept.
    * ``first_name``/``last_name``: Faker names, one per word. ``person_name`` maps each word too
      (``Jane Q. Doe`` → ``Mary K. Smith``, ``Doe, Jane`` → ``Smith, Mary``), keeping titles,
      initials and the number of words, so a full name stays consistent with its parts.
    * ``username``: ``user_<8 hex>`` (upper case when the original was).
    * ``street``/``address``/``city``: Faker values shaped like the original (a unit such as
      ``Apt. 5``, a street with or without a house number, a one-line or multi-line address).
    * ``postal_code``: digits stay digits and letters stay letters; case and separators are kept.
    * ``ip``: IPv4 → ``10.x.y.z``, IPv6 → ``fdxx::/8``; IPv4 addresses inside longer text and
      bracketed IPv6 (``[2001:db8::1]:443``) are replaced too.
    * ``credit_card``: same length and separators, first digit kept, valid Luhn checksum.
    * ``ssn``: ``9XX-XX-XXXX`` (area 9XX is never issued) with the original separators.
    * ``date_of_birth``: moved 1-365 days earlier or later and spelled exactly like the original;
      unparseable values get new digits instead.
    * ``free_text``: ``text-<12 hex>``.

    Non-string values are returned unchanged, except numeric (epoch) dates of birth. Empty strings
    and :data:`~fauxcall.privacy.REDACTED` are never replaced.

    Every real string replaced so far is remembered (:attr:`mapping`) so that
    :meth:`pseudonymize_text` can also replace it inside URLs, query strings and text bodies.
    Instances are not thread-safe.
    """

    def __init__(self, key: str | bytes) -> None:
        raw = key.encode() if isinstance(key, str) else bytes(key)
        if not raw:
            raise ValueError("the pseudonym key must not be empty")
        self._key = raw
        self._mapping: dict[str, str] = {}
        self._index: _TextIndex | None = None
        self._generators: dict[PiiCategory, Callable[[str], str]] = {
            "email": self._email,
            "phone": self._phone,
            "person_name": self._person_name,
            "first_name": self._first_name,
            "last_name": self._last_name,
            "username": self._username,
            "address": self._address,
            "street": self._street,
            "city": self._city,
            "postal_code": self._postal_code,
            "ip": self._ip,
            "credit_card": self._credit_card,
            "ssn": self._ssn,
            "date_of_birth": self._date_of_birth,
            "free_text": self._free_text,
        }

    def __repr__(self) -> str:
        return f"Pseudonymizer(key=<hidden>, known_values={len(self._mapping)})"

    @staticmethod
    def generate_key() -> str:
        """A new random key (32 bytes, URL-safe base64) for the ``.env`` file on first run."""
        return secrets.token_urlsafe(KEY_BYTES)

    @property
    def mapping(self) -> dict[str, str]:
        """A copy of every real string replaced so far, mapped to its pseudonym."""
        return dict(self._mapping)

    # -- public API ----------------------------------------------------------------------------

    def pseudonym(self, value: JsonValue, category: PiiCategory) -> JsonValue:
        """The pseudonym of ``value`` as ``category`` (deterministic for a given key).

        Raises:
            ValueError: ``category`` is not a :data:`~fauxcall.privacy.PiiCategory`.
        """
        checked = as_pii_category(category)
        if checked == "date_of_birth" and _is_number(value):
            return self._numeric_date_of_birth(value)
        if not isinstance(value, str) or not value.strip() or value == REDACTED:
            return value
        result = self._generators[checked](value)
        self._remember(value, result)
        return result

    def translate(self, value: JsonValue, category: PiiCategory) -> JsonValue:
        """Map a real value to the pseudonym the fake knows (alias of :meth:`pseudonym`).

        Parity tests use it to turn real inputs/outputs into what the fake was recorded with.
        """
        return self.pseudonym(value, category)

    def pseudonymize_json(
        self, value: JsonValue, categories: Mapping[str, PiiCategory]
    ) -> tuple[JsonValue, list[str]]:
        """Pseudonymize every field matched by ``categories`` in a copy of ``value``.

        Args:
            value: A JSON document; it is not modified.
            categories: Generalized paths (``$.users[*].email``, as returned by
                :func:`~fauxcall.privacy.detect_pii`) mapped to categories. A path that matches
                an object or array applies its category to every scalar below it. When several
                paths match the same field, the first one wins.

        Returns:
            The pseudonymized copy and the concrete paths (``$.users[0].email``) that changed.
        """
        result = _copy_json(value)
        changed: list[str] = []
        seen: set[tuple[Segment, ...]] = set()
        for path, category in categories.items():
            checked = as_pii_category(category)
            for trail, match in list(iter_matches(result, parse(path))):
                for leaf_trail, leaf in list(_scalars(match, trail)):
                    if leaf_trail in seen:
                        continue
                    seen.add(leaf_trail)
                    new = self.pseudonym(leaf, checked)
                    if new != leaf or type(new) is not type(leaf):
                        result = _replace(result, leaf_trail, new)
                        changed.append(format_path(leaf_trail))
        return result, changed

    def pseudonymize_text(self, text: str) -> str:
        """Replace every real value seen so far inside ``text`` with its pseudonym.

        For URL paths (``/users/johndoe``), query strings (raw or percent-encoded, e.g.
        ``email=jane%40example.com``) and text bodies. Values only match as whole words (never
        inside a longer run of letters and digits), the longest known value wins, and values
        shorter than :data:`MIN_TEXT_LENGTH` are skipped. Matching is case-sensitive.
        """
        if not text:
            return text
        if self._index is None:
            self._index = _TextIndex(self._mapping)
        index = self._index
        if index.pattern is None:
            return text
        return index.pattern.sub(lambda match: index.table[match[0]], text)

    # -- plumbing ------------------------------------------------------------------------------

    def _digest(self, category: str, value: str) -> bytes:
        return hmac.new(self._key, f"{category}:{value}".encode(), hashlib.sha256).digest()

    def _hex(self, category: str, value: str) -> str:
        return self._digest(category, value).hex()

    def _stream(self, category: str, value: str) -> _Entropy:
        return _Entropy(self._digest(category, value))

    def _remember(self, real: str, fake: str) -> None:
        if real != fake and real not in self._mapping:
            self._mapping[real] = fake
            self._index = None

    # -- categories ----------------------------------------------------------------------------

    def _email(self, value: str) -> str:
        return f"user.{self._hex('email', value.strip().casefold())[:10]}@example.com"

    def _phone(self, value: str) -> str:
        digits = _digits_of(value)
        international = value.lstrip().startswith("+")
        keep = _country_code_length(digits) if international else 0
        stream = self._stream("phone", ("+" if international else "") + digits)
        return _fill_digits(value, digits[:keep] + "".join(stream.digit() for _ in digits[keep:]))

    def _name(self, word: str, role: PiiCategory) -> str:
        """Pseudonymize one word of a name (``role``: first or last name), keeping punctuation."""
        found = _WORD_CORE_RE.search(word)
        if found is None:
            return word  # punctuation only, e.g. "&"
        core = found[0]
        seed = self._digest(role, core.casefold())
        if role == "last_name":
            generated = _fake(seed, lambda faker: faker.last_name())
        else:
            generated = _fake(seed, lambda faker: faker.first_name())
        if len(core) == 1:  # an initial stays an initial
            generated = generated[0]
        new_core = _match_case(core, generated)
        if len(core) >= MIN_TEXT_LENGTH:
            self._remember(core, new_core)
        return word[: found.start()] + new_core + word[found.end() :]

    def _names(self, value: str, role: PiiCategory | None) -> str:
        """Pseudonymize each word as ``role``; ``None`` means a full name (given names + surname).

        The surname is the last word, or the first one when it ends with a comma (``Doe, Jane``).
        Titles and suffixes (``Dr.``, ``Jr.``) and the whitespace between words are kept.
        """
        pieces = _WHITESPACE_RE.split(value)
        words = [
            index
            for index in range(0, len(pieces), 2)
            if pieces[index] and pieces[index].strip(".,").casefold() not in _HONORIFICS
        ]
        surname = None
        if role is None and len(words) > 1:
            surname = words[0] if pieces[words[0]].endswith(",") else words[-1]
        for index in words:
            word_role: PiiCategory = role or ("last_name" if index == surname else "first_name")
            pieces[index] = self._name(pieces[index], word_role)
        return "".join(pieces)

    def _person_name(self, value: str) -> str:
        return self._names(value, None)

    def _first_name(self, value: str) -> str:
        return self._names(value, "first_name")

    def _last_name(self, value: str) -> str:
        return self._names(value, "last_name")

    def _username(self, value: str) -> str:
        text = f"user_{self._hex('username', value)[:8]}"
        return text.upper() if value.isupper() else text

    def _street(self, value: str) -> str:
        seed = self._digest("street", " ".join(value.split()).casefold())
        if value.lstrip()[:1].isdigit():
            generated = _fake(seed, lambda faker: faker.street_address())
        else:
            generated = _fake(seed, lambda faker: faker.street_name())
        return _match_case(value, generated)

    def _address(self, value: str) -> str:
        text = value.strip()
        seed = self._digest("address", " ".join(text.split()).casefold())
        if _SECONDARY_RE.match(text):
            generated = _fake(seed, lambda faker: faker.secondary_address())
        elif "\n" in text:
            generated = _fake(seed, lambda faker: faker.address())
            if "\r\n" in text:
                generated = generated.replace("\n", "\r\n")
        elif "," in text:
            generated = _fake(seed, lambda faker: faker.address()).replace("\n", ", ")
        elif text[:1].isdigit():
            generated = _fake(seed, lambda faker: faker.street_address())
        else:
            generated = _fake(seed, lambda faker: faker.street_name())
        return _match_case(value, generated)

    def _city(self, value: str) -> str:
        seed = self._digest("city", " ".join(value.split()).casefold())
        return _match_case(value, _fake(seed, lambda faker: faker.city()))

    def _postal_code(self, value: str) -> str:
        stream = self._stream("postal_code", value.strip().upper())
        shaped: list[str] = []
        for char in value:
            if char in _DIGITS:
                shaped.append(stream.digit())
            elif "A" <= char <= "Z":
                shaped.append(stream.letter())
            elif "a" <= char <= "z":
                shaped.append(stream.letter().lower())
            else:
                shaped.append(char)
        return "".join(shaped)

    def _ip(self, value: str) -> str:
        text = value.strip()
        address = parse_ip(text)
        if address is not None:
            return value.replace(text, self._ip_address(text, address))
        value = _IPV4_RE.sub(lambda match: self._ip_text(match[0]), value)
        return _IPV6_BRACKETED_RE.sub(lambda match: self._ip_text(match[0]), value)

    def _ip_text(self, text: str) -> str:
        address = parse_ip(text)
        return text if address is None else self._ip_address(text, address)

    def _ip_address(self, text: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        stream = self._stream("ip", str(address))
        if isinstance(address, ipaddress.IPv4Address):
            return f"10.{stream.below(_BYTE)}.{stream.below(_BYTE)}.{1 + stream.below(_IPV4_HOSTS)}"
        fake = ipaddress.IPv6Address(_ULA_PREFIX | stream.bits(_IPV6_RANDOM_BITS))
        bare = text.partition("%")[0]
        unscoped = ipaddress.IPv6Address(int(address))  # .exploded fails on scoped addresses
        rendered = fake.exploded if bare.lower() == unscoped.exploded else fake.compressed
        if bare.upper() == bare and bare.lower() != bare:
            rendered = rendered.upper()
        return rendered + (f"%{address.scope_id}" if address.scope_id else "")

    def _credit_card(self, value: str) -> str:
        digits = _digits_of(value)
        if len(digits) < _MIN_CARD_DIGITS:
            return value
        stream = self._stream("credit_card", digits)
        payload = digits[0] + "".join(stream.digit() for _ in digits[2:])
        return _fill_digits(value, payload + luhn_check_digit(payload))

    def _ssn(self, value: str) -> str:
        digits = _digits_of(value)
        stream = self._stream("ssn", digits)
        new = [stream.digit() for _ in digits]
        if len(new) == _SSN_DIGITS:
            new[0] = "9"  # area numbers 900-999 are never issued
        return _fill_digits(value, new)

    def _shifted_date(self, value: JsonValue) -> JsonValue | None:
        """``value`` moved by 1-365 days (derived from its calendar date), in its own spelling."""
        spelled = _date_format(value)
        if spelled is None:
            return None
        fmt, zone = spelled
        try:
            epoch_ns = fmt.parse(value, zone)
            day = datetime.fromtimestamp(epoch_ns // NS_PER_SECOND, zone).date().isoformat()
            span = 2 * MAX_DOB_SHIFT_DAYS
            days = self._stream("date_of_birth", day).below(span) - MAX_DOB_SHIFT_DAYS
            shift = (days + 1 if days >= 0 else days) * _NS_PER_DAY  # never zero
            return fmt.render(epoch_ns + shift, zone)
        except (ValueError, OverflowError, OSError):  # out-of-range years, impossible dates
            return None

    def _date_of_birth(self, value: str) -> str:
        shifted = self._shifted_date(value)
        if shifted is not None:
            return str(shifted)
        stream = self._stream("date_of_birth", value.strip())
        return _fill_digits(value, (stream.digit() for _ in _digits_of(value)))

    def _numeric_date_of_birth(self, value: JsonValue) -> JsonValue:
        shifted = self._shifted_date(value)
        return value if shifted is None else shifted

    def _free_text(self, value: str) -> str:
        return f"text-{self._hex('free_text', value)[:12]}"
