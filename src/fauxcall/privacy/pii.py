"""Find personally identifiable information (PII) in recorded JSON.

Two complementary signals are combined:

* **Value patterns** (:func:`classify_value`): emails, phone numbers, IP addresses, payment card
  numbers (Luhn-checked) and US social security numbers have recognizable shapes.
* **Field names in context**: ``firstName`` is a first name anywhere, but ``name`` is only a
  person's name inside a person-like object (one that also has an email, a phone, a username …),
  so pets, products and exchanges keep their names.

:func:`detect_pii` runs both over many documents and returns generalized paths
(``$.users[*].email``) mapped to a :data:`PiiCategory`. It deliberately prefers false negatives on
ambiguous generic fields; ``include``/``exclude`` let the caller (or the agent building the fake)
correct it.
"""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, get_args

from fauxcall.jsonpath import WILDCARD, format_path, parse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from fauxcall.jsonpath import Segment
    from fauxcall.types import JsonValue

PiiCategory = Literal[
    "email",
    "phone",
    "person_name",
    "first_name",
    "last_name",
    "username",
    "address",
    "street",
    "city",
    "postal_code",
    "ip",
    "credit_card",
    "ssn",
    "date_of_birth",
    "free_text",
]
"""Kinds of personal data FauxCall pseudonymizes (see :class:`~fauxcall.privacy.Pseudonymizer`)."""

PII_CATEGORIES: Final[tuple[PiiCategory, ...]] = get_args(PiiCategory)
"""Every :data:`PiiCategory`, in declaration order."""

VALUE_SHARE: Final = 0.8
"""Share of a path's non-empty string values that must agree before the path is flagged."""

_NO_OVERRIDES: Final[Mapping[str, PiiCategory]] = MappingProxyType({})

# ---------------------------------------------------------------------------
# Small helpers shared with the other privacy modules
# ---------------------------------------------------------------------------

_CAMEL_LOWER_UPPER: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM: Final = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_ALNUM: Final = re.compile(r"[^0-9A-Za-z]+")


_LUHN_FOLD: Final = 4  # doubling a digit above 4 overflows into two digits


@lru_cache(maxsize=4096)
def normalize_key(key: str) -> str:
    """Normalize a field or header name: ``firstName`` and ``First-Name`` become ``first_name``."""
    text = _CAMEL_ACRONYM.sub("_", _CAMEL_LOWER_UPPER.sub("_", key))
    return _NOT_ALNUM.sub("_", text).strip("_").lower()


def as_pii_category(name: str) -> PiiCategory:
    """Validate a category name (e.g. read from a model file) as a :data:`PiiCategory`.

    Raises:
        ValueError: ``name`` is not a known category.
    """
    if name not in PII_CATEGORIES:
        known = ", ".join(PII_CATEGORIES)
        raise ValueError(f"unknown PII category {name!r}; expected one of: {known}")
    return name


def luhn_valid(digits: str) -> bool:
    """Whether a string of ASCII digits passes the Luhn checksum used by payment cards."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > _LUHN_FOLD else value * 2
        total += value
    return total % 10 == 0


def luhn_check_digit(payload: str) -> str:
    """The digit to append to ``payload`` (ASCII digits) so the result passes the Luhn checksum."""
    total = 0
    for position, char in enumerate(reversed(payload)):
        value = int(char)
        if position % 2 == 0:
            value = value * 2 - 9 if value > _LUHN_FOLD else value * 2
        total += value
    return str(-total % 10)


# ---------------------------------------------------------------------------
# Value patterns
# ---------------------------------------------------------------------------

_EMAIL_RE: Final = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_SSN_RE: Final = re.compile(r"^(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}$")
_CARD_RE: Final = re.compile(
    r"^\d{13,19}$"
    r"|^\d{4}(?P<sep>[ -])\d{4}(?:(?P=sep)\d{4}){1,2}(?:(?P=sep)\d{1,3})?$"
    r"|^\d{4}(?P<amex>[ -])\d{6}(?P=amex)\d{4,5}$"
)
_CARD_FIRST_DIGITS: Final = frozenset("23456")
_CARD_DIGITS: Final = (13, 19)
_E164_RE: Final = re.compile(r"^\+[1-9]\d{8,14}$")
_PHONE_RE: Final = re.compile(
    r"^(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,5}\)[ .-]?)?\d{1,8}(?:[ .-]\d{1,8})*$"
)
_PHONE_DIGITS: Final = (9, 15)
_DIGIT_GROUP_RE: Final = re.compile(r"\d+")
_SEPARATED_DIGITS_RE: Final = re.compile(r"\d[ .-]\d")
_DECIMAL_RE: Final = re.compile(r"^[+-]?\d+\.\d+$")
_THOUSANDS_RE: Final = re.compile(r"^\d{1,3}(?P<sep>[ .])\d{3}(?:(?P=sep)\d{3})*$")
_NOT_PHONE_RE: Final = re.compile(r"^\d{3}(?P<sep>[ -])\d{2}(?P=sep)\d{4}$|^\d{5}-\d{4}$")
"""SSN-shaped (even when not a valid SSN) and US ZIP+4 values are too ambiguous to be phones."""
_MIN_LAST_GROUP: Final = 2


def _digit_count(value: str) -> int:
    return sum(char in "0123456789" for char in value)


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IPv4/IPv6 address (``None`` when ``value`` is not exactly one address)."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_credit_card(value: str) -> bool:
    if not _CARD_RE.match(value):
        return False
    digits = re.sub(r"\D", "", value)
    low, high = _CARD_DIGITS
    return low <= len(digits) <= high and digits[0] in _CARD_FIRST_DIGITS and luhn_valid(digits)


def _is_phone(value: str) -> bool:
    low, high = _PHONE_DIGITS
    if not low <= _digit_count(value) <= high:
        return False
    if _E164_RE.match(value):
        return True
    if not _PHONE_RE.match(value) or any(
        pattern.match(value) for pattern in (_DECIMAL_RE, _THOUSANDS_RE, _NOT_PHONE_RE)
    ):
        return False
    structured = value.startswith("+") or "(" in value or bool(_SEPARATED_DIGITS_RE.search(value))
    last_group = _DIGIT_GROUP_RE.findall(value)[-1]
    return structured and len(last_group) >= _MIN_LAST_GROUP


_SHAPES: Final[tuple[tuple[PiiCategory, Callable[[str], bool]], ...]] = (
    ("email", lambda text: _EMAIL_RE.match(text) is not None),
    ("ip", lambda text: parse_ip(text) is not None),
    ("ssn", lambda text: _SSN_RE.match(text) is not None),
    ("credit_card", _is_credit_card),
    ("phone", _is_phone),
)
"""Value shapes in the order they are tried (an IP address can also look like a phone number)."""


def classify_value(value: str) -> PiiCategory | None:
    """Classify a single string by its shape alone.

    Recognized: ``email``; ``ip`` (any valid IPv4/IPv6 address); ``ssn`` (``###-##-####`` with
    an area, group and serial that can be issued: no ``000``/``666``/``9XX`` area, ``00`` group
    or ``0000`` serial); ``credit_card`` (13-19 digits, optionally in groups of four or the 4-6-5
    Amex layout, starting with 2-6 and passing Luhn); ``phone`` (E.164 such as ``+14155552671``,
    or 9-15 digits written with a ``+`` country code, an area code in parentheses or separators).
    Bare digit strings, ISO dates and times, decimals, thousands-grouped numbers, SSN-shaped and
    ZIP+4 values are *not* phones.

    Returns:
        The category, or ``None`` when the value has no recognizable PII shape.
    """
    text = value.strip()
    return next((category for category, matches in _SHAPES if matches(text)), None)


# ---------------------------------------------------------------------------
# Field-name heuristics (all names are compared after normalize_key)
# ---------------------------------------------------------------------------


def _assign(category: PiiCategory, *names: str) -> dict[str, PiiCategory]:
    return dict.fromkeys(names, category)


_EMAIL_KEYS: Final = ("email", "e_mail", "emails", "email_address", "email_addresses", "mail")
_PHONE_KEYS: Final = (
    "phone",
    "phones",
    "phone_number",
    "phone_numbers",
    "phonenumber",
    "mobile",
    "mobile_phone",
    "mobile_number",
    "tel",
    "telephone",
    "telephone_number",
    "cell",
    "cellphone",
    "cell_phone",
    "fax",
    "fax_number",
    "msisdn",
)
_FIRST_NAME_KEYS: Final = ("first_name", "firstname", "given_name", "givenname", "forename")
_LAST_NAME_KEYS: Final = ("last_name", "lastname", "surname", "family_name", "familyname")
_FULL_NAME_KEYS: Final = ("full_name", "fullname", "real_name", "legal_name", "person_name")
_USERNAME_KEYS: Final = (
    "username",
    "user_name",
    "login",
    "user_login",
    "login_name",
    "screen_name",
    "screenname",
    "handle",
    "user_handle",
)
_DOB_KEYS: Final = (
    "dob",
    "date_of_birth",
    "dateofbirth",
    "birthdate",
    "birth_date",
    "birthday",
    "birth_day",
)
_IP_KEYS: Final = (
    "ip",
    "ips",
    "ip_address",
    "ipaddress",
    "ip_addr",
    "remote_addr",
    "remote_ip",
    "client_ip",
)
_SSN_KEYS: Final = ("ssn", "social_security_number", "social_security")
_CARD_KEYS: Final = (
    "card_number",
    "cardnumber",
    "cc_number",
    "ccnumber",
    "credit_card",
    "creditcard",
    "credit_card_number",
    "pan",
    "card_no",
    "primary_account_number",
)
_STREET_KEYS: Final = (
    "street",
    "street_address",
    "street_name",
    "street1",
    "street_1",
    "address1",
    "address_1",
    "address_line1",
    "address_line_1",
    "line1",
    "line_1",
)
_SECONDARY_KEYS: Final = (
    "street2",
    "street_2",
    "address2",
    "address_2",
    "address_line2",
    "address_line_2",
    "line2",
    "line_2",
    "suite",
    "apartment",
    "apt",
)
_CITY_KEYS: Final = ("city", "town", "city_name", "locality")
_POSTAL_KEYS: Final = (
    "zip",
    "zipcode",
    "zip_code",
    "postcode",
    "post_code",
    "postal_code",
    "postalcode",
    "postal",
)
_FULL_ADDRESS_KEYS: Final = (
    "address",
    "full_address",
    "formatted_address",
    "postal_address",
    "mailing_address",
    "home_address",
    "billing_address",
    "shipping_address",
    "residential_address",
    "delivery_address",
)
_REGION_KEYS: Final = (
    "state",
    "state_code",
    "province",
    "region",
    "county",
    "country",
    "country_code",
)

_DIRECT: Final[dict[str, PiiCategory]] = {
    **_assign("email", *_EMAIL_KEYS),
    **_assign("phone", *_PHONE_KEYS),
    **_assign("first_name", *_FIRST_NAME_KEYS),
    **_assign("last_name", *_LAST_NAME_KEYS),
    **_assign("person_name", *_FULL_NAME_KEYS),
    **_assign("date_of_birth", *_DOB_KEYS),
    **_assign("ip", *_IP_KEYS),
    **_assign("ssn", *_SSN_KEYS),
    **_assign("credit_card", *_CARD_KEYS),
}
"""Names that identify their category on their own."""

_DIRECT_SUFFIXES: Final[tuple[tuple[str, PiiCategory], ...]] = (
    ("_email", "email"),
    ("_emails", "email"),
    ("_email_address", "email"),
    ("_phone", "phone"),
    ("_phone_number", "phone"),
    ("_mobile", "phone"),
    ("_telephone", "phone"),
    ("_ip", "ip"),
    ("_ip_address", "ip"),
)

_NAMED_PEOPLE: Final = frozenset(
    {
        "customer",
        "contact",
        "author",
        "sender",
        "recipient",
        "employee",
        "patient",
        "student",
        "member",
        "person",
        "cardholder",
        "card_holder",
        "account_holder",
        "holder",
        "passenger",
        "guest",
        "applicant",
        "candidate",
        "driver",
        "traveler",
        "attendee",
        "organizer",
        "assignee",
        "reporter",
    }
)
"""``<word>_name`` is a person's name (``customer_name``, ``author_name`` …)."""

_CONTEXT_PERSON: Final[dict[str, PiiCategory]] = {
    **_assign("person_name", "name", "display_name", "displayname"),
    **_assign("username", *_USERNAME_KEYS),
}
"""Generic names that are PII only inside a person-like object."""

_LOCATION: Final[dict[str, PiiCategory]] = {
    **_assign("street", *_STREET_KEYS),
    **_assign("address", *_SECONDARY_KEYS, *_FULL_ADDRESS_KEYS),
    **_assign("city", *_CITY_KEYS),
    **_assign("postal_code", *_POSTAL_KEYS),
}
"""Address parts: PII only inside a person-like or address-like object."""

_PERSON_CONTAINERS: Final = frozenset(
    {
        "user",
        "users",
        "person",
        "people",
        "persons",
        "customer",
        "customers",
        "contact",
        "contacts",
        "author",
        "authors",
        "employee",
        "employees",
        "member",
        "members",
        "patient",
        "patients",
        "student",
        "students",
        "attendee",
        "attendees",
        "participant",
        "participants",
        "sender",
        "recipient",
        "recipients",
        "assignee",
        "assignees",
        "reporter",
        "organizer",
        "passenger",
        "passengers",
        "guest",
        "guests",
        "profile",
        "cardholder",
        "account_holder",
        "driver",
        "drivers",
        "traveler",
        "travelers",
        "applicant",
        "candidate",
        "friend",
        "friends",
        "follower",
        "followers",
    }
)
"""Container keys whose objects describe people (``users[*]``, ``author`` …)."""

_NON_PERSON_CONTAINERS: Final = frozenset(
    {
        "company",
        "companies",
        "organization",
        "organizations",
        "organisation",
        "organisations",
        "org",
        "orgs",
        "business",
        "businesses",
        "merchant",
        "merchants",
        "vendor",
        "vendors",
        "supplier",
        "suppliers",
        "store",
        "stores",
        "shop",
        "shops",
        "brand",
        "brands",
        "team",
        "teams",
        "group",
        "groups",
        "exchange",
        "exchanges",
        "product",
        "products",
        "item",
        "items",
        "pet",
        "pets",
        "category",
        "categories",
        "tag",
        "tags",
        "venue",
        "venues",
        "project",
        "projects",
        "repository",
        "repositories",
        "repo",
        "repos",
        "app",
        "apps",
        "application",
        "applications",
        "channel",
        "channels",
        "bank",
        "banks",
    }
)
"""Container keys whose objects are *not* people, even when they carry an email or phone."""

_PERSON_INDICATORS: Final = frozenset(
    {
        *_EMAIL_KEYS,
        *_PHONE_KEYS,
        *_FIRST_NAME_KEYS,
        *_LAST_NAME_KEYS,
        *_USERNAME_KEYS,
        *_DOB_KEYS,
        *_SSN_KEYS,
        "middle_name",
        "password",
        "passwd",
        "password_hash",
        "hashed_password",
        "gender",
    }
)
"""Sibling keys that make an object person-like (not ``full_name``: GitHub repos have one)."""

_ADDRESS_CONTAINERS: Final = frozenset({*_FULL_ADDRESS_KEYS, "addresses", "residence"})
_STREET_LEVEL: Final = frozenset(
    {*_STREET_KEYS, *_SECONDARY_KEYS, *_POSTAL_KEYS, *_FULL_ADDRESS_KEYS}
)
_ADDRESS_COMPONENTS: Final = frozenset({*_STREET_LEVEL, *_CITY_KEYS, *_REGION_KEYS})
_MIN_ADDRESS_COMPONENTS: Final = 2
_NAME_CONTAINERS: Final = frozenset({"name", "names", *_FULL_NAME_KEYS})

_ID_LIKE_RE: Final = re.compile(
    r"(?:^|_)(?:ids?|uuid|guid|key|code|sku|isbn|ean|upc|gtin|slug|hash|checksum|version|"
    r"number|no|ref|reference|nonce|serial|token)$"
)
"""Identifier-like names: digit-shaped values there are ids, not phones/cards/SSNs."""

_DIGIT_CATEGORIES: Final = frozenset({"phone", "credit_card", "ssn"})

# ---------------------------------------------------------------------------
# Plausibility: does a value fit the category its field name suggests?
# ---------------------------------------------------------------------------

_PERSON_NAME_RE: Final = re.compile(r"[^\W\d_]+(?:[ '\u2019.,-]+[^\W\d_]+)*\.?")
_PLACE_NAME_RE: Final = re.compile(r"[^\W\d_]+(?:[ '\u2019.,()-]+[^\W\d_]+)*\.?\)?")
_POSTAL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{1,10}")
_SSN_LIKE_RE: Final = re.compile(r"[\dXx*#]{3}[- ]?[\dXx*#]{2}[- ]?\d{4}")
_CARD_LIKE_RE: Final = re.compile(r"[\d -]+")
_IPV4_ANYWHERE_RE: Final = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAX_NAME_LENGTH: Final = 100
_MAX_USERNAME_LENGTH: Final = 64
_MAX_ADDRESS_LENGTH: Final = 200
_MIN_PHONE_DIGITS_BY_NAME: Final = 3
_MAX_PHONE_LETTERS: Final = 5
_CARD_DIGITS_BY_NAME: Final = (12, 19)


def _plausible_phone(value: str) -> bool:
    letters = sum(char.isalpha() for char in value)
    return _digit_count(value) >= _MIN_PHONE_DIGITS_BY_NAME and letters <= _MAX_PHONE_LETTERS


def _plausible_person_name(value: str) -> bool:
    text = value.strip()
    return len(text) <= _MAX_NAME_LENGTH and _PERSON_NAME_RE.fullmatch(text) is not None


def _plausible_username(value: str) -> bool:
    return len(value) <= _MAX_USERNAME_LENGTH and not any(char.isspace() for char in value)


def _plausible_address(value: str) -> bool:
    return len(value) <= _MAX_ADDRESS_LENGTH and any(char.isalpha() for char in value)


def _plausible_city(value: str) -> bool:
    text = value.strip()
    return len(text) <= _MAX_NAME_LENGTH and _PLACE_NAME_RE.fullmatch(text) is not None


def _plausible_postal_code(value: str) -> bool:
    return _POSTAL_RE.fullmatch(value.strip()) is not None and _digit_count(value) > 0


def _plausible_ip(value: str) -> bool:
    return parse_ip(value.strip()) is not None or _IPV4_ANYWHERE_RE.search(value) is not None


def _plausible_card(value: str) -> bool:
    low, high = _CARD_DIGITS_BY_NAME
    text = value.strip()
    return _CARD_LIKE_RE.fullmatch(text) is not None and low <= _digit_count(text) <= high


def _plausible_ssn(value: str) -> bool:
    return _SSN_LIKE_RE.fullmatch(value.strip()) is not None


_PLAUSIBLE: Final[dict[PiiCategory, Callable[[str], bool]]] = {
    "email": lambda value: "@" in value,
    "phone": _plausible_phone,
    "person_name": _plausible_person_name,
    "first_name": _plausible_person_name,
    "last_name": _plausible_person_name,
    "username": _plausible_username,
    "address": _plausible_address,
    "street": _plausible_address,
    "city": _plausible_city,
    "postal_code": _plausible_postal_code,
    "ip": _plausible_ip,
    "credit_card": _plausible_card,
    "ssn": _plausible_ssn,
    "date_of_birth": lambda value: _digit_count(value) > 0,
    "free_text": lambda _value: True,
}

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Field:
    """Everything observed about one generalized path."""

    name: str | None
    owner: str | None
    siblings: set[str] = field(default_factory=set)
    values: list[str] = field(default_factory=list)

    @classmethod
    def at(cls, trail: tuple[Segment, ...]) -> _Field:
        """Derive the field name and its object's container key from the path itself."""
        keyed = [
            (index, segment) for index, segment in enumerate(trail) if isinstance(segment, str)
        ]
        if not keyed:
            return cls(name=None, owner=None)
        index, name = keyed[-1]
        owner = next((s for s in reversed(trail[:index]) if isinstance(s, str)), None)
        return cls(name=normalize_key(name), owner=normalize_key(owner) if owner else None)


def _collect(
    node: JsonValue,
    trail: tuple[Segment, ...],
    keys: frozenset[str],
    fields: dict[tuple[Segment, ...], _Field],
) -> None:
    if isinstance(node, dict):
        child_keys = frozenset(normalize_key(key) for key in node)
        for key, child in node.items():
            _collect(child, (*trail, key), child_keys, fields)
    elif isinstance(node, list):
        for child in node:
            _collect(child, (*trail, WILDCARD), keys, fields)
    else:
        info = fields.get(trail)
        if info is None:
            info = fields[trail] = _Field.at(trail)
        info.siblings.update(keys)
        if isinstance(node, str) and node.strip():
            info.values.append(node)


def _is_person_object(info: _Field) -> bool:
    if info.owner in _PERSON_CONTAINERS:
        return True
    if info.owner in _NON_PERSON_CONTAINERS:
        return False
    return any(
        key in _PERSON_INDICATORS or key.endswith(("_email", "_phone"))
        for key in info.siblings
        if key != info.name
    )


def _is_address_object(info: _Field) -> bool:
    if info.owner in _ADDRESS_CONTAINERS or info.owner in _STREET_KEYS:
        return True
    components = info.siblings & _ADDRESS_COMPONENTS
    return len(components) >= _MIN_ADDRESS_COMPONENTS and bool(components & _STREET_LEVEL)


def _nested_category(name: str, owner: str | None) -> PiiCategory | None:
    """Fields named by their container: ``name.first``, ``dob.date``, ``street.name``."""
    if owner is None:
        return None
    if owner in _NAME_CONTAINERS or owner.endswith("_name"):
        if name in {"first", "given"}:
            return "first_name"
        if name in {"last", "family"}:
            return "last_name"
    if owner in _DOB_KEYS and name in {"date", "value"}:
        return "date_of_birth"
    if owner in _STREET_KEYS and name == "name":
        return "street"
    return None


def _suffix_category(name: str) -> PiiCategory | None:
    """Fields named by a suffix: ``billing_email``, ``home_phone``, ``customer_name`` …."""
    for suffix, category in _DIRECT_SUFFIXES:
        if name.endswith(suffix):
            return category
    if name.endswith("_name") and name.removesuffix("_name") in _NAMED_PEOPLE:
        return "person_name"
    return None


def _name_category(info: _Field) -> PiiCategory | None:
    name = info.name
    if name is None:
        return None
    category = _DIRECT.get(name) or _nested_category(name, info.owner) or _suffix_category(name)
    if category is None and name in _CONTEXT_PERSON and _is_person_object(info):
        category = _CONTEXT_PERSON[name]
    in_place = name in _LOCATION and (_is_address_object(info) or _is_person_object(info))
    if category is None and in_place:
        category = _LOCATION[name]
    return category


def _value_category(values: Sequence[str]) -> PiiCategory | None:
    counts = Counter(category for value in values if (category := classify_value(value)))
    if not counts:
        return None
    category, count = counts.most_common(1)[0]
    return category if count / len(values) >= VALUE_SHARE else None


def _categorize(info: _Field) -> PiiCategory | None:
    values = info.values
    if not values:
        return None
    by_value = _value_category(values)
    if by_value == "email":
        return by_value  # emails are unambiguous, and must match the same email elsewhere
    by_name = _name_category(info)
    if by_name is not None:
        plausible = sum(_PLAUSIBLE[by_name](value) for value in values)
        if plausible / len(values) >= VALUE_SHARE:
            return by_name
    if by_value in _DIGIT_CATEGORIES and info.name and _ID_LIKE_RE.search(info.name):
        return None
    return by_value


def _canonical(path: str) -> str:
    return format_path(parse(path))


def detect_pii(
    documents: Sequence[JsonValue],
    *,
    include: Mapping[str, PiiCategory] = _NO_OVERRIDES,
    exclude: Iterable[str] = (),
) -> dict[str, PiiCategory]:
    """Find the paths of PII fields across many observations of the same kind of document.

    Only non-empty string leaves are considered (pseudonyms are only produced for strings); list
    indices are generalized, so ``$.users[*].email`` covers every user. A path is flagged when:

    1. at least :data:`VALUE_SHARE` of its values are emails (by pattern), or else
    2. its field name names a category and at least :data:`VALUE_SHARE` of its values are
       plausible for it. Names that are unambiguous on their own: ``email``/``*_email``,
       ``phone``/``mobile``/``tel``/``*_phone``, ``firstName``/``given_name``,
       ``lastName``/``surname``, ``fullName``/``real_name``/``customer_name``…,
       ``dob``/``date_of_birth``/``birthdate``/``birthday``, ``ip``/``ip_address``/
       ``remote_addr``/``*_ip``, ``ssn``, ``card_number``/``cc_number``, plus nested spellings
       such as ``name.first``, ``dob.date`` and ``street.name``. Generic names need **person
       context** — a sibling key such as ``email``, ``phone``, ``firstName``, ``username`` or
       ``password``, or a container like ``users[*]``/``author`` (never ``company``,
       ``products`` …): ``name``/``displayName`` → ``person_name``, ``username``/``login``/
       ``handle`` → ``username``. Address parts (``address``, ``street``, ``city``,
       ``zip``/``postcode``/``postal_code``, ``address2``/``suite``) need a person-like or
       address-like object (an ``address`` container, or siblings with at least two address
       components including a street-level one, so an exchange's ``city`` is left alone), or
       else
    3. at least :data:`VALUE_SHARE` of its values share a pattern from :func:`classify_value`
       (phone, card and SSN shapes are ignored in identifier-like fields such as ``id``,
       ``order_number`` or ``sku``).

    ``free_text`` is never detected automatically. Ambiguous generic fields are left alone
    (false negatives are preferred); correct the result with the overrides.

    Args:
        documents: Observations to analyze (e.g. every response body of one endpoint).
        include: Paths to flag regardless of the heuristics (they win over ``exclude``).
        exclude: Paths whose detections are dropped, together with everything below them.

    Returns:
        Canonical generalized paths (``$.users[*].email``) mapped to their category, in the
        order first seen, followed by included paths that were not detected.

    Raises:
        ValueError: An override path is not a valid JSON path or a category is unknown.
    """
    fields: dict[tuple[Segment, ...], _Field] = {}
    for document in documents:
        _collect(document, (), frozenset(), fields)
    excluded = [parse(path) for path in exclude]
    forced = {_canonical(path): as_pii_category(category) for path, category in include.items()}
    found: dict[str, PiiCategory] = {}
    for trail, info in fields.items():
        if any(trail[: len(prefix)] == prefix for prefix in excluded):
            continue
        category = _categorize(info)
        if category is not None:
            found[format_path(trail)] = category
    found.update(forced)
    return found
