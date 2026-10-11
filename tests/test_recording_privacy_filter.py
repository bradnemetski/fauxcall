from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from fauxcall import jsonfmt
from fauxcall.errors import ConfigurationError
from fauxcall.model import Approvals, RecordingPlan
from fauxcall.privacy import Pseudonymizer
from fauxcall.recording import RealTarget, Recorder, RecordingStore
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.types import JsonValue

KEY = "test-pseudonym-key"
TOKEN = "sk-live-0123456789"
EMAIL = "jane.roe@example.org"


def j(value: object) -> Any:
    return value


def privacy(**config: Any) -> RecordingPrivacy:
    return RecordingPrivacy(
        PrivacyConfig.model_validate(config), secret_values=[TOKEN, ""], pseudonym_key=KEY
    )


def test_a_key_is_required_unless_disabled() -> None:
    with pytest.raises(ConfigurationError, match="FAUXCALL_PSEUDONYM_KEY is missing"):
        RecordingPrivacy(PrivacyConfig(), secret_values=[], pseudonym_key=None)
    disabled = RecordingPrivacy(
        PrivacyConfig(enabled=False), secret_values=[TOKEN], pseudonym_key=None
    )
    assert disabled.pseudonymizer is None
    assert disabled.secrets == [TOKEN]


def test_disabled_still_redacts_credentials() -> None:
    disabled = RecordingPrivacy(
        PrivacyConfig(enabled=False), secret_values=[TOKEN], pseudonym_key=None
    )
    value, redacted, pseudonymized = disabled.json({"password": "hunter2", "email": EMAIL})
    assert value == {"password": "[REDACTED]", "email": EMAIL}
    assert (redacted, pseudonymized) == (["$.password"], [])
    url, path, params, query, headers, body, notes = disabled.request(
        url=f"https://api.test/u/{TOKEN}?api_key=abc123&q=1",
        path=f"/u/{TOKEN}",
        path_params={"id": TOKEN},
        query=[("api_key", "abc123"), ("q", "1")],
        headers=[("Authorization", f"Bearer {TOKEN}"), ("Accept", "*/*")],
        json_body=None,
    )
    assert url == "https://api.test/u/[REDACTED]?api_key=[REDACTED]&q=1"
    assert (path, params) == ("/u/[REDACTED]", {"id": "[REDACTED]"})
    assert query == [("api_key", "[REDACTED]"), ("q", "1")]
    assert headers == [("Authorization", "Bearer [REDACTED]"), ("Accept", "*/*")]
    assert body is None
    assert notes == ["request.header:Authorization", "request.query:api_key"]
    assert disabled.text(f"token={TOKEN}") == "token=[REDACTED]"
    response, names = disabled.response_headers([("Set-Cookie", "sid=abc; Path=/")])
    assert (response, names) == ([("Set-Cookie", "sid=[REDACTED]; Path=/")], ["Set-Cookie"])


def test_json_rules_from_the_config() -> None:
    filter_ = privacy(
        extra_secret_names=["X-Session", "pin"],
        allow_names=["token_count"],
        include={"$.owner.handle": "username"},
        exclude=["$.contact"],
    )
    value, redacted, pseudonymized = filter_.json(
        {
            "token_count": 12,
            "access_token": "abc",
            "pin": "1234",
            "owner": {"handle": "jroe"},
            "contact": {"email": EMAIL},
            "author": {"email": EMAIL, "name": "Jane Roe"},
        }
    )
    value = j(value)
    assert value["token_count"] == 12
    assert value["access_token"] == "[REDACTED]"
    assert value["pin"] == "[REDACTED]"
    assert value["owner"]["handle"].startswith("user_")
    assert value["contact"] == {"email": EMAIL}  # excluded
    assert value["author"]["email"].endswith("@example.com")
    assert value["author"]["name"] != "Jane Roe"
    assert redacted == ["$.access_token", "$.pin"]
    assert set(pseudonymized) == {"$.owner.handle", "$.author.email", "$.author.name"}
    assert filter_.json({"plain": 1}) == ({"plain": 1}, [], [])


def test_known_values_are_replaced_inside_longer_strings() -> None:
    filter_ = privacy(exclude=["$.public"])
    assert filter_.json({"plain": 1, "list": [1, "x"]}) == ({"plain": 1, "list": [1, "x"]}, [], [])
    value, _, pseudonymized = filter_.json(
        {
            "author": {"email": EMAIL, "username": "jroe"},
            "links": [f"mailto:{EMAIL}", {"html_url": "https://example.com/jroe"}],
            "public": {"contact": f"write to {EMAIL}"},
        }
    )
    fake_email = str(Pseudonymizer(KEY).pseudonym(EMAIL, "email"))
    fake_user = str(Pseudonymizer(KEY).pseudonym("jroe", "username"))
    assert j(value)["links"] == [
        f"mailto:{fake_email}",
        {"html_url": f"https://example.com/{fake_user}"},
    ]
    assert j(value)["public"] == {"contact": f"write to {EMAIL}"}  # excluded subtrees stay
    assert pseudonymized == [
        "$.author.email",
        "$.author.username",
        "$.links[0]",
        "$.links[1].html_url",
    ]


def test_request_pseudonyms_are_consistent_everywhere() -> None:
    filter_ = privacy(
        path_params={"username": "username", "n": "username"}, query_params={"email": "email"}
    )
    fake_user = str(Pseudonymizer(KEY).pseudonym("jroe", "username"))
    fake_email = str(Pseudonymizer(KEY).pseudonym(EMAIL, "email"))
    url, path, params, query, headers, body, notes = filter_.request(
        url=f"https://api.test/users/jroe/x/7?email={EMAIL.replace('@', '%40')}&cc={EMAIL}",
        path="/users/jroe/x/7",
        path_params={"username": "jroe", "n": "7", "missing": ""},
        query=[("email", EMAIL), ("cc", EMAIL)],
        headers=[("X-User", "jroe"), ("X-Email", EMAIL)],
        json_body={"email": "new.person@example.net", "note": "hi"},
    )
    fake_seven = str(Pseudonymizer(KEY).pseudonym("7", "username"))
    assert params == {"username": fake_user, "n": fake_seven, "missing": ""}
    assert path == f"/users/{fake_user}/x/{fake_seven}"  # whole segments, even one character long
    assert url.startswith(f"https://api.test/users/{fake_user}/x/{fake_seven}?")
    assert EMAIL not in url
    assert EMAIL.replace("@", "%40") not in url
    assert query == [("email", fake_email), ("cc", fake_email)]  # configured, then known
    assert headers == [("X-User", fake_user), ("X-Email", fake_email)]
    body_email = j(body)["email"]
    assert body_email.endswith("@example.com")
    assert j(body)["note"] == "hi"
    assert notes == [
        "request.pii:$.email",
        "request.path:username",
        "request.path:n",
    ]
    # a value first seen in this request's body is already replaced in its URL
    _, _, _, query2, _, _, _ = filter_.request(
        url="https://api.test/x",
        path="/x",
        path_params={},
        query=[("to", "other.person@example.net")],
        headers=[],
        json_body={"email": "other.person@example.net"},
    )
    assert query2[0][1].endswith("@example.com")


def test_response_headers_and_text_use_known_values() -> None:
    filter_ = privacy()
    value, _, _ = filter_.json({"author": {"email": EMAIL, "username": "jroe"}})
    fake = j(value)["author"]["username"]
    headers, names = filter_.response_headers(
        [("Location", "/users/jroe"), ("Set-Cookie", f"t={TOKEN}")]
    )
    assert headers == [("Location", f"/users/{fake}"), ("Set-Cookie", "t=[REDACTED]")]
    assert names == ["Set-Cookie"]
    assert filter_.text(f"hello jroe, your key is {TOKEN}") == (
        f"hello {fake}, your key is [REDACTED]"
    )


# -- end to end through the recorder ---------------------------------------------------------------


def users_api() -> FastAPI:
    app = FastAPI()

    @app.get("/users/{username}")
    async def user(username: str, request: Request) -> Response:
        return JSONResponse(
            {
                "username": username,
                "email": EMAIL,
                "name": "Jane Roe",
                "api_token": request.headers.get("authorization", "").removeprefix("Bearer "),
                "profile": f"https://api.test/users/{username}",
            },
            headers={"Location": f"/users/{username}"},
        )

    @app.get("/search")
    async def search(request: Request) -> Response:
        return PlainTextResponse(f"results for {request.query_params.get('q')}")

    @app.get("/events")
    async def events() -> Response:
        payload = jsonfmt.dumps({"email": EMAIL, "secret": "s3cr3t-value"})
        return Response(f"data: {payload}\n\n", media_type="text/event-stream")

    return app


async def test_recordings_hold_no_secrets_and_no_personal_data(tmp_path: Path) -> None:
    calls: list[JsonValue] = [
        {
            "id": "user",
            "endpoint": "GET /users/{username}",
            "request": {
                "method": "GET",
                "path": "/users/{username}",
                "path_params": {"username": "jroe"},
            },
        },
        {
            "id": "search",
            "endpoint": "GET /search",
            "request": {"method": "GET", "path": "/search", "query": [["q", EMAIL]]},
        },
        {
            "id": "events",
            "endpoint": "GET /events",
            "request": {"method": "GET", "path": "/events"},
            "stream": {"kind": "sse", "duration_s": 5},
        },
    ]
    plan = RecordingPlan.model_validate(
        {"name": "p", "created_at": "2026-10-10T12:00:00Z", "calls": calls}
    )
    filter_ = RecordingPrivacy(
        PrivacyConfig(path_params={"username": "username"}),
        secret_values=[TOKEN],
        pseudonym_key=KEY,
    )
    store = RecordingStore(tmp_path / "recordings")
    target = RealTarget(base_url="https://api.test", headers={"Authorization": f"Bearer {TOKEN}"})
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=users_api())) as http:
        report = await Recorder(store, target, http, Approvals(), privacy=filter_, rps=0).run(plan)
    assert [o.statuses for o in report.outcomes] == [[200], [200], [200]]
    on_disk = b"".join(p.read_bytes() for p in (tmp_path / "recordings").rglob("*.json"))
    for real in (TOKEN, EMAIL, "jroe", "Jane Roe", "s3cr3t-value"):
        assert real.encode() not in on_disk, real
    exchanges = {e.plan_call: e for _, e in store.iter_exchanges()}
    fake_user = str(Pseudonymizer(KEY).pseudonym("jroe", "username"))
    fake_email = str(Pseudonymizer(KEY).pseudonym(EMAIL, "email"))
    user = exchanges["user"]
    assert user.request.path == f"/users/{fake_user}"
    assert user.request.path_params == {"username": fake_user}
    assert user.response is not None
    body = j(user.response.body.json_value)
    assert body["username"] == fake_user  # the same pseudonym in the URL and in the body
    assert body["email"] == fake_email
    assert body["profile"] == f"https://api.test/users/{fake_user}"
    assert body["api_token"] == "[REDACTED]"
    assert user.response.header("location") == f"/users/{fake_user}"
    assert ("authorization", "Bearer [REDACTED]") in user.request.headers
    search = exchanges["search"]
    assert search.request.query == [("q", fake_email)]  # learned from the earlier response
    assert search.response is not None
    assert search.response.body.text == f"results for {fake_email}"
    (event,) = exchanges["events"].stream
    assert event.data == jsonfmt.dumps({"email": fake_email, "secret": "[REDACTED]"})
