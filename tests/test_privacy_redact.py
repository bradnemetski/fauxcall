from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall import logs
from fauxcall.jsonfmt import JsonFloat
from fauxcall.privacy import (
    MIN_SECRET_LENGTH,
    REDACTED,
    is_public_name,
    is_secret_name,
    redact_headers,
    redact_json,
    redact_query,
    redact_url,
    scrub_text,
)
from fauxcall.privacy.redact import REDACTED_UUID

if TYPE_CHECKING:
    from fauxcall.types import JsonValue

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"


def test_marker_is_the_log_pipeline_marker() -> None:
    assert REDACTED == logs.REDACTED == "[REDACTED]"
    assert MIN_SECRET_LENGTH == 4


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "Proxy-Authorization",
        "X-API-Key",
        "access_token",
        "refreshToken",
        "password",
        "Cookie",
        "Set-Cookie",
        "session_id",
        "client_secret",
        "sig",
        "X-Amz-Signature",
        "oauth_signature",
        "jwt",
        "access_key",
        "Ocp-Apim-Subscription-Key",
        "X-Auth",
        "auth",
        "pwd",
        "pass",
        "passphrase",
    ],
)
def test_secret_names(name: str) -> None:
    assert is_secret_name(name)
    assert not is_public_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "token_type",
        "tokenType",
        "expires_in",
        "scope",
        "password_policy",
        "authorization_endpoint",
        "token_endpoint_auth_methods_supported",
        "token_endpoint_auth_method",
        "authorization_servers",
        "client_secret_expires_at",
        "nextPageToken",
        "page_token",
        "continuationToken",
        "NextToken",
        "next_sync_token",
        "progressToken",  # MCP: identifies a request's progress notifications
        "X-RateLimit-Remaining-Tokens",
        "x-ratelimit-limit-tokens",
        "Access-Control-Allow-Credentials",
    ],
)
def test_public_names_describe_credentials_without_holding_them(name: str) -> None:
    assert is_public_name(name)
    assert not is_secret_name(name)


@pytest.mark.parametrize("name", ["key", "name", "bypass", "author", "id", "Accept"])
def test_ordinary_names(name: str) -> None:
    assert not is_secret_name(name)
    assert not is_public_name(name)


def test_public_name_rules_are_suffix_based() -> None:
    assert is_public_name("Content-Type")  # harmless: it is not secret either
    assert not is_secret_name("Content-Type")


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_redact_headers_keeps_the_shape_of_credentials() -> None:
    headers = [
        ("Authorization", "Bearer abc"),
        ("authorization", "Basic xyz=="),
        ("Proxy-Authorization", 'Digest username="u", response="r"'),
        ("Cookie", "a=1; b=2"),
        ("Set-Cookie", "sid=abc; Path=/; HttpOnly"),
        ("Set-Cookie", "a=1; Expires=Wed, 21 Oct 2015 07:28:00 GMT, b=2; Path=/"),
        ("X-API-Key", "k3y-123"),
        ("X-Auth", "Token abc"),
        ("Content-Type", "application/json"),
    ]
    original = copy.deepcopy(headers)
    redacted, names = redact_headers(headers)
    assert redacted == [
        ("Authorization", "Bearer [REDACTED]"),
        ("authorization", "Basic [REDACTED]"),
        ("Proxy-Authorization", "Digest [REDACTED]"),
        ("Cookie", "a=[REDACTED]; b=[REDACTED]"),
        ("Set-Cookie", "sid=[REDACTED]; Path=/; HttpOnly"),
        ("Set-Cookie", "a=[REDACTED]; Expires=Wed, 21 Oct 2015 07:28:00 GMT, b=[REDACTED]; Path=/"),
        ("X-API-Key", "[REDACTED]"),
        ("X-Auth", "Token [REDACTED]"),
        ("Content-Type", "application/json"),
    ]
    assert names == [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "X-API-Key",
        "X-Auth",
    ]
    assert headers == original  # the input is not modified


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GenieKey abc123", "GenieKey [REDACTED]"),  # a custom scheme in Authorization
        ("Klaviyo-API-Key pk_123", "Klaviyo-API-Key [REDACTED]"),
        ("AWS4-HMAC-SHA256 Credential=AKIA/x, Signature=abc", "AWS4-HMAC-SHA256 [REDACTED]"),
        ("a1b2c3 d4e5", "[REDACTED]"),  # a "scheme" with digits is more likely the secret itself
        ("sk_live_abcdef", "[REDACTED]"),
        ("Bearer", "Bearer"),  # a bare scheme carries no secret
        ("", ""),
        ("  ", "  "),
        ("[REDACTED]", "[REDACTED]"),
        ("Bearer [REDACTED]", "Bearer [REDACTED]"),
    ],
)
def test_authorization_values(value: str, expected: str) -> None:
    redacted, names = redact_headers([("Authorization", value)])
    assert redacted == [("Authorization", expected)]
    assert names == (["Authorization"] if expected != value else [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bearer abc", "Bearer [REDACTED]"),  # known schemes are kept in any secret header
        ("GenieKey abc", "[REDACTED]"),  # custom schemes only in Authorization
        ("abc def", "[REDACTED]"),
    ],
)
def test_other_secret_headers(value: str, expected: str) -> None:
    assert redact_headers([("X-Access-Token", value)])[0] == [("X-Access-Token", expected)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a=1; b=2", "a=[REDACTED]; b=[REDACTED]"),
        ("a=; b=[REDACTED]; c", "a=; b=[REDACTED]; [REDACTED]"),
        ("a=1;", "a=[REDACTED];"),
        ("sessionvalue", "[REDACTED]"),
    ],
)
def test_cookie_values(value: str, expected: str) -> None:
    assert redact_headers([("Cookie", value)])[0] == [("Cookie", expected)]


def test_set_cookie_without_value_or_attributes() -> None:
    redacted, names = redact_headers([("Set-Cookie", "sid=; Max-Age=0"), ("set-cookie2", "x=1")])
    assert redacted == [("Set-Cookie", "sid=; Max-Age=0"), ("set-cookie2", "x=[REDACTED]")]
    assert names == ["set-cookie2"]


def test_public_headers_and_embedded_credentials() -> None:
    redacted, names = redact_headers(
        [
            ("Access-Control-Allow-Credentials", "true"),
            ("X-RateLimit-Remaining-Tokens", "1000"),
            ("Location", "https://app.test/cb?code=1&access_token=zzz&state=s"),
            ("Link", '<https://api.test/items?page=2&api_key=abc>; rel="next"'),
            ("X-Debug", f"forwarded {JWT}"),
            ("Content-Type", "text/plain;charset=utf-8"),
        ]
    )
    assert redacted == [
        ("Access-Control-Allow-Credentials", "true"),
        ("X-RateLimit-Remaining-Tokens", "1000"),
        ("Location", "https://app.test/cb?code=1&access_token=[REDACTED]&state=s"),
        ("Link", '<https://api.test/items?page=2&api_key=[REDACTED]>; rel="next"'),
        ("X-Debug", "forwarded [REDACTED]"),
        ("Content-Type", "text/plain;charset=utf-8"),
    ]
    assert names == ["Location", "Link", "X-Debug"]


def test_header_secret_values_extra_and_allowed_names() -> None:
    headers = [
        ("X-Echo", "you sent sk_live_123 to us"),
        ("X-Custom-Credential-Header", "abc"),
        ("X-Tenant", "acme"),
        ("X-Session-Id", "s-1"),
        ("X-Both", "value"),
        ("X-sk_live_123", "1"),
    ]
    redacted, names = redact_headers(
        headers,
        extra_names=["x-tenant", "X-BOTH"],
        secret_values=["sk_live_123", "abc"],  # "abc" is too short to scrub
        allow_names=["x-session-id", "x-both", "X-Custom-Credential-Header"],
    )
    assert redacted == [
        ("X-Echo", "you sent [REDACTED] to us"),
        ("X-Custom-Credential-Header", "abc"),  # allowed, and "abc" is below the minimum length
        ("X-Tenant", "[REDACTED]"),
        ("X-Session-Id", "s-1"),
        ("X-Both", "[REDACTED]"),  # extra names win over allowed names
        ("X-[REDACTED]", "1"),
    ]
    assert names == ["X-Echo", "X-Tenant", "X-Both", "X-[REDACTED]"]


def test_header_names_are_reported_once() -> None:
    redacted, names = redact_headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
    assert redacted == [("Set-Cookie", "a=[REDACTED]"), ("Set-Cookie", "b=[REDACTED]")]
    assert names == ["Set-Cookie"]


def test_header_redaction_is_idempotent() -> None:
    headers = [("Authorization", "Bearer abc"), ("Cookie", "a=1"), ("Set-Cookie", "s=1; Path=/")]
    once, _ = redact_headers(headers)
    twice, names = redact_headers(once)
    assert twice == once
    assert names == []


# ---------------------------------------------------------------------------
# Query strings and URLs
# ---------------------------------------------------------------------------


def test_redact_query() -> None:
    query = [
        ("api_key", "abc"),
        ("page", "2"),
        ("redirect_uri", "https://app.test/cb?token=t0k&x=1"),
        ("pageToken", "CiAKGjBp"),
        ("q", JWT),
        ("token", ""),
        ("access_token", REDACTED),
        ("note", "key is sk_live_123"),
        ("sk_live_123", ""),
    ]
    redacted, names = redact_query(query, secret_values=["sk_live_123"])
    assert redacted == [
        ("api_key", "[REDACTED]"),
        ("page", "2"),
        ("redirect_uri", "https://app.test/cb?token=[REDACTED]&x=1"),
        ("pageToken", "CiAKGjBp"),
        ("q", "[REDACTED]"),
        ("token", ""),
        ("access_token", "[REDACTED]"),
        ("note", "key is [REDACTED]"),
        ("[REDACTED]", ""),
    ]
    assert names == ["api_key", "redirect_uri", "q", "note", "[REDACTED]"]


def test_redact_query_overrides() -> None:
    redacted, names = redact_query(
        [("key", "AIzaSy123"), ("session_id", "s1"), ("api_key", "k")],
        extra_names=["KEY"],
        allow_names=["session_id"],
    )
    assert redacted == [("key", "[REDACTED]"), ("session_id", "s1"), ("api_key", "[REDACTED]")]
    assert names == ["key", "api_key"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:pass@api.test/p?b=2&api_key=abc&a=%20x+y",
            "https://[REDACTED]:[REDACTED]@api.test/p?b=2&api_key=[REDACTED]&a=%20x+y",
        ),
        ("https://sk_test_abc@api.test/v1", "https://[REDACTED]@api.test/v1"),
        ("https://sk_test_abc:@api.test/v1", "https://[REDACTED]:@api.test/v1"),
        (
            "https://app.test/items;jsessionid=XYZ?api_key=abc&page=2",
            "https://app.test/items;jsessionid=[REDACTED]?api_key=[REDACTED]&page=2",
        ),
        (
            "https://app.test/cb#access_token=t&token_type=bearer&expires_in=3600",
            "https://app.test/cb#access_token=[REDACTED]&token_type=bearer&expires_in=3600",
        ),
        ("/v1/items?token=x&limit=10", "/v1/items?token=[REDACTED]&limit=10"),
        ("https://api.test/p?access%5Ftoken=abc", "https://api.test/p?access%5Ftoken=[REDACTED]"),
        ("https://api.test/p?token=%5BREDACTED%5D", "https://api.test/p?token=%5BREDACTED%5D"),
        ("https://api.test/p?token=&flag&sig=", "https://api.test/p?token=&flag&sig="),
        (f"https://api.test/verify/{JWT}?x=1", "https://api.test/verify/[REDACTED]?x=1"),
        (
            "https://app.test/cb?redirect=https://other.test/z?token=abc&ok=1",
            "https://app.test/cb?redirect=https://other.test/z?token=[REDACTED]&ok=1",
        ),
        ("mailto:jane@example.com", "mailto:jane@example.com"),
        (
            "https://api.test/v1/pets?status=sold&limit=5",
            "https://api.test/v1/pets?status=sold&limit=5",
        ),
    ],
)
def test_redact_url_keeps_everything_else_byte_identical(url: str, expected: str) -> None:
    assert redact_url(url) == expected
    assert redact_url(expected) == expected  # idempotent


def test_redact_url_secret_values_and_overrides() -> None:
    bot = "https://api.telegram.test/bot123456:ABC-DEF/getMe?chat=1"
    assert redact_url(bot, secret_values=["123456:ABC-DEF"]) == (
        "https://api.telegram.test/bot[REDACTED]/getMe?chat=1"
    )
    encoded = "https://api.test/s3cr%2Bt/v?k=s3cr%2Bt%2Fv&q=s3cr+t/v"
    assert redact_url(encoded, secret_values=["s3cr+t/v"]) == (
        "https://api.test/[REDACTED]?k=[REDACTED]&q=[REDACTED]"
    )
    assert redact_url("https://api.test/p?key=AIza&token=t", extra_names=["key"]) == (
        "https://api.test/p?key=[REDACTED]&token=[REDACTED]"
    )
    assert redact_url("https://api.test/p?token0=USDC", allow_names=["token0"]) == (
        "https://api.test/p?token0=USDC"
    )


def test_scrub_text() -> None:
    secrets = ["abcd", "abcdef", "xyz", "p@ss word"]
    text = "1 abcdef 2 abcd 3 xyz 4 p%40ss%20word 5 p%40ss+word 6 p@ss word"
    assert scrub_text(text, secrets) == (
        "1 [REDACTED] 2 [REDACTED] 3 xyz 4 [REDACTED] 5 [REDACTED] 6 [REDACTED]"
    )
    assert scrub_text("nothing to see", []) == "nothing to see"
    assert scrub_text("", ["abcd"]) == ""


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_redact_json_oauth_token_response() -> None:
    body: JsonValue = {
        "access_token": "at-123",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "rt-456",
        "scope": "read write",
        "id_token": JWT,
    }
    redacted, paths = redact_json(body)
    assert redacted == {
        "access_token": "[REDACTED]",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "[REDACTED]",
        "scope": "read write",
        "id_token": "[REDACTED]",
    }
    assert paths == ["$.access_token", "$.refresh_token", "$.id_token"]


def test_redact_json_subtrees_and_non_strings() -> None:
    body: JsonValue = {
        "credentials": {
            "type": "service_account",
            "private_key": "-----BEGIN PRIVATE KEY-----",
            "id": 7,
            "scopes": ["a", "b"],
            "nested": [{"value": "v"}, "plain", None],
        },
        "tokens": ["t1", "t2"],
        "usage": {"total_tokens": 87, "prompt_tokens": 12},
        "password_set": True,
        "api_key": None,
        "password": "",
        "secret_ratio": JsonFloat("0.50"),
        "data": [{"name": "x", "token": "abc"}],
    }
    original = copy.deepcopy(body)
    redacted, paths = redact_json(body)
    assert redacted == {
        "credentials": {
            "type": "service_account",
            "private_key": "[REDACTED]",
            "id": 7,
            "scopes": ["a", "b"],
            "nested": [{"value": "[REDACTED]"}, "[REDACTED]", None],
        },
        "tokens": ["[REDACTED]", "[REDACTED]"],
        "usage": {"total_tokens": 87, "prompt_tokens": 12},
        "password_set": True,
        "api_key": None,
        "password": "",
        "secret_ratio": JsonFloat("0.50"),
        "data": [{"name": "x", "token": "[REDACTED]"}],
    }
    assert paths == [
        "$.credentials.private_key",
        "$.credentials.nested[0].value",
        "$.credentials.nested[1]",
        "$.tokens[0]",
        "$.tokens[1]",
        "$.data[0].token",
    ]
    assert isinstance(redacted, dict)
    ratio = redacted["secret_ratio"]
    assert isinstance(ratio, JsonFloat)
    assert ratio.lexeme == "0.50"
    assert body == original  # the input is not modified


def test_redact_json_keeps_descriptions_and_cursors() -> None:
    metadata: JsonValue = {
        "issuer": "https://as.test",
        "authorization_endpoint": "https://as.test/authorize",
        "token_endpoint": "https://as.test/token",
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "private_key_jwt"],
        "authorization_servers": ["https://as.test"],
        "password_policy": "min 8 characters",
        "nextPageToken": "CiAKGjBpNDd2",
        "results": [{"tokenType": "Bearer"}],
    }
    redacted, paths = redact_json(metadata)
    assert redacted == metadata
    assert paths == []


def test_redact_json_embedded_credentials_and_secret_values() -> None:
    body: JsonValue = {
        "paging": {"next": "https://graph.test/me?access_token=EAAB&after=2"},
        "callback": "https://user:pw@hooks.test/x",
        "note": f"use {JWT} or sk_live_123",
        "sk_live_123": {"ok": True},
        "count": 1234,
    }
    redacted, paths = redact_json(body, secret_values=["sk_live_123", "1234"])
    assert redacted == {
        "paging": {"next": "https://graph.test/me?access_token=[REDACTED]&after=2"},
        "callback": "https://[REDACTED]:[REDACTED]@hooks.test/x",
        "note": "use [REDACTED] or [REDACTED]",
        "[REDACTED]": {"ok": True},
        "count": 1234,  # only strings are scrubbed
    }
    assert paths == ["$.paging.next", "$.callback", "$.note", '$["[REDACTED]"]']


def test_redact_json_overrides() -> None:
    body: JsonValue = {
        "token0": {"symbol": "USDC", "id": "0xa0b8"},
        "customer_ref": "c-1",
        "token": "t",
    }
    redacted, paths = redact_json(body, extra_keys=["Customer_Ref"], allow_keys=["TOKEN0"])
    assert redacted == {
        "token0": {"symbol": "USDC", "id": "0xa0b8"},
        "customer_ref": "[REDACTED]",
        "token": "[REDACTED]",
    }
    assert paths == ["$.customer_ref", "$.token"]
    without_override, _ = redact_json(body)
    assert without_override == {
        "token0": {"symbol": "[REDACTED]", "id": "[REDACTED]"},
        "customer_ref": "c-1",
        "token": "[REDACTED]",
    }


@pytest.mark.parametrize(
    ("value", "expected", "paths"),
    [
        (f"Bearer {JWT}", "Bearer [REDACTED]", ["$"]),
        ("plain", "plain", []),
        (42, 42, []),
        (None, None, []),
        ([JWT, 1], ["[REDACTED]", 1], ["$[0]"]),
    ],
)
def test_redact_json_roots(value: JsonValue, expected: JsonValue, paths: list[str]) -> None:
    assert redact_json(value) == (expected, paths)


def test_redact_json_is_idempotent() -> None:
    body: JsonValue = {"token": "t", "next": "https://x.test/?api_key=k", "list": [{"secret": "s"}]}
    once, _ = redact_json(body)
    twice, paths = redact_json(once)
    assert twice == once
    assert paths == []


def test_public_values_stay_under_secret_keys() -> None:
    body: JsonValue = {
        "users": [
            {"userName": "User 1", "password": "Password1"},  # a public demo's generated password
            {"userName": "someone", "password": "hunter2"},
        ],
        "token": "s3cret-real",
    }
    redacted, paths = redact_json(
        body, secret_values=["s3cret-real"], public_values=["Password1", "s3cret-real"]
    )
    users: Any = redacted
    assert users["users"][0]["password"] == "Password1"
    assert users["users"][1]["password"] == REDACTED  # anyone else's is still redacted
    assert users["token"] == REDACTED  # a real credential, even when listed as public
    assert paths == ["$.users[1].password", "$.token"]


def test_a_redacted_uuid_stays_a_uuid() -> None:
    session = "3f2b8c1e-9d4a-4e6b-8a1c-2b7d9e0f1a2c"
    meta: JsonValue = {"_meta": {"openai/widgetSessionId": session, "sessionId": "abc-123"}}
    redacted, paths = redact_json(meta)
    assert redacted == {"_meta": {"openai/widgetSessionId": REDACTED_UUID, "sessionId": REDACTED}}
    assert paths == ['$._meta["openai/widgetSessionId"]', "$._meta.sessionId"]
    assert redact_json(redacted) == (redacted, [])  # idempotent
