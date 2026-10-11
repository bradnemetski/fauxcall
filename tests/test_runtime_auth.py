from __future__ import annotations

import base64
from typing import Any

import pytest
from structlog.testing import capture_logs

from fauxcall.config import AuthSettings, IdentityConfig
from fauxcall.model.auth import AuthModel
from fauxcall.runtime.auth import Authenticator, AuthOutcome, Identity

S = 1_000_000_000
API_KEY = {"kind": "api_key", "location": "header", "name": "X-API-Key"}
QUERY_KEY = {"kind": "api_key", "location": "query", "name": "api_key"}
COOKIE_KEY = {"kind": "api_key", "location": "cookie", "name": "session"}
TOKEN_KEY = {"kind": "api_key", "location": "header", "name": "Authorization", "prefix": "Token "}
BEARER = {"kind": "bearer"}
OAUTH = {"kind": "oauth2"}
BASIC = {"kind": "basic"}

USERS = [
    {"name": "alice", "api_key": "k-alice", "bearer_token": "t-alice", "scopes": ["read"]},
    {"name": "bob", "status": "denied", "api_key": "k-bob"},
    {"name": "carol", "status": "expired", "api_key": "k-carol"},
    {"name": "dave", "status": "insufficient_scope", "api_key": "k-dave", "bearer_token": "t-dave"},
    {"name": "erin", "username": "erin", "password": "pw"},
    {"name": "frank", "username": "frank"},
    {"name": "josé", "api_key": "clé-josé", "username": "josé", "password": "mot-de-passé"},
]


def authenticator(
    *schemes: dict[str, Any],
    mode: str = "any",
    users: list[dict[str, Any]] | None = None,
    required_for: tuple[str, ...] = ("*",),
    exempt: tuple[str, ...] = (),
) -> Authenticator:
    model = AuthModel.model_validate(
        {"schemes": list(schemes), "required_for": list(required_for), "exempt": list(exempt)}
    )
    settings = AuthSettings.model_validate(
        {"mode": mode, "users": USERS if users is None else users}
    )
    return Authenticator(model, settings)


def check(
    auth: Authenticator,
    *,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    now_ns: int = 0,
    required: bool = True,
) -> AuthOutcome:
    return auth.authenticate(
        headers=headers or {},
        query=query or {},
        cookies=cookies or {},
        now_ns=now_ns,
        required=required,
    )


def who(outcome: AuthOutcome) -> tuple[str | None, str | None]:
    return (outcome.identity.name if outcome.identity else None, outcome.failure)


def basic(credentials: str | bytes) -> dict[str, str]:
    raw = credentials.encode() if isinstance(credentials, str) else credentials
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


ANONYMOUS = Identity("anonymous", "allowed", anonymous=True)


# -- where credentials come from ---------------------------------------------------------------


def test_api_key_in_a_header() -> None:
    auth = authenticator(API_KEY)
    alice = check(auth, headers={"X-API-Key": "k-alice"})
    assert alice == AuthOutcome(Identity("alice", "allowed", ("read",)))
    assert alice.ok
    assert who(check(auth, headers={"x-api-key": "k-alice"})) == ("alice", None)
    assert check(auth, headers={"X-API-Key": "k-unknown"}) == AuthOutcome(ANONYMOUS)
    assert who(check(auth)) == (None, "missing")
    assert who(check(auth, headers={"X-API-Key": ""})) == (None, "missing")
    assert not check(auth).ok
    assert check(auth, required=False) == AuthOutcome(None)


def test_api_key_in_the_query_or_a_cookie() -> None:
    query = authenticator(QUERY_KEY)
    assert who(check(query, query={"api_key": "k-alice"})) == ("alice", None)
    assert who(check(query, headers={"api_key": "k-alice"})) == (None, "missing")
    cookie = authenticator(COOKIE_KEY)
    assert who(check(cookie, cookies={"session": "k-alice"})) == ("alice", None)
    assert who(check(cookie, query={"session": "k-alice"})) == (None, "missing")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Token k-alice", ("alice", None)),
        ("token   k-alice", ("alice", None)),
        ("Bearer k-alice", (None, "invalid")),  # the wrong prefix is a malformed credential
        ("k-alice", (None, "invalid")),
        ("Token", (None, "missing")),
        ("Token   ", (None, "missing")),
        ("", (None, "missing")),
    ],
)
def test_api_key_prefixes(value: str, expected: tuple[str | None, str | None]) -> None:
    assert who(check(authenticator(TOKEN_KEY), headers={"authorization": value})) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bearer t-alice", ("alice", None)),
        ("bearer t-alice", ("alice", None)),
        ("Bearer t-unknown", ("anonymous", None)),
        ("Bearer", (None, "missing")),
        ("Bearer   ", (None, "missing")),
        ("Basic dXNlcjpwdw==", (None, "missing")),  # another scheme's credential
    ],
)
def test_bearer_tokens(value: str, expected: tuple[str | None, str | None]) -> None:
    assert who(check(authenticator(BEARER), headers={"Authorization": value})) == expected


def test_bearer_with_a_custom_prefix() -> None:
    auth = authenticator({"kind": "bearer", "prefix": "Token "})
    assert who(check(auth, headers={"Authorization": "Token t-alice"})) == ("alice", None)
    assert who(check(auth, headers={"Authorization": "Bearer t-alice"})) == (None, "missing")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (basic("erin:pw"), ("erin", None)),
        (basic("erin:wrong"), (None, "invalid")),  # a known user with the wrong password
        (basic("frank:anything"), ("frank", None)),  # no password configured: any is fine
        (basic("zed:pw"), ("anonymous", None)),
        (basic("josé:mot-de-passé"), ("josé", None)),
        (basic(b"\xff:\xfe"), (None, "invalid")),
        (basic("erin"), (None, "invalid")),  # no colon
        (basic(":pw"), (None, "invalid")),  # no username
        ({"Authorization": "Basic !!!"}, (None, "invalid")),
        ({"Authorization": "Basic"}, (None, "missing")),
        ({"Authorization": "Bearer t-alice"}, (None, "missing")),
    ],
)
def test_basic_auth(headers: dict[str, str], expected: tuple[str | None, str | None]) -> None:
    assert who(check(authenticator(BASIC), headers=headers)) == expected


def test_non_ascii_credentials_are_compared_safely() -> None:
    auth = authenticator(API_KEY)
    assert who(check(auth, headers={"X-API-Key": "clé-josé"})) == ("josé", None)
    assert who(check(auth, headers={"X-API-Key": "clé-inconnue"})) == ("anonymous", None)


def test_unknown_scheme_kinds_find_no_credential() -> None:
    assert who(check(authenticator({"kind": "none"}), headers={"X-API-Key": "k"})) == (
        None,
        "missing",
    )


def test_the_first_present_credential_decides() -> None:
    auth = authenticator(API_KEY, BEARER)
    both = {"X-API-Key": "k-bob", "Authorization": "Bearer t-alice"}
    assert who(check(auth, headers=both)) == ("bob", "forbidden")
    blank_key = {"X-API-Key": "", "Authorization": "Bearer t-alice"}
    assert who(check(auth, headers=blank_key)) == ("alice", None)


# -- what credentials look like ----------------------------------------------------------------


def test_format_regex() -> None:
    auth = authenticator({**API_KEY, "format_regex": "k-[a-z]+"})
    assert who(check(auth, headers={"X-API-Key": "k-alice"})) == ("alice", None)
    assert who(check(auth, headers={"X-API-Key": "k-unknown"})) == ("anonymous", None)
    assert who(check(auth, headers={"X-API-Key": "K_ALICE"})) == (None, "invalid")
    assert check(auth, headers={"X-API-Key": "K_ALICE"}, required=False) == AuthOutcome(None)


def test_unusable_format_regexes_are_not_enforced() -> None:
    auth = authenticator({**API_KEY, "format_regex": r"^\p{Lu}{3}-[0-9]+$"})
    with capture_logs() as logs:
        assert who(check(auth, headers={"X-API-Key": "k-alice"})) == ("alice", None)
        assert who(check(auth, headers={"X-API-Key": "k-bob"})) == ("bob", "forbidden")
    assert [entry["event"] for entry in logs] == ["auth_format_regex_invalid"]


# -- who gets in -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "status", "failure"),
    [
        ("k-alice", "allowed", None),
        ("k-bob", "denied", "forbidden"),
        ("k-carol", "expired", "expired"),
        ("k-dave", "insufficient_scope", "insufficient_scope"),
    ],
)
def test_user_statuses(key: str, status: str, failure: str | None) -> None:
    auth = authenticator(API_KEY)
    outcome = check(auth, headers={"X-API-Key": key})
    assert outcome.identity is not None
    assert (outcome.identity.status, outcome.failure) == (status, failure)
    optional = check(auth, headers={"X-API-Key": key}, required=False)
    assert (optional.identity, optional.failure) == (outcome.identity, None)


def test_allowlist_mode_admits_only_configured_users() -> None:
    auth = authenticator(API_KEY, mode="allowlist")
    assert who(check(auth, headers={"X-API-Key": "k-alice"})) == ("alice", None)
    assert who(check(auth, headers={"X-API-Key": "k-unknown"})) == (None, "invalid")
    assert check(auth, headers={"X-API-Key": "k-unknown"}, required=False) == AuthOutcome(None)


def test_off_mode_and_no_model_check_nothing() -> None:
    off = authenticator(API_KEY, mode="off")
    assert check(off, headers={"X-API-Key": "k-bob"}) == AuthOutcome(None)
    assert not off.requires_auth("GET /pets")
    no_model = Authenticator(None, AuthSettings())
    assert check(no_model) == AuthOutcome(None)
    assert not no_model.requires_auth("GET /pets", override=True)


def test_update_users_replaces_the_configured_users() -> None:
    auth = authenticator(API_KEY)
    auth.update_users([IdentityConfig.model_validate({"name": "zoe", "api_key": "k-zoe"})])
    assert who(check(auth, headers={"X-API-Key": "k-zoe"})) == ("zoe", None)
    assert check(auth, headers={"X-API-Key": "k-bob"}) == AuthOutcome(ANONYMOUS)


@pytest.mark.parametrize(
    ("endpoint", "override", "expected"),
    [
        ("GET /v1/pets", None, True),
        ("GET /healthz", None, False),  # exempt
        ("GET /internal/x", None, False),  # not required
        ("GET /healthz", True, True),  # the endpoint's own setting wins
        ("GET /v1/pets", False, False),
    ],
)
def test_requires_auth(endpoint: str, override: bool | None, expected: bool) -> None:
    auth = authenticator(
        API_KEY, required_for=("GET /v1/*", "GET /healthz"), exempt=("GET /healthz",)
    )
    assert auth.requires_auth(endpoint, override) is expected


def test_no_schemes_means_no_auth() -> None:
    assert not authenticator().requires_auth("GET /v1/pets", override=True)


# -- tokens from the built-in OAuth server -----------------------------------------------------


def test_issued_tokens_identify_their_user_until_they_expire() -> None:
    auth = authenticator(OAUTH)
    token = auth.issue_token("alice", now_ns=0, ttl_seconds=60, scopes=("read", "write"))
    bearer = {"Authorization": f"Bearer {token}"}
    assert check(auth, headers=bearer, now_ns=60 * S - 1) == AuthOutcome(
        Identity("alice", "allowed", ("read",))  # configured scopes win over requested ones
    )
    expired = check(auth, headers=bearer, now_ns=60 * S)
    assert expired == AuthOutcome(Identity("alice", "expired", ("read", "write")), "expired")
    assert check(auth, headers=bearer, now_ns=60 * S, required=False) == AuthOutcome(
        Identity("alice", "expired", ("read", "write"))
    )


def test_issued_tokens_carry_the_user_status_and_requested_scopes() -> None:
    auth = authenticator(OAUTH)
    for name, failure in (("bob", "forbidden"), ("dave", "insufficient_scope")):
        token = auth.issue_token(name, now_ns=0, ttl_seconds=60)
        assert who(check(auth, headers={"Authorization": f"Bearer {token}"})) == (name, failure)
    guest = auth.issue_token("anonymous", now_ns=0, ttl_seconds=60, scopes=("read",))
    assert check(auth, headers={"Authorization": f"Bearer {guest}"}) == AuthOutcome(
        Identity("anonymous", "allowed", ("read",), anonymous=True)
    )


def test_oauth_resources_reject_tokens_they_did_not_issue() -> None:
    for mode in ("any", "allowlist"):
        auth = authenticator(OAUTH, mode=mode)
        made_up = {"Authorization": "Bearer made-up"}
        assert who(check(auth, headers=made_up)) == (None, "invalid")
        assert check(auth, headers=made_up, required=False) == AuthOutcome(None)
        assert who(check(auth, headers={"Authorization": "Bearer t-alice"})) == ("alice", None)
        assert who(check(auth)) == (None, "missing")


def test_revoked_tokens_are_invalid() -> None:
    auth = authenticator(OAUTH)
    token = auth.issue_token("alice", now_ns=0, ttl_seconds=60)
    bearer = {"Authorization": f"Bearer {token}"}
    assert who(check(auth, headers=bearer)) == ("alice", None)
    assert auth.revoke_token(token) is True
    assert auth.revoke_token(token) is False
    assert who(check(auth, headers=bearer)) == (None, "invalid")


def test_plain_bearer_schemes_keep_any_mode_semantics_for_unknown_tokens() -> None:
    auth = authenticator(BEARER)
    token = auth.issue_token("alice", now_ns=0, ttl_seconds=60)
    auth.revoke_token(token)
    assert check(auth, headers={"Authorization": f"Bearer {token}"}) == AuthOutcome(ANONYMOUS)


def test_allowlist_rejects_tokens_of_users_removed_after_issue() -> None:
    auth = authenticator(OAUTH, mode="allowlist")
    token = auth.issue_token("alice", now_ns=0, ttl_seconds=60)
    auth.update_users([])
    bearer = {"Authorization": f"Bearer {token}"}
    assert who(check(auth, headers=bearer)) == (None, "invalid")
    assert check(auth, headers=bearer, required=False) == AuthOutcome(None)
