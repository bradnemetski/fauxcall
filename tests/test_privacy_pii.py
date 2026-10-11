from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, get_args

import pytest

from fauxcall.privacy import PII_CATEGORIES, PiiCategory, as_pii_category, classify_value
from fauxcall.privacy import detect_pii as detect
from fauxcall.privacy.pii import luhn_check_digit, luhn_valid, normalize_key, parse_ip

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

JSONPLACEHOLDER_USERS: JsonValue = [
    {
        "id": 1,
        "name": "Leanne Graham",
        "username": "Bret",
        "email": "Sincere@april.biz",
        "address": {
            "street": "Kulas Light",
            "suite": "Apt. 556",
            "city": "Gwenborough",
            "zipcode": "92998-3874",
            "geo": {"lat": "-37.3159", "lng": "81.1496"},
        },
        "phone": "1-770-736-8031 x56442",
        "website": "hildegard.org",
        "company": {"name": "Romaguera-Crona", "catchPhrase": "Multi-layered", "bs": "harness"},
    },
    {
        "id": 2,
        "name": "Ervin Howell",
        "username": "Antonette",
        "email": "Shanna@melissa.tv",
        "address": {
            "street": "Victor Plains",
            "suite": "Suite 879",
            "city": "Wisokyburgh",
            "zipcode": "90566-7771",
            "geo": {"lat": "-43.9509", "lng": "-34.4618"},
        },
        "phone": "010-692-6593 x09125",
        "website": "anastasia.net",
        "company": {"name": "Deckow-Crist", "catchPhrase": "Proactive", "bs": "synergize"},
    },
]

RANDOMUSER: JsonValue = {
    "results": [
        {
            "gender": "male",
            "name": {"title": "Mr", "first": "Brad", "last": "Gibson"},
            "location": {
                "street": {"number": 9278, "name": "New Road"},
                "city": "Kilcoole",
                "state": "Waterford",
                "country": "Ireland",
                "postcode": "93027",
            },
            "email": "brad.gibson@example.com",
            "login": {"uuid": "155e77ee", "username": "silverswan131", "password": "firewall"},
            "dob": {"date": "1993-07-20T09:44:18.674Z", "age": 26},
            "registered": {"date": "2002-05-21T10:59:49.966Z", "age": 17},
            "phone": "011-962-7516",
            "cell": "081-454-0666",
            "id": {"name": "PPS", "value": "0390511T"},
            "nat": "IE",
        }
    ]
}


def test_categories() -> None:
    assert get_args(PiiCategory) == PII_CATEGORIES
    assert len(PII_CATEGORIES) == 15
    for category in PII_CATEGORIES:
        assert as_pii_category(category) == category
    with pytest.raises(ValueError, match="unknown PII category 'name'"):
        as_pii_category("name")


@pytest.mark.parametrize(
    ("key", "normalized"),
    [
        ("firstName", "first_name"),
        ("First-Name", "first_name"),
        ("IPAddress", "ip_address"),
        ("userID", "user_id"),
        ("e-mail", "e_mail"),
        ("ZIP", "zip"),
        ("addressLine1", "address_line1"),
        ("X-RateLimit-Remaining", "x_rate_limit_remaining"),
        ("__weird  key__", "weird_key"),
    ],
)
def test_normalize_key(key: str, normalized: str) -> None:
    assert normalize_key(key) == normalized


def test_luhn_helpers() -> None:
    assert luhn_valid("4111111111111111")
    assert luhn_valid("378282246310005")
    assert not luhn_valid("4111111111111112")
    assert luhn_check_digit("411111111111111") == "1"
    assert luhn_check_digit("37828224631000") == "5"
    for payload in ("4", "12", "5555555555554", "60110000000000"):
        assert luhn_valid(payload + luhn_check_digit(payload))


def test_parse_ip() -> None:
    assert parse_ip("203.0.113.7") == ipaddress.IPv4Address("203.0.113.7")
    assert parse_ip("2001:db8::1") == ipaddress.IPv6Address("2001:db8::1")
    assert parse_ip("01.2.3.4") is None
    assert parse_ip("12:34") is None


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("jane@example.com", "email"),
        ("  Jane.Doe+news@sub.example.co.uk ", "email"),
        ("+14155552671", "phone"),
        ("+1 (555) 123-4567", "phone"),
        ("(555) 123-4567", "phone"),
        ("555-123-4567", "phone"),
        ("555.123.4567", "phone"),
        ("+44 20 7946 0958", "phone"),
        ("020 7946 0958", "phone"),
        ("+33 1 23 45 67 89", "phone"),
        ("10.0.0.1", "ip"),
        ("2001:db8::1", "ip"),
        ("123-45-6789", "ssn"),
        ("4111 1111 1111 1111", "credit_card"),
        ("4111-1111-1111-1111", "credit_card"),
        ("4111111111111111", "credit_card"),
        ("3782 822463 10005", "credit_card"),
        ("", None),
        ("   ", None),
        ("hello world", None),
        ("jane@localhost", None),
        ("123456789", None),  # bare digits are ids, not phones
        ("1791681447000", None),  # epoch milliseconds
        ("2026-10-10", None),
        ("2026-10-10T21:17:26.123Z", None),
        ("21:17:26", None),
        ("1234567.89", None),
        ("+1234567.89", None),
        ("123 456 789", None),
        ("123.456.789", None),
        ("978-3-16-148410-0", None),  # ISBN
        ("666-12-3456", None),  # SSN-shaped but never issued, and not a phone either
        ("123 45 6789", None),
        ("000-12-3456", None),
        ("123-00-4567", None),
        ("12345-6789", None),  # ZIP+4
        ("4111 1111 1111 1112", None),  # fails Luhn
        ("1111 1111 1111 1117", None),  # passes Luhn, but no card starts with 1
        ("4111 1111-1111 1111", None),  # mixed separators
        ("1-770-736-8031 x56442", None),
        ("123-456", None),
        ("550e8400-e29b-41d4-a716-446655440000", None),
    ],
)
def test_classify_value(value: str, category: PiiCategory | None) -> None:
    assert classify_value(value) == category


# ---------------------------------------------------------------------------
# detect_pii: true positives
# ---------------------------------------------------------------------------


def test_detect_jsonplaceholder_users() -> None:
    assert detect([JSONPLACEHOLDER_USERS]) == {
        "$[*].name": "person_name",
        "$[*].username": "username",
        "$[*].email": "email",
        "$[*].address.street": "street",
        "$[*].address.suite": "address",
        "$[*].address.city": "city",
        "$[*].address.zipcode": "postal_code",
        "$[*].phone": "phone",
    }


def test_detect_randomuser_nested_spellings() -> None:
    assert detect([RANDOMUSER]) == {
        "$.results[*].name.first": "first_name",
        "$.results[*].name.last": "last_name",
        "$.results[*].location.street.name": "street",
        "$.results[*].location.city": "city",
        "$.results[*].location.postcode": "postal_code",
        "$.results[*].email": "email",
        "$.results[*].login.username": "username",
        "$.results[*].dob.date": "date_of_birth",
        "$.results[*].phone": "phone",
        "$.results[*].cell": "phone",
    }


def test_detect_petstore_user_and_unambiguous_names() -> None:
    user: JsonValue = {
        "id": 10,
        "username": "theUser",
        "firstName": "John",
        "lastName": "James",
        "email": "john@email.com",
        "password": "12345",
        "phone": "12345",
        "userStatus": 1,
    }
    assert detect([user]) == {
        "$.username": "username",
        "$.firstName": "first_name",
        "$.lastName": "last_name",
        "$.email": "email",
        "$.phone": "phone",
    }
    profile: JsonValue = {
        "full_name": "Jane Q. Doe",
        "customer_name": "Jane Doe",
        "billing_email": "jane@example.com",
        "home_phone": "+1 555 123 4567",
        "last_login_ip": "203.0.113.7",
        "ssn": "123 45 6789",  # not a valid SSN spelling, but the name says SSN
        "card_number": "4111 1111 1111 1111",
        "birthdate": "1990-01-15",
        "names": [{"givenName": "Jane", "familyName": "Doe", "displayName": "Jane Doe"}],
    }
    assert detect([profile]) == {
        "$.full_name": "person_name",
        "$.customer_name": "person_name",
        "$.billing_email": "email",
        "$.home_phone": "phone",
        "$.last_login_ip": "ip",
        "$.ssn": "ssn",
        "$.card_number": "credit_card",
        "$.birthdate": "date_of_birth",
        "$.names[*].givenName": "first_name",
        "$.names[*].familyName": "last_name",
        "$.names[*].displayName": "person_name",
    }


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"users": [{"id": 1, "name": "Alice"}]}, {"$.users[*].name": "person_name"}),
        ({"author": {"name": "Jane", "url": "https://x.test"}}, {"$.author.name": "person_name"}),
        ({"name": "Jane", "work_email": None}, {"$.name": "person_name"}),
        ({"handle": "jdoe", "phone_number": None}, {"$.handle": "username"}),
        ({"login": "octocat", "id": 1, "email": None}, {"$.login": "username"}),
        (
            {
                "shipping_address": {
                    "line1": "1 Main St",
                    "city": "Springfield",
                    "postal_code": "62704",
                }
            },
            {
                "$.shipping_address.line1": "street",
                "$.shipping_address.city": "city",
                "$.shipping_address.postal_code": "postal_code",
            },
        ),
        (
            {"store": "Acme", "street": "1 Main St", "city": "Springfield", "zip": "62704"},
            {"$.street": "street", "$.city": "city", "$.zip": "postal_code"},
        ),
        (
            {"contact": {"city": "Paris", "email": "j@x.test"}},
            {"$.contact.city": "city", "$.contact.email": "email"},
        ),
        ({"emails": ["a@b.co", "c@d.co"]}, {"$.emails[*]": "email"}),
        ({"notes": "123-45-6789"}, {"$.notes": "ssn"}),
        ({"from": "+14155552671"}, {"$.from": "phone"}),
        ({"peer": "2001:db8::1"}, {"$.peer": "ip"}),
        ({"payment": "4111 1111 1111 1111"}, {"$.payment": "credit_card"}),
        ({"id": "jane@example.com"}, {"$.id": "email"}),  # e.g. calendar ids are emails
        ({"username": "jane@example.com", "password": "x"}, {"$.username": "email"}),
        ("jane@example.com", {"$": "email"}),
        (["a@b.co", None, "", "c@d.co"], {"$[*]": "email"}),
        (["+14155552671", "+14155552672"], {"$[*]": "phone"}),
    ],
)
def test_detect_positives(document: JsonValue, expected: dict[str, PiiCategory]) -> None:
    assert detect([document]) == expected


# ---------------------------------------------------------------------------
# detect_pii: true negatives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        # pets, products and exchanges keep their names
        [
            {
                "id": 10,
                "name": "doggie",
                "category": {"id": 1, "name": "Dogs"},
                "photoUrls": ["https://img.test/1.png"],
                "tags": [{"id": 0, "name": "friendly"}],
                "status": "available",
            }
        ],
        {
            "products": [
                {"id": 1, "sku": "A-1", "name": "Widget", "handle": "widget", "price": 9.99}
            ]
        },
        [{"code": "NYSE", "name": "New York Stock Exchange", "city": "New York", "country": "US"}],
        {"company": {"name": "Acme Corp", "display_name": "Acme"}},
        {"name": "Hello-World", "full_name": "octocat/Hello-World", "owner": {"login": "o"}},
        # timestamps and identifiers
        {
            "created_at": "2026-10-10T21:17:26.379953-04:00",
            "date": "2026-10-10",
            "time": "21:17:26",
            "epoch": 1791681447,
            "epoch_ms": "1791681447000",
            "id": 123456789,
            "uuid": "550e8400-e29b-41d4-a716-446655440000",
        },
        # digit shapes in identifier-like fields
        {
            "id": "4111111111111111",
            "order_number": "+1 555 123 4567",
            "reference": "123-45-6789",
            "tracking_code": "555-123-4567",
        },
        # names that suggest PII, values that do not
        {"email": "weekly", "phone": "N/A", "ip": "unknown", "card_number": "**** 4242"},
        {"send_email": True, "is_mobile": False, "dob": None},
        # free text is never detected automatically
        {"bio": "Call me at +1 555 123 4567 or write to jane@example.com"},
        # OAuth metadata
        {"issuer": "https://as.test", "authorization_endpoint": "https://as.test/authorize"},
        # mixed values (no single category reaches the threshold)
        {"contact": ["a@b.co", "+14155552671"]},
        [{"value": "a@b.co"}, {"value": "x"}, {"value": "y"}],
        # address parts outside person- or address-like objects
        {"event": {"city": "Paris", "country": "FR"}, "zip": "75001"},
    ],
)
def test_detect_negatives(document: JsonValue) -> None:
    assert detect([document]) == {}


def _rows(key: str, *values: str) -> JsonValue:
    return [{key: value} for value in values]


def test_value_share_threshold() -> None:
    four_of_five = _rows("v", "a@b.co", "a@b.co", "a@b.co", "a@b.co", "nope")
    three_of_five = _rows("v", "a@b.co", "a@b.co", "a@b.co", "nope", "nope")
    assert detect([four_of_five]) == {"$[*].v": "email"}
    assert detect([three_of_five]) == {}
    names = _rows("first_name", "Jane", "Jane", "Jane", "Jane", "R2-D2")
    assert detect([names]) == {"$[*].first_name": "first_name"}
    robots = _rows("first_name", "R2-D2", "R2-D2", "Jane")
    assert detect([robots]) == {}


def test_observations_are_merged_across_documents() -> None:
    first: JsonValue = {"name": "Alice"}
    second: JsonValue = {"name": "Bob", "email": "bob@example.com"}
    assert detect([first]) == {}
    assert detect([first, second]) == {"$.name": "person_name", "$.email": "email"}


def test_overrides() -> None:
    found = detect(
        [JSONPLACEHOLDER_USERS],
        include={"[*].website": "free_text", "$[*].company.name": "free_text"},
        exclude=["$[*].address", "$[*].email"],
    )
    assert found == {
        "$[*].name": "person_name",
        "$[*].username": "username",
        "$[*].phone": "phone",
        "$[*].website": "free_text",
        "$[*].company.name": "free_text",
    }
    assert detect([{"a": 1}], include={"$.a": "ssn"}, exclude=["$"]) == {"$.a": "ssn"}
    with pytest.raises(ValueError, match="unknown PII category"):
        detect([], include={"$.a": "name"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="invalid JSON path"):
        detect([], exclude=["a b"])
