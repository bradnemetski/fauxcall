from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from fauxcall.jsonfmt import JsonFloat
from fauxcall.privacy import (
    PII_CATEGORIES,
    REDACTED,
    PiiCategory,
    Pseudonymizer,
    classify_value,
    detect_pii,
)
from fauxcall.privacy.pii import luhn_valid
from fauxcall.privacy.pseudonym import KEY_BYTES, MAX_DOB_SHIFT_DAYS, _trie_regex
from fauxcall.timefmt import infer_time_format

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

KEY = "test-key-not-a-secret"

SAMPLES: list[tuple[PiiCategory, str]] = [
    ("email", "jane.doe@example.org"),
    ("phone", "+1 (415) 555-2671"),
    ("person_name", "Jane Q. Doe"),
    ("first_name", "Jane"),
    ("last_name", "Doe"),
    ("username", "janedoe"),
    ("address", "742 Evergreen Terrace, Springfield, OR 97403"),
    ("street", "742 Evergreen Terrace"),
    ("city", "Springfield"),
    ("postal_code", "97403-1234"),
    ("ip", "203.0.113.7"),
    ("credit_card", "4111 1111 1111 1111"),
    ("ssn", "123-45-6789"),
    ("date_of_birth", "1990-01-15"),
    ("free_text", "Allergic to peanuts"),
]


@pytest.fixture
def pseudo() -> Pseudonymizer:
    return Pseudonymizer(KEY)


def _text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _shape(text: str) -> str:
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", text)))


def _hmac_hex(category: str, value: str, key: str = KEY) -> str:
    return hmac.new(key.encode(), f"{category}:{value}".encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Keys and determinism
# ---------------------------------------------------------------------------


def test_generate_key() -> None:
    first, second = Pseudonymizer.generate_key(), Pseudonymizer.generate_key()
    assert first != second
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    assert KEY_BYTES == 32
    assert Pseudonymizer(first).pseudonym("a@b.co", "email") != "a@b.co"


def test_keys_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Pseudonymizer("")
    with pytest.raises(ValueError, match="must not be empty"):
        Pseudonymizer(b"")


def test_str_and_bytes_keys_agree_and_repr_hides_the_key(pseudo: Pseudonymizer) -> None:
    from_bytes = Pseudonymizer(KEY.encode())
    assert from_bytes.pseudonym("Jane Doe", "person_name") == pseudo.pseudonym(
        "Jane Doe", "person_name"
    )
    assert KEY not in repr(pseudo)
    assert repr(pseudo) == "Pseudonymizer(key=<hidden>, known_values=3)"


def test_derivation_is_the_documented_keyed_hmac(pseudo: Pseudonymizer) -> None:
    assert pseudo.pseudonym("Jane@Example.org", "email") == (
        f"user.{_hmac_hex('email', 'jane@example.org')[:10]}@example.com"
    )
    assert pseudo.pseudonym("janedoe", "username") == f"user_{_hmac_hex('username', 'janedoe')[:8]}"
    assert pseudo.pseudonym("notes", "free_text") == f"text-{_hmac_hex('free_text', 'notes')[:12]}"


def test_same_key_same_pseudonyms_across_instances() -> None:
    first, second = Pseudonymizer(KEY), Pseudonymizer(KEY)
    for category, value in SAMPLES:
        assert first.pseudonym(value, category) == second.pseudonym(value, category), category
    assert first.mapping == second.mapping


def test_different_keys_give_different_pseudonyms() -> None:
    first, second = Pseudonymizer(KEY), Pseudonymizer("another-key")
    for category, value in SAMPLES:
        one, two = first.pseudonym(value, category), second.pseudonym(value, category)
        assert one != value, category
        assert one != two, category


def test_distinct_values_get_distinct_pseudonyms(pseudo: Pseudonymizer) -> None:
    emails = {_text(pseudo.pseudonym(f"user{n}@example.org", "email")) for n in range(1000)}
    assert len(emails) == 1000
    phones = {_text(pseudo.pseudonym(f"+1 415 555 {n:04d}", "phone")) for n in range(300)}
    assert len(phones) == 300


def test_unknown_categories_and_untouched_values(pseudo: Pseudonymizer) -> None:
    with pytest.raises(ValueError, match="unknown PII category"):
        pseudo.pseudonym("x", "name")  # type: ignore[arg-type]
    values: list[JsonValue] = [None, 5, 2.5, True, "", "   ", REDACTED, ["a@b.co"], {"e": "a"}]
    for value in values:
        for category in PII_CATEGORIES:
            assert pseudo.pseudonym(value, category) == value
    assert pseudo.mapping == {}


# ---------------------------------------------------------------------------
# Format preservation, per category
# ---------------------------------------------------------------------------


def test_email(pseudo: Pseudonymizer) -> None:
    fake = _text(pseudo.pseudonym("jane.doe+news@example.org", "email"))
    assert re.fullmatch(r"user\.[0-9a-f]{10}@example\.com", fake)
    assert classify_value(fake) == "email"
    assert pseudo.pseudonym(" JANE.DOE+NEWS@EXAMPLE.ORG ", "email") == fake  # case-insensitive


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("+1 (415) 555-2671", "+1"),
        ("+14155552671", "+1"),
        ("+44 20 7946 0958", "+44"),
        ("+353 1 234 5678", "+353"),
        ("+7 912 345-67-89", "+7"),
        ("+999 123 456 789", "+999"),
        ("(415) 555-2671", ""),
        ("1-770-736-8031 x56442", ""),
        ("12345", ""),
    ],
)
def test_phone_keeps_format_and_country_code(pseudo: Pseudonymizer, value: str, kept: str) -> None:
    fake = _text(pseudo.pseudonym(value, "phone"))
    assert fake != value
    assert len(fake) == len(value)
    assert re.sub(r"\d", "9", fake) == re.sub(r"\d", "9", value)
    assert fake.startswith(kept)


def test_phone_digits_do_not_depend_on_formatting(pseudo: Pseudonymizer) -> None:
    formatted = _text(pseudo.pseudonym("+1 (415) 555-2671", "phone"))
    compact = _text(pseudo.pseudonym("+14155552671", "phone"))
    assert re.sub(r"\D", "", formatted) == compact.removeprefix("+")


def test_person_names_are_consistent_with_their_parts(pseudo: Pseudonymizer) -> None:
    first = _text(pseudo.pseudonym("Jane", "first_name"))
    last = _text(pseudo.pseudonym("Doe", "last_name"))
    assert re.fullmatch(r"[A-Z][A-Za-z'-]+", first)
    assert re.fullmatch(r"[A-Z][A-Za-z'-]+", last)
    assert pseudo.pseudonym("Jane Doe", "person_name") == f"{first} {last}"
    assert pseudo.pseudonym("Doe, Jane", "person_name") == f"{last}, {first}"
    assert pseudo.pseudonym("Dr. Jane Doe, Jr.", "person_name") == f"Dr. {first} {last}, Jr."
    assert pseudo.pseudonym("JANE  DOE", "person_name") == f"{first.upper()}  {last.upper()}"
    assert pseudo.pseudonym("jane doe", "person_name") == f"{first.lower()} {last.lower()}"
    assert pseudo.pseudonym("Jane", "person_name") == first  # one word stays one word
    middle = _text(pseudo.pseudonym("Jane Q. Doe", "person_name"))
    assert re.fullmatch(rf"{first} [A-Z]\. {last}", middle)
    assert pseudo.pseudonym("Jane & John", "first_name") == (
        f"{first} & {pseudo.pseudonym('John', 'first_name')}"
    )
    assert pseudo.pseudonym("Dr.", "person_name") == "Dr."  # nothing to replace
    assert pseudo.pseudonym("Mary Ann", "first_name") == (
        f"{pseudo.pseudonym('Mary', 'first_name')} {pseudo.pseudonym('Ann', 'first_name')}"
    )


def test_username(pseudo: Pseudonymizer) -> None:
    lower = _text(pseudo.pseudonym("JaneDoe", "username"))
    assert re.fullmatch(r"user_[0-9a-f]{8}", lower)
    upper = _text(pseudo.pseudonym("JANEDOE", "username"))
    assert re.fullmatch(r"USER_[0-9A-F]{8}", upper)
    assert upper.lower() != lower  # usernames are case-sensitive


@pytest.mark.parametrize(
    ("category", "value", "pattern"),
    [
        ("street", "742 Evergreen Terrace", r"\d+ [A-Z][\w ]+"),
        ("street", "Kulas Light", r"[A-Z][a-z]+ [A-Z][a-z]+"),
        ("street", "KULAS LIGHT", r"[A-Z]+ [A-Z]+"),
        ("address", "Apt. 556", r"(Apt\.|Suite) \d+"),
        ("address", "suite 879", r"(apt\.|suite) \d+"),
        (
            "address",
            "742 Evergreen Terrace, Springfield, OR 97403",
            r"[^\n]+, [^\n]+, [A-Z]{2} \d{5}",
        ),
        ("address", "742 Evergreen Terrace\nSpringfield, OR 97403", r"[^\n]+\n[^\n]+"),
        ("address", "742 Evergreen Terrace\r\nSpringfield", r"[^\r\n]+\r\n[^\r\n]+"),
        ("address", "742 Evergreen Terrace", r"\d+ [A-Z][\w ]+"),
        ("address", "Evergreen Terrace", r"[A-Z][a-z]+ [A-Z][a-z]+"),
        ("city", "Springfield", r"[A-Z][A-Za-z ]+"),
        ("city", "springfield", r"[a-z ]+"),
    ],
)
def test_places_look_like_the_original(
    pseudo: Pseudonymizer, category: PiiCategory, value: str, pattern: str
) -> None:
    fake = _text(pseudo.pseudonym(value, category))
    assert fake != value
    assert re.fullmatch(pattern, fake), fake


@pytest.mark.parametrize("value", ["97403", "97403-1234", "SW1A 1AA", "m5v 3l9", "D02 X285"])
def test_postal_code_keeps_its_pattern(pseudo: Pseudonymizer, value: str) -> None:
    fake = _text(pseudo.pseudonym(value, "postal_code"))
    assert fake != value
    assert _shape(fake) == _shape(value)


def test_postal_code_letters_are_case_insensitive(pseudo: Pseudonymizer) -> None:
    assert (
        _text(pseudo.pseudonym("sw1a 1aa", "postal_code"))
        == _text(pseudo.pseudonym("SW1A 1AA", "postal_code")).lower()
    )


def test_ipv4(pseudo: Pseudonymizer) -> None:
    fake = _text(pseudo.pseudonym("203.0.113.7", "ip"))
    assert ipaddress.ip_address(fake) in ipaddress.ip_network("10.0.0.0/8")
    assert pseudo.pseudonym(" 203.0.113.7 ", "ip") == f" {fake} "
    assert pseudo.pseudonym("203.0.113.7:8080, 198.51.100.2", "ip") == (
        f"{fake}:8080, {pseudo.pseudonym('198.51.100.2', 'ip')}"
    )
    assert pseudo.pseudonym("unknown", "ip") == "unknown"
    assert pseudo.pseudonym("999.1.1.1 via proxy", "ip") == "999.1.1.1 via proxy"


def test_ipv6(pseudo: Pseudonymizer) -> None:
    fake = _text(pseudo.pseudonym("2001:db8::1", "ip"))
    assert ipaddress.ip_address(fake) in ipaddress.ip_network("fd00::/8")
    assert fake == ipaddress.ip_address(fake).compressed
    exploded = _text(pseudo.pseudonym("2001:0db8:0000:0000:0000:0000:0000:0001", "ip"))
    assert exploded == ipaddress.ip_address(fake).exploded  # same address, same spelling style
    assert pseudo.pseudonym("2001:DB8::1", "ip") == fake.upper()
    assert pseudo.pseudonym("[2001:db8::1]:443", "ip") == f"[{fake}]:443"
    scoped = _text(pseudo.pseudonym("fe80::1%eth0", "ip"))
    address, _, scope = scoped.partition("%")
    assert scope == "eth0"
    assert ipaddress.ip_address(address) in ipaddress.ip_network("fd00::/8")


@pytest.mark.parametrize(
    "value", ["4111 1111 1111 1111", "4111-1111-1111-1111", "378282246310005", "5555555555554444"]
)
def test_credit_card(pseudo: Pseudonymizer, value: str) -> None:
    fake = _text(pseudo.pseudonym(value, "credit_card"))
    digits = re.sub(r"\D", "", fake)
    assert fake != value
    assert re.sub(r"\d", "9", fake) == re.sub(r"\d", "9", value)
    assert fake[0] == value[0]
    assert luhn_valid(digits)


def test_short_or_masked_cards(pseudo: Pseudonymizer) -> None:
    assert pseudo.pseudonym("4", "credit_card") == "4"
    masked = _text(pseudo.pseudonym("**** **** **** 4242", "credit_card"))
    assert masked.startswith("**** **** **** 4")
    assert luhn_valid(masked[-4:])


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("123-45-6789", r"9\d\d-\d\d-\d{4}"),
        ("123 45 6789", r"9\d\d \d\d \d{4}"),
        ("123456789", r"9\d{8}"),
        ("***-**-1234", r"\*\*\*-\*\*-\d{4}"),
    ],
)
def test_ssn(pseudo: Pseudonymizer, value: str, pattern: str) -> None:
    fake = _text(pseudo.pseudonym(value, "ssn"))
    assert fake != value
    assert re.fullmatch(pattern, fake)
    assert classify_value(fake) is None  # never a valid, issued SSN


@pytest.mark.parametrize(
    "value",
    [
        "1990-01-15",
        "01/15/1990",
        "15.01.1990",
        "Jan 15, 1990",
        "1990-01-15T00:00:00Z",
        "1990-01-15T00:00:00.000+05:30",
        "Mon, 15 Jan 1990 00:00:00 GMT",
        "1955-03-04",
    ],
)
def test_date_of_birth_keeps_its_spelling(pseudo: Pseudonymizer, value: str) -> None:
    fake = _text(pseudo.pseudonym(value, "date_of_birth"))
    fmt = infer_time_format([value])
    assert fmt is not None
    assert infer_time_format([fake]) == fmt
    days = abs(fmt.parse(fake) - fmt.parse(value)) // (86_400 * 10**9)
    assert 1 <= days <= MAX_DOB_SHIFT_DAYS


def test_date_of_birth_shift_depends_only_on_the_date(pseudo: Pseudonymizer) -> None:
    iso = _text(pseudo.pseudonym("1990-01-15", "date_of_birth"))
    shifted = datetime.fromisoformat(iso).replace(tzinfo=UTC)
    original = datetime(1990, 1, 15, tzinfo=UTC)
    assert timedelta(days=1) <= abs(shifted - original) <= timedelta(days=MAX_DOB_SHIFT_DAYS)
    expected_us = shifted.strftime("%m/%d/%Y")
    assert pseudo.pseudonym("01/15/1990", "date_of_birth") == expected_us
    assert pseudo.pseudonym("19900115", "date_of_birth") == shifted.strftime("%Y%m%d")
    assert pseudo.pseudonym("1990-01-15T00:00:00Z", "date_of_birth") == (
        shifted.strftime("%Y-%m-%dT00:00:00Z")
    )


def test_date_of_birth_fallbacks(pseudo: Pseudonymizer) -> None:
    unpadded = _text(pseudo.pseudonym("1/5/1990", "date_of_birth"))
    assert re.sub(r"\d", "9", unpadded) == "9/9/9999"
    assert unpadded != "1/5/1990"
    impossible = _text(pseudo.pseudonym("99999999", "date_of_birth"))
    assert re.fullmatch(r"\d{8}", impossible)
    assert impossible != "99999999"
    assert pseudo.pseudonym("12:30", "date_of_birth") != "12:30"  # a time of day is not a date


def test_numeric_dates_of_birth(pseudo: Pseudonymizer) -> None:
    epoch = 631_152_000  # 1990-01-01T00:00:00Z
    fake = pseudo.pseudonym(epoch, "date_of_birth")
    assert isinstance(fake, int)
    assert 0 < abs(fake - epoch) <= MAX_DOB_SHIFT_DAYS * 86_400
    assert fake % 86_400 == 0
    precise = pseudo.pseudonym(JsonFloat("631152000.5"), "date_of_birth")
    assert isinstance(precise, JsonFloat)
    assert precise.lexeme.endswith(".5")
    assert pseudo.pseudonym(42, "date_of_birth") == 42  # not a plausible epoch
    flag: JsonValue = True
    assert pseudo.pseudonym(flag, "date_of_birth") is True
    assert pseudo.mapping == {}


def test_free_text(pseudo: Pseudonymizer) -> None:
    fake = _text(pseudo.pseudonym("Allergic to peanuts", "free_text"))
    assert re.fullmatch(r"text-[0-9a-f]{12}", fake)


# ---------------------------------------------------------------------------
# Mapping, JSON documents and free text
# ---------------------------------------------------------------------------


def test_mapping_is_a_copy_and_first_pseudonym_wins(pseudo: Pseudonymizer) -> None:
    email = pseudo.pseudonym("jane@example.org", "email")
    pseudo.pseudonym("jane@example.org", "username")
    pseudo.pseudonym("Jane Doe", "person_name")
    pseudo.pseudonym("+1", "phone")  # only a country code: nothing changes, nothing is recorded
    mapping = pseudo.mapping
    assert mapping["jane@example.org"] == email
    assert set(mapping) == {"jane@example.org", "Jane", "Doe", "Jane Doe"}
    mapping.clear()
    assert len(pseudo.mapping) == 4


def test_pseudonymize_json_nested_arrays(pseudo: Pseudonymizer) -> None:
    document: JsonValue = {
        "users": [
            {
                "email": "jane@example.org",
                "name": "Jane Doe",
                "addresses": [{"city": "Springfield"}, {"city": "Shelbyville"}],
                "tags": ["vip", "beta"],
                "dob": 631_152_000,
            },
            {"email": "john@example.org", "name": None, "addresses": [], "tags": []},
        ],
        "count": 2,
    }
    original = copy.deepcopy(document)
    result, changed = pseudo.pseudonymize_json(
        document,
        {
            "$.users[*].email": "email",
            "users[*].email": "email",  # the same field spelled differently: replaced once
            "$.users[*].name": "person_name",
            "$.users[*].addresses[*].city": "city",
            "$.users[*].tags": "free_text",  # a container: every scalar below it
            "$.users[*].dob": "date_of_birth",
            "$.missing": "ssn",
        },
    )
    assert document == original  # the input is not modified
    assert changed == [
        "$.users[0].email",
        "$.users[1].email",
        "$.users[0].name",
        "$.users[0].addresses[0].city",
        "$.users[0].addresses[1].city",
        "$.users[0].tags[0]",
        "$.users[0].tags[1]",
        "$.users[0].dob",
    ]
    assert isinstance(result, dict)
    users = result["users"]
    assert isinstance(users, list)
    first, second = users
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["email"] == pseudo.pseudonym("jane@example.org", "email")
    assert second["email"] == pseudo.pseudonym("john@example.org", "email")
    assert first["name"] == pseudo.pseudonym("Jane Doe", "person_name")
    assert second["name"] is None
    assert first["dob"] != 631_152_000
    assert result["count"] == 2


def test_pseudonymize_json_root_and_validation(pseudo: Pseudonymizer) -> None:
    assert pseudo.pseudonymize_json("jane@example.org", {"$": "email"}) == (
        pseudo.pseudonym("jane@example.org", "email"),
        ["$"],
    )
    assert pseudo.pseudonymize_json(["a@b.co", 1], {"$": "email"}) == (
        [pseudo.pseudonym("a@b.co", "email"), 1],
        ["$[0]"],
    )
    contact: JsonValue = {"contact": {"work": "a@b.co", "home": ["c@d.co", None]}, "id": 1}
    assert pseudo.pseudonymize_json(contact, {"$.contact": "email"}) == (
        {
            "contact": {
                "work": pseudo.pseudonym("a@b.co", "email"),
                "home": [pseudo.pseudonym("c@d.co", "email"), None],
            },
            "id": 1,
        },
        ["$.contact.work", "$.contact.home[0]"],
    )
    with pytest.raises(ValueError, match="unknown PII category"):
        pseudo.pseudonymize_json({"a": "b"}, {"$.a": "text"})  # type: ignore[dict-item]


def test_references_survive_across_endpoints() -> None:
    users: JsonValue = [{"id": 1, "username": "jdoe", "email": "Jane@Example.org", "name": "Jane"}]
    orders: JsonValue = {"orders": [{"id": 9, "owner_email": "jane@example.org", "total": 5}]}
    recorder = Pseudonymizer(KEY)
    fake_users, _ = recorder.pseudonymize_json(users, detect_pii([users]))
    fake_orders, _ = recorder.pseudonymize_json(orders, detect_pii([orders]))
    assert isinstance(fake_users, list)
    assert isinstance(fake_users[0], dict)
    assert isinstance(fake_orders, dict)
    orders_list = fake_orders["orders"]
    assert isinstance(orders_list, list)
    assert isinstance(orders_list[0], dict)
    assert orders_list[0]["owner_email"] == fake_users[0]["email"]
    # A parity test with the same key translates real values into what the fake knows.
    parity = Pseudonymizer(KEY)
    assert parity.translate("jane@example.org", "email") == fake_users[0]["email"]
    assert parity.translate("jdoe", "username") == fake_users[0]["username"]
    assert parity.translate("Jane", "person_name") == fake_users[0]["name"]


def test_pseudonymize_text(pseudo: Pseudonymizer) -> None:
    assert pseudo.pseudonymize_text("/users/johndoe") == "/users/johndoe"  # nothing known yet
    username = _text(pseudo.pseudonym("johndoe", "username"))
    email = _text(pseudo.pseudonym("jane.doe+x@example.org", "email"))
    name = _text(pseudo.pseudonym("Jane Q. Doe", "person_name"))
    first = _text(pseudo.pseudonym("Jane", "first_name"))
    pseudo.pseudonym("J", "first_name")  # too short to replace inside text
    assert pseudo.pseudonymize_text("/users/johndoe/repos") == f"/users/{username}/repos"
    assert pseudo.pseudonymize_text("/users/johndoe2") == "/users/johndoe2"  # whole words only
    assert pseudo.pseudonymize_text("?email=jane.doe%2Bx%40example.org") == (
        f"?email={email.replace('@', '%40')}"
    )
    assert pseudo.pseudonymize_text("name=Jane+Q.+Doe&by=Jane") == (
        f"name={name.replace(' ', '+')}&by={first}"
    )
    assert pseudo.pseudonymize_text("Jane Q. Doe met Janet, J and Jane_") == (
        f"{name} met Janet, J and {first}_"
    )
    assert pseudo.pseudonymize_text("") == ""


def test_trie_regex_prefers_the_longest_value() -> None:
    pattern = re.compile(_trie_regex(["ab", "abc", "b", "a.c", "(x)"]))
    assert pattern.pattern == r"(?:a(?:b(?:c)?|\.c)|b|\(x\))"
    assert [match[0] for match in pattern.finditer("abc ab a.c b (x) axc")] == [
        "abc",
        "ab",
        "a.c",
        "b",
        "(x)",
    ]
