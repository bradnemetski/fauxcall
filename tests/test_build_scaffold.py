"""``fauxcall.build.scaffold``: generated code, workspace files, and the generated fakes' own gates.

The generated project must pass ``ruff check`` (``select = ["ALL"]``), ``ruff format --check``,
``mypy --strict`` and its own tests with no edits. Scaffolding runs in-process in the fast tests,
which also lint and format-check the generated fakes with ruff (well under a second).
``test_generated_fakes_pass_their_own_gates`` is marked ``slow`` (deselected by default; ~20 s with
a warm uv cache): it installs both fakes with ``uv sync --offline`` and runs ruff, mypy and pytest
(with the fakes' 95% coverage gate) in subprocesses. Run it with::

    uv run pytest -m slow tests/test_build_scaffold.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import httpx2
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from _mcp import mcp_app, mcp_model
from fauxcall.build import scaffold as scaffold_module
from fauxcall.build.init import (
    default_gitignore,
    ensure_env,
    ensure_workspace_gitignore,
    env_value,
    init_fake,
    new_progress,
)
from fauxcall.build.scaffold import (
    FIRST_PORT,
    LINE_LIMIT,
    Atom,
    Route,
    ScaffoldResult,
    assign_port,
    call_lines,
    credential_lines,
    doc_text,
    fauxcall_requirement,
    identifier,
    import_line,
    markdown_text,
    overlong,
    param_identifier,
    py_literal,
    py_string,
    route_path,
    routes_for,
    routes_imports,
    scaffold,
    scenarios_for,
    specificity,
    toml_string,
    value_lines,
    with_extra,
)
from fauxcall.errors import ConfigurationError
from fauxcall.jsonfmt import JsonFloat
from fauxcall.model import (
    ApiModel,
    AuthModel,
    AuthScheme,
    Body,
    CapturedRequest,
    CapturedResponse,
    EndpointModel,
    Exchange,
    ParamModel,
    RealTarget,
    SourceInfo,
    StreamEvent,
    StreamModel,
)
from fauxcall.model.plan import PlannedCall, PlannedRequest, RecordingPlan
from fauxcall.paths import FakeLayout, Workspace
from fauxcall.recording.mcp import McpRecorder
from fauxcall.recording.privacy_filter import PrivacyConfig, RecordingPrivacy
from fauxcall.recording.store import RecordingStore
from fauxcall.runtime.dynamic import specificity as runtime_specificity
from fauxcall.storage import write_model
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from fauxcall.model.common import Purpose
    from fauxcall.types import JsonValue

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
BS = "\\"
RIGHT_QUOTE = chr(0x2019)
E_ACUTE = chr(0xE9)
CJK = chr(0x83B7) + chr(0x53D6)
LONG_PATH = (
    "/v1/" + "/".join(["segment-with-a-rather-long-name"] * 4) + "/{first-param}/details/{1st}"
)
RUFF = Path(sys.executable).with_name("ruff")


# -- Python literals ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", '"plain"'),
        ('say "hi"', "'say \"hi\"'"),
        ("it's", '"it\'s"'),
        ('it\'s "q" "r"', f'\'it{BS}\'s "q" "r"\''),
        ("one \"two\" 'three'", f'"one {BS}"two{BS}" \'three\'"'),
        ("tab\tnew\nline\r", f'"tab{BS}tnew{BS}nline{BS}r"'),
        (BS, f'"{BS}{BS}"'),
        ("caf" + E_ACUTE, f'"caf{BS}xe9"'),
        (RIGHT_QUOTE, f'"{BS}u2019"'),
        (chr(0x1F600), f'"{BS}U0001f600"'),
        (chr(0x7F) + chr(1), f'"{BS}x7f{BS}x01"'),
    ],
)
def test_py_string(text: str, expected: str) -> None:
    assert py_string(text) == expected


@given(st.text())
def test_py_string_round_trips_as_ascii(text: str) -> None:
    literal = py_string(text)
    assert literal.isascii()
    assert ast.literal_eval(literal) == text
    assert literal[0] == ("'" if text.count('"') > text.count("'") else '"')


def test_py_literal() -> None:
    value: JsonValue = {
        "a": True,
        "b": None,
        "c": [1, 2.5, 1e20, "x"],
        "d": {},
        "e": [],
        "f": False,
    }
    assert py_literal(value) == (
        '{"a": True, "b": None, "c": [1, 2.5, 1e20, "x"], "d": {}, "e": [], "f": False}'
    )
    assert py_literal(1.5e-7) == "1.5e-07"
    assert py_literal(-0.0) == "-0.0"
    assert py_literal(JsonFloat("1.50")) == "1.5"
    assert py_literal(10**30) == str(10**30)


JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=20,
)


@given(JSON)
def test_py_literal_round_trips(value: JsonValue) -> None:
    literal = py_literal(value)
    assert literal.isascii()
    assert ast.literal_eval(literal) == value


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("x" * LINE_LIMIT, False),
        ("    " + "word " * 30, True),
        ("    " + "single-word-" * 20 + ",", False),
        ('    "url": "https://example.com/' + "a" * 120 + '",', False),
        ("    " + "x " * 60 + "https://example.com/late", True),
    ],
)
def test_overlong_mirrors_ruffs_exemptions(line: str, flagged: bool) -> None:
    assert overlong(line) is flagged


def test_value_lines_and_call_lines() -> None:
    assert call_lines("    ", "f", [Atom("request"), "GET /x"]) == ['    f(request, "GET /x")']
    long_id = "GET /" + "segment/" * 15
    assert call_lines("    ", "return await _serve", [Atom("request"), long_id]) == [
        "    return await _serve(",
        "        request,",
        f'        "{long_id}",  # noqa: E501',
        "    )",
    ]
    nested: JsonValue = {"question": "why " * 30, "tags": ["a", "b"], "deep": {"k": ["v" * 95]}}
    assert value_lines(nested, "        ", suffix=",") == [
        "        {",
        f'            "question": "{"why " * 30}",  # noqa: E501',
        '            "tags": ["a", "b"],',
        '            "deep": {',
        '                "k": [',
        f'                    "{"v" * 95}",',
        "                ],",
        "            },",
        "        },",
    ]
    assert value_lines({}, "    ", prefix="x" * 100 + ": ") == [
        f"    {'x' * 100}: {{}}  # noqa: E501"
    ]
    assert value_lines(Atom("a" * 120), "") == ["a" * 120]


def test_import_line() -> None:
    assert import_line("fastapi", ["APIRouter"]) == "from fastapi import APIRouter"
    package = "fauxcall_" + "x" * 80
    assert import_line(package, ["A", "B"]) == f"from {package} import (\n    A,\n    B,\n)"


def test_doc_text() -> None:
    text = f'Say "hi" {BS} it{RIGHT_QUOTE}s \t new\n line {chr(0x2026)} caf{E_ACUTE} {CJK}'
    assert doc_text(text, 90) == "Say 'hi' / it's new line ... cafe"
    assert doc_text("abcdef " * 20, 20) == "abcdef abcdef abc..."
    assert len(doc_text("abcdef " * 20, 20)) == 20
    assert doc_text(CJK, 20) == ""


@given(st.text())
def test_toml_string_round_trips(text: str) -> None:
    parsed = tomllib.loads(f"x = {toml_string(text)}")["x"]
    assert parsed == " ".join(text.split())


def test_markdown_text() -> None:
    assert markdown_text("a | b\n  c") == "a \\| b c"


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("findPetsByStatus", "find_pets_by_status"),
        ("getHTTPResponse", "get_http_response"),
        ("GET_/v1/pets/{pet-id}", "get_v1_pets_pet_id"),
        ("list", "op_list"),
        ("import", "op_import"),
        ("router", "op_router"),
        ("handle", "op_handle"),
        ("2fa", "op_2fa"),
        ("", "endpoint"),
        ("caf" + E_ACUTE, "cafe"),
        (CJK, "endpoint"),
        ("x" * 80, "x" * 50),
    ],
)
def test_identifier(base: str, expected: str) -> None:
    assert identifier(base, set()) == expected


def test_identifier_collisions_stay_unique_and_short() -> None:
    taken: set[str] = set()
    assert [identifier("getPet", taken) for _ in range(3)] == ["get_pet", "get_pet_2", "get_pet_3"]
    long = [identifier("y" * 80, taken) for _ in range(3)]
    assert long == ["y" * 50, "y" * 48 + "_2", "y" * 48 + "_3"]
    assert identifier("2fa", set(), prefix="") == "2fa"


# -- requirement, ordering, paths -----------------------------------------------------------------


def test_fauxcall_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert fauxcall_requirement(str(tmp_path)) == ("fauxcall", str(tmp_path.resolve()))
    assert fauxcall_requirement(">=0.2") == ("fauxcall>=0.2", None)
    assert fauxcall_requirement("~=0.1") == ("fauxcall~=0.1", None)
    assert fauxcall_requirement("fauxcall==0.2.1") == ("fauxcall==0.2.1", None)
    assert fauxcall_requirement("==0.2.1") == ("fauxcall==0.2.1", None)
    monkeypatch.chdir(tmp_path.parent)
    assert fauxcall_requirement(tmp_path.name) == ("fauxcall", str(tmp_path.resolve()))
    for missing in ("/does/not/exist", "./nope", "~/nope-fauxcall"):
        with pytest.raises(ConfigurationError, match="not a directory"):
            fauxcall_requirement(missing)
    for version, expected in (
        ("0.3.1", "fauxcall>=0.3.1,<0.4"),
        ("1.2.3", "fauxcall>=1.2.3,<2"),
        ("2", "fauxcall>=2,<3"),
    ):
        monkeypatch.setattr(scaffold_module, "__version__", version)
        assert fauxcall_requirement(None) == (expected, None)


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("fauxcall", "fauxcall[testing]"),
        ("fauxcall>=0.1.0,<0.2", "fauxcall[testing]>=0.1.0,<0.2"),
        ("fauxcall==0.2.1", "fauxcall[testing]==0.2.1"),
        (
            "fauxcall @ git+https://example.com/f.git",
            "fauxcall[testing] @ git+https://example.com/f.git",
        ),
        ("fauxcall [foo] >=1", "fauxcall[foo,testing] >=1"),
        ("FauxCall[testing]", "fauxcall[testing]"),
        ('fauxcall;python_version>"3"', 'fauxcall[testing];python_version>"3"'),
        ("fauxcallx", "fauxcall[testing]"),
    ],
)
def test_with_extra(requirement: str, expected: str) -> None:
    assert with_extra(requirement, "testing") == expected


def endpoint(method: str, path: str, **fields: object) -> EndpointModel:
    names = re.findall(r"\{([^{}/]+)\}", path)
    params = [ParamModel(name=n, location="path", required=True) for n in names]
    return EndpointModel.model_validate(
        {"id": f"{method} {path}", "method": method, "path": path, "params": params, **fields}
    )


def test_routes_are_ordered_like_the_real_routers() -> None:
    endpoints = [
        endpoint("GET", "/v1/{date}"),
        endpoint("GET", "/v1/{start_date}..{end_date}"),
        endpoint("GET", "/v1/latest"),
        endpoint("GET", "/v1/{start_date}.."),
        endpoint("GET", "/v1/currencies"),
        endpoint("GET", "/pet/{petId}"),
        endpoint("GET", "/pet/findByStatus"),
    ]
    ordered = [e.path for e in sorted(endpoints, key=specificity)]
    assert ordered == [
        "/pet/findByStatus",
        "/v1/currencies",
        "/v1/latest",
        "/v1/{start_date}..",
        "/v1/{start_date}..{end_date}",
        "/pet/{petId}",
        "/v1/{date}",
    ]
    assert ordered == [e.path for e in sorted(endpoints, key=runtime_specificity)]


def test_route_paths_use_identifiers() -> None:
    assert route_path("/pets/{pet-id}/x/{1st}/{ok}") == "/pets/{pet_id}/x/{p_1st}/{ok}"
    assert param_identifier("pet-id") == "pet_id"


def test_route_lines() -> None:
    http = Route("get", "/pets/{pet_id}", "get_pet", "GET /pets/{pet-id}.", "GET /pets/{pet-id}")
    assert http.path_line == '    "/pets/{pet_id}",  # noqa: FAST003'
    assert not http.websocket
    assert http.block().splitlines() == [
        "",
        "",
        "@router.get(",
        '    "/pets/{pet_id}",  # noqa: FAST003',
        '    name="get_pet",',
        "    include_in_schema=False,",
        ")",
        "async def get_pet(request: Request) -> Response:",
        '    """GET /pets/{pet-id}."""',
        '    return await _serve(request, "GET /pets/{pet-id}")',
    ]
    socket = Route("websocket", "/rooms/{room_id}", "room", "GET /rooms/{room-id}.", "GET /r")
    assert socket.path_line == '    "/rooms/{room_id}",'
    assert socket.block().splitlines()[2:] == [
        "@router.websocket(",
        '    "/rooms/{room_id}",',
        '    name="room",',
        ")",
        "async def room(websocket: WebSocket) -> None:",
        '    """GET /rooms/{room-id}."""',
        '    await _serve_websocket(websocket, "GET /r")',
    ]
    spaced = Route("get", "/a b" * 40, "spaced", "GET.", "GET")
    assert spaced.path_line.endswith("  # noqa: E501")


def model_with(*endpoints: EndpointModel, **fields: object) -> ApiModel:
    return ApiModel.model_validate(
        {
            "name": "demo",
            "title": "Demo",
            "source": SourceInfo(
                kind="openapi", url="https://api.test/openapi.json", fetched_at=NOW
            ),
            "real": RealTarget(base_url="https://api.test"),
            "endpoints": list(endpoints),
            **fields,
        }
    )


@pytest.mark.parametrize(
    ("summary", "doc"),
    [
        (None, "GET /x."),
        ("", "GET /x."),
        ("...", "GET /x."),
        ("Get it.", "GET /x: Get it."),
        ('Say "hi" \\ with """triple""" quotes', "GET /x: Say 'hi' / with '''triple''' quotes."),
        ("Is it?", "GET /x: Is it?."),
        ("Line one\nline two", "GET /x: Line one line two."),
        ("y" * 200, "GET /x: " + "y" * 78 + "..."),
    ],
)
def test_route_docstrings(summary: str | None, doc: str) -> None:
    (route,) = routes_for(model_with(endpoint("GET", "/x", summary=summary)))
    assert route.doc == doc
    assert len(f'    """{route.doc}"""') <= LINE_LIMIT


def test_route_functions_avoid_reserved_and_duplicate_names() -> None:
    routes = routes_for(
        model_with(
            endpoint("GET", "/a", operation_id="getPet"),
            endpoint("GET", "/b", operation_id="getPet"),
            endpoint("GET", "/c", operation_id="list"),
            endpoint("GET", "/d"),
            endpoint("GET", "/ws", role="websocket", stream=StreamModel(kind="websocket")),
        )
    )
    assert [r.function for r in routes] == ["get_ws", "get_pet", "get_pet_2", "op_list", "get_d"]
    assert [r.decorator for r in routes] == ["websocket", "get", "get", "get", "get"]


def test_routes_imports() -> None:
    http = Route("get", "/a", "a", "GET /a.", "GET /a")
    socket = Route("websocket", "/b", "b", "GET /b.", "GET /b")
    assert routes_imports("pkg", []) == "from fastapi import APIRouter"
    assert routes_imports("pkg", [http]).splitlines() == [
        "from fastapi import APIRouter, Request, Response",
        "from fauxcall.runtime import handle",
        "",
        "from pkg.handlers import HANDLERS",
    ]
    assert routes_imports("pkg", [socket]).splitlines() == [
        "from fastapi import APIRouter, WebSocket",
        "from fauxcall.runtime import handle_websocket",
        "",
        "from pkg.handlers import WEBSOCKET_HANDLERS",
    ]
    assert routes_imports("pkg", [http, socket]).splitlines()[-1] == (
        "from pkg.handlers import HANDLERS, WEBSOCKET_HANDLERS"
    )


def test_credential_lines() -> None:
    assert credential_lines(model_with()) == []
    schemes = [
        AuthScheme(kind="api_key", location="header", name="X-API-Key"),
        AuthScheme(kind="api_key", location="query", name="apikey"),
        AuthScheme(kind="api_key", location="cookie", name="session"),
        AuthScheme(kind="bearer", prefix="Bearer "),
        AuthScheme(kind="basic"),
        AuthScheme(kind="oauth2"),
        AuthScheme(kind="none"),
    ]
    lines = credential_lines(model_with(auth=AuthModel(schemes=schemes)))
    assert lines == [
        "FAUXCALL_REAL__HEADERS='"
        + json.dumps(
            {
                "X-API-Key": "your-api-key",
                "Cookie": "session=your-api-key",
                "Authorization": "Bearer your-token",
            }
        )
        + "'",
        "FAUXCALL_REAL__QUERY='" + json.dumps({"apikey": "your-api-key"}) + "'",
    ]
    basic = credential_lines(model_with(auth=AuthModel(schemes=[AuthScheme(kind="basic")])))
    assert basic == [
        "FAUXCALL_REAL__HEADERS='"
        + json.dumps({"Authorization": "Basic base64(user:password)"})
        + "'"
    ]


# -- scenarios ------------------------------------------------------------------------------------


def recorded(
    store: RecordingStore,
    endpoint_id: str,
    path: str,
    status: int,
    body: JsonValue,
    *,
    method: HttpMethod = HttpMethod.GET,
    query: list[tuple[str, str]] | None = None,
    path_params: dict[str, str] | None = None,
    purpose: Purpose = "sample",
    request_body: JsonValue = None,
) -> Exchange:
    exchange = Exchange(
        id=store.next_id(),
        endpoint=endpoint_id,
        purpose=purpose,
        started_at=NOW,
        started_ns=int(NOW.timestamp()) * 1_000_000_000 + len(path),
        request=CapturedRequest(
            method=method,
            url=f"https://api.tricky.test{path}",
            path=path,
            path_params=path_params or {},
            query=query or [],
            headers=[("authorization", "Bearer [REDACTED]"), ("accept", "application/json")],
            body=Body(kind="json", json_value=request_body) if request_body is not None else Body(),
        ),
        response=CapturedResponse(
            status=status,
            headers=[("content-type", "application/json")],
            body=Body(kind="json", content_type="application/json", json_value=body),
        ),
    )
    store.save(exchange)
    return exchange


def test_rest_scenarios(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    api = model_with(
        endpoint("GET", "/pets/{pet-id}"),
        endpoint("GET", "/pets"),
        endpoint("POST", "/pets"),
        endpoint("GET", "/jobs/{id}", role="replay"),
        endpoint("GET", "/files/{id}", role="download"),
        auth=AuthModel(schemes=[AuthScheme(kind="api_key", location="query", name="apikey")]),
    )
    exchanges = [
        recorded(
            store, "GET /pets/{pet-id}", "/pets/1", 200, {"id": 1}, path_params={"pet-id": "1"}
        ),
        recorded(store, "GET /pets/{pet-id}", "/pets/2", 200, {"id": 2}),
        recorded(
            store,
            "GET /pets",
            "/pets",
            200,
            [],
            query=[("status", 'avail"able'), ("apikey", "k"), ("token", "[REDACTED]")],
        ),
        recorded(store, "POST /pets", "/pets", 201, {"id": 3}, method=HttpMethod.POST),
        recorded(store, "GET /files/{id}", "/files/1", 200, {}),
        recorded(store, "GET /unknown", "/unknown", 200, {}),
        recorded(store, "GET /jobs/{id}", "/jobs/1", 404, {}, purpose="invalid"),
        recorded(store, "GET /jobs/{id}", "/jobs/2", 404, {"message": "gone"}, purpose="variation"),
    ]
    exchanges.append(exchanges[0].model_copy(update={"response": None, "endpoint": "GET /pets/x"}))
    scenarios = scenarios_for(api, exchanges)
    assert scenarios == [
        {
            "name": "recorded_get_pets_pet_id",
            "body": '    response = await api.get("/pets/1")\n'
            "    assert response.status_code == 200",
        },
        {
            "name": "recorded_get_pets",
            "body": '    response = await api.get("/pets?status=avail%22able")\n'
            "    assert response.status_code == 200",
        },
        {
            "name": "recorded_get_jobs_id",
            "body": '    response = await api.get("/jobs/2")\n'
            "    assert response.status_code == 404",
        },
    ]
    assert len(scenarios_for(api, exchanges, limit=1)) == 1


def mcp_call(store: RecordingStore, sent: JsonValue) -> Exchange:
    exchange = Exchange(
        id=store.next_id(),
        endpoint="MCP tools/call:ask",
        started_at=NOW,
        started_ns=0,
        request=CapturedRequest(
            method=HttpMethod.POST,
            url="https://mcp.test/mcp",
            path="/mcp",
            body=Body(kind="json", json_value=sent),
        ),
        response=CapturedResponse(status=200, body=Body(kind="json", json_value={"result": {}})),
    )
    store.save(exchange)
    return exchange


def test_mcp_scenarios(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    api = mcp_model()
    question = "How do hooks work?"
    calls = [
        mcp_call(store, "not an object"),
        mcp_call(store, {"method": "tools/list"}),
        mcp_call(store, {"method": "tools/call", "params": "nope"}),
        mcp_call(
            store, {"method": "tools/call", "params": {"name": "ask", "arguments": {"q": question}}}
        ),
    ]
    other = mcp_call(store, {"method": "tools/call", "params": {"name": "now", "arguments": 3}})
    calls.append(other.model_copy(update={"endpoint": "MCP tools/call:now"}))
    scenarios = scenarios_for(api, calls)
    assert [s["name"] for s in scenarios] == [
        "recorded_mcp_tools_call_ask",
        "recorded_mcp_tools_call_now",
    ]
    assert scenarios[0]["body"] == (
        '    result = await mcp.call_tool("", {})\n    assert result is not None'
    )
    assert scenarios[1]["body"] == (
        '    result = await mcp.call_tool("now", {})\n    assert result is not None'
    )


def test_mcp_scenarios_explode_long_arguments(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    question = "How do hooks work? " * 6
    sent: JsonValue = {
        "method": "tools/call",
        "params": {"name": "ask_question", "arguments": {"repoName": "r", "question": question}},
    }
    (scenario,) = scenarios_for(mcp_model(), [mcp_call(store, sent)])
    assert scenario["body"].splitlines() == [
        "    result = await mcp.call_tool(",
        '        "ask_question",',
        "        {",
        '            "repoName": "r",',
        f'            "question": "{question}",  # noqa: E501',
        "        },",
        "    )",
        "    assert result is not None",
    ]


# -- workspace helpers ----------------------------------------------------------------------------


def write_fake(workspace: Workspace, model: ApiModel) -> FakeLayout:
    """What ``fauxcall init`` leaves behind: model, checkpoints, ``.env``."""
    ensure_workspace_gitignore(workspace)
    layout = workspace.fake(model.name)
    write_model(layout.model_path, model)
    write_model(
        layout.progress_path,
        new_progress(layout.name, model.source.url, [e.id for e in model.endpoints], NOW),
    )
    ensure_env(layout, model.real.base_url)
    return layout


def named(name: str, title: str = "Demo") -> ApiModel:
    return model_with(endpoint("GET", "/items/{id}")).model_copy(
        update={"name": name, "title": title}
    )


def test_assign_port(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    first = write_fake(workspace, named("first"))
    assert assign_port(workspace, first) == FIRST_PORT
    (first.root / "fauxcall.toml").write_text("port = 8701\n")
    second = write_fake(workspace, named("second"))
    assert assign_port(workspace, second) == FIRST_PORT + 1
    (second.root / "fauxcall.toml").write_text("port = 9000\n")
    assert assign_port(workspace, second) == 9000
    third = write_fake(workspace, named("third"))
    assert assign_port(workspace, third) == FIRST_PORT + 1
    (third.root / "fauxcall.toml").write_text("port = [broken\n")
    assert assign_port(workspace, third) == FIRST_PORT + 1
    (third.root / "fauxcall.toml").write_text("port = true\n")
    assert assign_port(workspace, third) == FIRST_PORT + 1
    (third.root / "fauxcall.toml").write_bytes(b"port = \xff\n")
    assert assign_port(workspace, third) == FIRST_PORT + 1


def test_update_members(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.other]\nmembers = ["keep"]\n\n[tool.uv.workspace]  # fakes\n'
        'members = [\n  "b",\n]\n\n[tool.ruff]\nline-length = 100\n'
    )
    assert scaffold_module._update_members(pyproject, "a") is True
    data = tomllib.loads(pyproject.read_text())
    assert data["tool"]["uv"]["workspace"]["members"] == ["a", "b"]
    assert data["tool"]["other"]["members"] == ["keep"]
    assert data["tool"]["ruff"]["line-length"] == 100
    assert scaffold_module._update_members(pyproject, "b") is False

    bare = tmp_path / "bare.toml"
    bare.write_text("[tool.uv.workspace]\nexclude = []\n")
    assert scaffold_module._update_members(bare, "x") is True
    assert tomllib.loads(bare.read_text())["tool"]["uv"]["workspace"] == {
        "members": ["x"],
        "exclude": [],
    }
    missing = tmp_path / "missing.toml"
    missing.write_text('[project]\nname = "x"\n')
    assert scaffold_module._update_members(missing, "y") is True
    assert tomllib.loads(missing.read_text())["tool"]["uv"]["workspace"]["members"] == ["y"]


def test_workflow_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert scaffold_module._workflow_dir(Workspace(project / "tools" / "fakes"), project) == (
        "tools/fakes"
    )
    assert scaffold_module._workflow_dir(Workspace(project), project) == "."
    assert scaffold_module._workflow_dir(Workspace(tmp_path / "elsewhere"), project) == "elsewhere"


def test_summary_paths(tmp_path: Path) -> None:
    result = ScaffoldResult(written=[tmp_path / "a" / "b.py"], kept=[Path("/elsewhere/c.py")])
    assert result.summary(tmp_path).splitlines() == [
        f"- wrote `{Path('a') / 'b.py'}`",
        f"- kept `{Path('/elsewhere/c.py')}` (yours)",
    ]


# -- whole fakes ----------------------------------------------------------------------------------


def tricky_model(*, auth: bool = False) -> ApiModel:
    """Everything that tends to break code generation, in one REST API."""
    stream = StreamModel(kind="websocket")
    title = (
        f'Tricky "API" {BS} with \'\'\' quotes """ and a very long title '
        f"{RIGHT_QUOTE}caf{E_ACUTE}{RIGHT_QUOTE} {CJK} that goes on and on beyond the line limit"
    )
    schemes = [
        AuthScheme(kind="api_key", location="query", name="apikey"),
        AuthScheme(kind="bearer"),
    ]
    return ApiModel(
        name="tricky",
        title=title,
        source=SourceInfo(
            kind="openapi", url="https://api.tricky.test/openapi.json", fetched_at=NOW
        ),
        real=RealTarget(base_url="https://api.tricky.test", base_path="/v1"),
        auth=AuthModel(schemes=schemes) if auth else None,
        endpoints=[
            endpoint(
                "GET", "/v1/pets/{pet-id}", operation_id="getPet", summary='By "id" \\ """x"""'
            ),
            endpoint(
                "GET", "/v1/pets/findByStatus", operation_id="findPetsByStatus", summary="A\nB"
            ),
            endpoint(
                "DELETE", "/v1/pets/{pet-id}", operation_id="list", summary=f"{CJK} {E_ACUTE}"
            ),
            endpoint("POST", "/v1/pets", operation_id="import", summary="Create a pet."),
            endpoint("GET", "/v1/{start_date}..{end_date}", summary="Rates for a time period"),
            endpoint("GET", "/v1/{start_date}..", summary="Rates since a date"),
            endpoint("GET", "/v1/{date}", operation_id="handle", summary="Rates for a past date"),
            endpoint("GET", "/v1/latest", operation_id="router", summary="x" * 200),
            endpoint("GET", LONG_PATH, operation_id="getTheDetailsOfAVeryLongResourceNameForever"),
            endpoint("GET", "/v1/stream/{room-id}", role="websocket", stream=stream),
            endpoint("GET", LONG_PATH.replace("details", "live"), role="websocket", stream=stream),
            endpoint("GET", f"/v1/caf{E_ACUTE}/{{id}}", operation_id=f"obtenirCaf{E_ACUTE}"),
            endpoint("GET", "/v1/search", operation_id="2fa", summary="..."),
            endpoint("GET", "/v1/ticks", role="sse", stream=StreamModel(kind="sse", loop=True)),
        ],
    )


def record_tricky(layout: FakeLayout) -> list[Exchange]:
    store = RecordingStore(layout.recordings)
    concrete = LONG_PATH.replace("{first-param}", "abc").replace("{1st}", "7")
    redacted = [("apikey", "[REDACTED]")]
    recorded(
        store,
        "GET /v1/pets/{pet-id}",
        "/v1/pets/1",
        200,
        {"id": 1, "tags": []},
        path_params={"pet-id": "1"},
        query=redacted,
    )
    recorded(
        store,
        "GET /v1/pets/findByStatus",
        "/v1/pets/findByStatus",
        200,
        [{"id": 1, "name": "Rex"}],
        query=[("status", 'avail"able'), ("tags", "caf" + E_ACUTE), *redacted],
    )
    recorded(
        store,
        "GET /v1/{start_date}..{end_date}",
        "/v1/2020-01-01..2020-01-31",
        200,
        {"rates": {"USD": 1.1}},
        path_params={"start_date": "2020-01-01", "end_date": "2020-01-31"},
        query=redacted,
    )
    recorded(
        store,
        "GET /v1/{start_date}..",
        "/v1/2020-01-01..",
        200,
        {"rates": {}},
        path_params={"start_date": "2020-01-01"},
        query=redacted,
    )
    recorded(
        store,
        "GET /v1/{date}",
        "/v1/2020-01-01",
        200,
        {"date": "2020-01-01"},
        path_params={"date": "2020-01-01"},
        query=redacted,
    )
    recorded(
        store,
        "GET /v1/{date}",
        "/v1/1999-01-01",
        404,
        {"message": "not found"},
        path_params={"date": "1999-01-01"},
        purpose="invalid",
        query=redacted,
    )
    recorded(
        store,
        "GET /v1/latest",
        "/v1/latest",
        200,
        {"amount": 1.0, "huge": 1e20, "flag": True, "none": None},
        query=redacted,
    )
    recorded(
        store,
        f"GET {LONG_PATH}",
        concrete,
        200,
        {"ok": True},
        path_params={"first-param": "abc", "1st": "7"},
        query=redacted,
    )
    recorded(
        store,
        "POST /v1/pets",
        "/v1/pets",
        201,
        {"id": 2},
        method=HttpMethod.POST,
        request_body={"name": f"caf{E_ACUTE}"},
        query=redacted,
    )
    recorded(
        store, "GET /v1/search", "/v1/search", 200, {"q": None}, query=[("q", "x" * 120), *redacted]
    )
    ticks = recorded(store, "GET /v1/ticks", "/v1/ticks", 200, None)
    stream = [StreamEvent(t_ms=float(n), kind="sse", data=f"tick {n}") for n in range(3)]
    assert ticks.response is not None
    response = ticks.response.model_copy(update={"body": Body()})
    store.save(ticks.model_copy(update={"stream": stream, "response": response}))
    return [exchange for _, exchange in store.iter_exchanges()]


def scaffold_tricky(
    workspace: Workspace, project: Path, *, auth: bool = False, source: str | None = None
) -> tuple[FakeLayout, ScaffoldResult]:
    model = tricky_model(auth=auth)
    layout = write_fake(workspace, model)
    result = scaffold(
        layout,
        workspace,
        model,
        exchanges=record_tricky(layout),
        project_root=project,
        source=source,
    )
    return layout, result


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if ".venv" not in path.parts)


def test_scaffold_writes_a_complete_fake(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout, result = scaffold_tricky(workspace, tmp_path, source=str(REPO))
    expected = {
        "pyproject.toml",
        "README.md",
        "fauxcall.toml",
        ".env.example",
        "Dockerfile",
        "src/fauxcall_tricky/__init__.py",
        "src/fauxcall_tricky/__main__.py",
        "src/fauxcall_tricky/settings.py",
        "src/fauxcall_tricky/app.py",
        "src/fauxcall_tricky/routes.py",
        "src/fauxcall_tricky/handlers.py",
        "tests/conftest.py",
        "tests/unit/test_app.py",
        "tests/unit/test_routes.py",
        "tests/unit/test_recorded.py",
        "tests/e2e/test_scenarios.py",
    }
    assert {
        p.relative_to(layout.root).as_posix()
        for p in result.written
        if p.is_relative_to(layout.root)
    } == expected
    assert workspace.root / ".gitignore" in result.kept  # init wrote it first
    assert (workspace.root / ".gitignore").read_text() == default_gitignore()
    for path in python_files(layout.root):
        compile(path.read_text(), str(path), "exec")
        assert path.read_text().isascii(), path
    routes = (layout.src / "routes.py").read_text()
    order = [
        routes.index(f'"{p}"')
        for p in (
            "/v1/pets/findByStatus",
            "/v1/pets/{pet_id}",
            "/v1/{start_date}..",
            "/v1/{start_date}..{end_date}",
            "/v1/{date}",
        )
    ]
    assert order == sorted(order)
    assert "async def op_list(request: Request) -> Response:" in routes
    assert "@router.websocket(" in routes
    pyproject = tomllib.loads((layout.root / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "fauxcall-tricky"
    assert pyproject["project"]["dependencies"] == ["fauxcall"]
    assert pyproject["dependency-groups"]["dev"] == [
        "fauxcall[testing]",
        "mypy>=2.0",
        "pytest-cov>=7.0",
        "ruff>=0.15",
    ]
    assert pyproject["project"]["description"].startswith('FauxCall fake of Tricky "API" \\ with')
    assert pyproject["project"]["scripts"] == {"fauxcall-tricky": "fauxcall_tricky.__main__:main"}
    assert tomllib.loads((layout.root / "fauxcall.toml").read_text()) == {
        "name": "tricky",
        "port": FIRST_PORT,
    }
    workspace_toml = tomllib.loads((workspace.root / "pyproject.toml").read_text())
    assert workspace_toml["tool"]["uv"]["workspace"]["members"] == ["tricky"]
    assert "dependency-groups" not in workspace_toml  # each fake declares its own dev tools
    assert workspace_toml["tool"]["uv"]["sources"]["fauxcall"] == {
        "path": str(REPO),
        "editable": True,
    }
    settings = (layout.src / "settings.py").read_text()
    assert f"port: int = {FIRST_PORT}" in settings
    assert 'name: str = "tricky"' in settings
    scenarios = (layout.tests / "e2e" / "test_scenarios.py").read_text()
    assert (
        "async def test_recorded_get_v1_pets_find_by_status(api: ApiClient) -> None:" in scenarios
    )
    assert "apikey" not in scenarios
    env_example = (layout.root / ".env.example").read_text()
    assert "FAUXCALL_REAL__BASE_URL=https://api.tricky.test\n" in env_example
    readme = (layout.root / "README.md").read_text()
    assert readme.startswith('# Tricky "API" \\ with')
    assert "http://127.0.0.1:8701" in readme
    workflow = (tmp_path / ".github" / "workflows" / "fauxcall.yml").read_text()
    assert 'fake: ["tricky"]' in workflow
    assert "working-directory: fakes/${{ matrix.fake }}" in workflow
    compose = (workspace.root / "compose.yaml").read_text()
    assert '- "8701:8701"' in compose
    assert "dockerfile: tricky/Dockerfile" in compose


def test_rescaffold_keeps_owned_files_and_rewrites_generated_ones(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout, _ = scaffold_tricky(workspace, tmp_path)
    owned = [
        layout.src / "handlers.py",
        layout.src / "settings.py",
        layout.src / "app.py",
        layout.root / "pyproject.toml",
        layout.root / "fauxcall.toml",
        layout.root / "Dockerfile",
        layout.tests / "conftest.py",
        layout.tests / "unit" / "test_app.py",
        layout.tests / "e2e" / "test_scenarios.py",
        workspace.root / ".gitignore",
    ]
    generated = [
        layout.src / "routes.py",
        layout.root / "README.md",
        layout.root / ".env.example",
        workspace.root / "README.md",
        workspace.root / "compose.yaml",
        workspace.root / ".dockerignore",
        tmp_path / ".github" / "workflows" / "fauxcall.yml",
    ]
    originals = {path: path.read_text() for path in generated}
    for path in owned + generated:
        path.write_text(path.read_text() + "\n# edited by hand\n")
    model = tricky_model().model_copy(
        update={"auth": AuthModel(schemes=[AuthScheme(kind="bearer")])}
    )
    result = scaffold(layout, workspace, model, project_root=tmp_path)
    for path in owned:
        assert path.read_text().endswith("# edited by hand\n"), path
        assert path in result.kept
    for path in generated:
        assert path in result.written
    for path in generated:
        if path.name not in {".env.example", "README.md"}:
            assert path.read_text() == originals[path], path
    assert "Bearer your-token" in (layout.root / ".env.example").read_text()
    readme = (layout.root / "README.md").read_text()
    send = "refuses calls without credentials: send `Authorization: Bearer any-token` (any\n"
    assert send in readme
    assert workspace.root / "pyproject.toml" in result.kept


def test_images_never_get_credentials(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    scaffold_tricky(workspace, tmp_path)
    ignored = (workspace.root / ".dockerignore").read_text().splitlines()
    for pattern in ("**/.env", "**/.env.*", "!**/.env.example", "**/.venv", "**/data/state"):
        assert pattern in ignored  # COPY . /workspace would otherwise copy every fake's .env


def test_readme_notes_survive_a_rescaffold(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout, _ = scaffold_tricky(workspace, tmp_path)
    readme = layout.root / "README.md"
    fresh = readme.read_text()
    assert "## Known limitations and notes" in fresh
    placeholder = "None yet: list what the fake does differently from the real API and why.\n"
    assert placeholder in fresh
    notes = "- **Rates stop after the last recording.** Travel the clock back for trends.\n"
    readme.write_text(fresh.replace(placeholder, notes) + "\nText outside the notes.\n")
    model = tricky_model()
    model.endpoints = model.endpoints[:1]
    result = scaffold(layout, workspace, model, project_root=tmp_path)
    text = readme.read_text()
    assert notes in text  # the author's notes carry over
    assert placeholder not in text
    assert "Text outside the notes." not in text  # everything else is regenerated
    assert "Endpoints: 1." in text
    assert readme in result.written


def test_a_readme_from_before_notes_regions_is_kept(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout, _ = scaffold_tricky(workspace, tmp_path)
    readme = layout.root / "README.md"
    older = "# Tricky\n\n## Known limitations\n\n- written by the agent that built it\n"
    readme.write_text(older)
    result = scaffold(layout, workspace, tricky_model(), project_root=tmp_path)
    assert readme.read_text() == older  # no markers: can't tell generated text from the author's
    assert readme in result.kept


def test_fakes_brought_together_get_their_own_ports(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    fakes = {name: write_fake(workspace, named(name)) for name in ("alpha", "beta", "gamma")}
    for name, port in (("alpha", 8701), ("beta", 8701), ("gamma", 8702)):
        config = (
            f'# copied from elsewhere\nname = "{name}"\nport = {port}\n\n[auth]\nmode = "any"\n'
        )
        (fakes[name].root / "fauxcall.toml").write_text(config)
    result = scaffold(fakes["gamma"], workspace, named("gamma"), project_root=tmp_path)
    ports = {
        name: tomllib.loads((fake.root / "fauxcall.toml").read_text())["port"]
        for name, fake in fakes.items()
    }
    assert ports == {"alpha": 8701, "beta": 8703, "gamma": 8702}  # first by name keeps 8701
    beta = (fakes["beta"].root / "fauxcall.toml").read_text()
    assert beta.startswith("# copied from elsewhere\n")  # only the port line changed
    assert '[auth]\nmode = "any"\n' in beta
    assert "beta: port 8701 was another fake's; it now uses 8703 (fauxcall.toml)" in result.summary(
        tmp_path
    )
    scaffold(fakes["beta"], workspace, named("beta"), project_root=tmp_path)
    assert (
        "| [`beta`](beta/README.md) | Demo | 8703 |" in (workspace.root / "README.md").read_text()
    )


def test_the_fake_being_scaffolded_yields_its_port(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    newcomer = write_fake(workspace, named("alpha"))  # sorts first, but arrives last
    settled = write_fake(workspace, named("beta"))
    (newcomer.root / "fauxcall.toml").write_text("port = 8701\n")
    (settled.root / "fauxcall.toml").write_text("port = 8701\n")
    result = scaffold(newcomer, workspace, named("alpha"))
    assert (settled.root / "fauxcall.toml").read_text() == "port = 8701\n"
    assert (newcomer.root / "fauxcall.toml").read_text() == "port = 8702\n"
    assert "alpha: port 8701 was another fake's; it now uses 8702 (fauxcall.toml)" in result.notes


def test_a_port_clash_without_a_port_line_is_reported(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    first = write_fake(workspace, named("first"))
    second = write_fake(workspace, named("second"))
    (first.root / "fauxcall.toml").write_text("port = 0x21FD\n")  # 8701, spelled oddly
    (second.root / "fauxcall.toml").write_text("port = 8701\n")
    result = scaffold(first, workspace, named("first"))
    assert "first: port 8701 is another fake's too: change `port` in fauxcall.toml" in result.notes
    assert (first.root / "fauxcall.toml").read_text() == "port = 0x21FD\n"


def test_ports_members_and_listings_across_fakes(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    first = write_fake(workspace, named("first", "First | API"))
    scaffold(first, workspace, named("first", "First | API"), project_root=tmp_path)
    second = write_fake(workspace, named("second"))
    scaffold(second, workspace, named("second"), project_root=tmp_path)
    write_fake(workspace, named("pending"))  # initialized, never scaffolded
    scaffold(first, workspace, named("first", "First | API"), project_root=tmp_path)
    assert tomllib.loads((first.root / "fauxcall.toml").read_text())["port"] == FIRST_PORT
    assert tomllib.loads((second.root / "fauxcall.toml").read_text())["port"] == FIRST_PORT + 1
    members = tomllib.loads((workspace.root / "pyproject.toml").read_text())["tool"]["uv"][
        "workspace"
    ]["members"]
    assert members == ["first", "second"]
    readme = (workspace.root / "README.md").read_text()
    assert "| [`first`](first/README.md) | First \\| API | 8701 |" in readme
    assert "| [`second`](second/README.md) | Demo | 8702 |" in readme
    assert "pending" not in readme
    compose = yaml.safe_load((workspace.root / "compose.yaml").read_text())
    assert compose["services"]["first"]["ports"] == ["8701:8701"]
    assert compose["services"]["second"]["build"] == {
        "context": ".",
        "dockerfile": "second/Dockerfile",
    }
    assert compose["services"]["second"]["environment"] == {
        "FAUXCALL_HOST": "0.0.0.0",  # noqa: S104 - inside the container
        "FAUXCALL_PORT": "8702",
    }
    assert compose["services"]["second"]["env_file"] == [{"path": "second/.env", "required": False}]
    workflow = yaml.safe_load((tmp_path / ".github" / "workflows" / "fauxcall.yml").read_text())
    job = workflow["jobs"]["fake"]
    assert job["strategy"]["matrix"]["fake"] == ["first", "second"]
    assert job["defaults"]["run"]["working-directory"] == "fakes/${{ matrix.fake }}"
    assert job["steps"][1]["with"] == {"python-version": "3.12"}
    assert [step.get("run") for step in job["steps"][2:]] == [
        "uv sync --locked --all-groups",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy",
        "uv run pytest --cov --cov-report=term-missing",
    ]
    (second.root / "fauxcall.toml").write_text('name = "second"\nport = 9100\n')
    third = write_fake(workspace, named("third"))
    scaffold(third, workspace, named("third"))
    assert tomllib.loads((third.root / "fauxcall.toml").read_text())["port"] == FIRST_PORT + 1
    assert "| 9100 |" in (workspace.root / "README.md").read_text()


def test_an_unreadable_model_still_lists_the_fake(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    broken = write_fake(workspace, named("broken"))
    scaffold(broken, workspace, named("broken"))
    broken.model_path.write_text("{not json")
    other = write_fake(workspace, named("other"))
    scaffold(other, workspace, named("other"))
    assert (
        "| [`broken`](broken/README.md) | broken | 8701 |"
        in (workspace.root / "README.md").read_text()
    )


def test_no_project_root_means_no_workflow(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout = write_fake(workspace, named("demo"))
    result = scaffold(layout, workspace, named("demo"))
    assert not any(path.name == "fauxcall.yml" for path in result.written)
    assert not (tmp_path / ".github").exists()
    pyproject = tomllib.loads((layout.root / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == [fauxcall_requirement(None)[0]]
    assert pyproject["dependency-groups"]["dev"][0] == with_extra(
        fauxcall_requirement(None)[0], "testing"
    )
    assert (
        "sources"
        not in tomllib.loads((workspace.root / "pyproject.toml").read_text())["tool"]["uv"]
    )


def test_a_fake_without_endpoints_has_a_bare_router(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    model = model_with()
    layout = write_fake(workspace, model)
    scaffold(layout, workspace, model)
    routes = (layout.src / "routes.py").read_text()
    assert routes.endswith("from fastapi import APIRouter\n\nrouter = APIRouter()\n")
    scenarios = (layout.tests / "e2e" / "test_scenarios.py").read_text()
    assert scenarios.endswith('"""\n\nfrom __future__ import annotations\n')


def build_mcp_fake(workspace: Workspace, server_dir: Path) -> tuple[FakeLayout, ScaffoldResult]:
    """``init`` against an in-process MCP server, record two tool calls, then scaffold."""
    app, _ = mcp_app(server_dir, mcp_model())

    async def build() -> tuple[FakeLayout, list[Exchange], ApiModel]:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            result = await init_fake(
                "https://mcp.deepwiki.test/mcp",
                workspace=workspace,
                http=http,
                name="deepwiki",
                force_mcp=True,
                rps=10_000,
            )
            layout = result.layout
            privacy = RecordingPrivacy(
                PrivacyConfig(),
                secret_values=[],
                pseudonym_key=env_value(layout, "FAUXCALL_PSEUDONYM_KEY"),
            )
            question = 'How do "hooks" work? It\'s a ' + "long question " * 8
            calls: list[tuple[str, JsonValue]] = [
                ("ask_question", {"repoName": "facebook/react", "question": question}),
                ("read_wiki_structure", {"repoName": "facebook/react"}),
            ]
            plan = RecordingPlan(
                name="tools",
                created_at=NOW,
                calls=[
                    PlannedCall(
                        id=f"call-{index}",
                        endpoint=f"MCP tools/call:{tool}",
                        mutating=False,
                        request=PlannedRequest(
                            method=HttpMethod.POST,
                            path="/mcp",
                            json_body={"name": tool, "arguments": arguments},
                        ),
                    )
                    for index, (tool, arguments) in enumerate(calls)
                ],
            )
            recorder = McpRecorder(
                RecordingStore(layout.recordings),
                "https://mcp.deepwiki.test/mcp",
                http,
                privacy=privacy,
                rps=10_000,
            )
            definitions = {tool.name: tool.definition for tool in result.model.mcp.tools}  # type: ignore[union-attr]
            await recorder.run(plan, definitions)
        exchanges = [e for _, e in RecordingStore(layout.recordings).iter_exchanges()]
        return layout, exchanges, result.model

    layout, exchanges, model = asyncio.run(build())
    return layout, scaffold(
        layout,
        workspace,
        model,
        exchanges=exchanges,
        project_root=workspace.root.parent,
        source=str(REPO),
    )


def test_mcp_fake(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "fakes")
    layout, _ = build_mcp_fake(workspace, tmp_path / "server")
    app = (layout.src / "app.py").read_text()
    assert "from fauxcall.runtime import create_app, register_tool" in app
    assert "for name, handler in TOOL_HANDLERS.items():" in app
    assert (layout.src / "routes.py").read_text().endswith("router = APIRouter()\n")
    assert (
        "test_custom_tool_handlers_are_registered"
        in (layout.tests / "unit" / "test_app.py").read_text()
    )
    scenarios = (layout.tests / "e2e" / "test_scenarios.py").read_text()
    assert (
        "async def test_recorded_mcp_tools_call_ask_question(mcp: McpClient) -> None:" in scenarios
    )
    assert (
        "async def test_recorded_mcp_tools_call_read_wiki_structure(mcp: McpClient) -> None:"
        in scenarios
    )
    readme = (layout.root / "README.md").read_text()
    assert "Point your agent's MCP client at `http://127.0.0.1:8701/mcp`" in readme
    assert '"url": "http://127.0.0.1:8701/mcp"' in readme
    assert "claude mcp add --transport http deepwiki http://127.0.0.1:8701/mcp`." in readme
    assert "Tools: 3 · resources: 0 · prompts: 0." in readme
    assert "Endpoints:" not in readme
    assert "data/world/" not in readme  # no collections, no world
    assert "routes.py" not in readme  # an MCP fake has no REST routes
    assert "refuses calls without credentials" not in readme  # DeepWiki needs none


@dataclass(frozen=True, slots=True)
class Fakes:
    """A workspace with the tricky (auth-protected) REST fake and an MCP fake, built once."""

    project: Path
    workspace: Workspace
    rest: FakeLayout
    mcp: FakeLayout


@pytest.fixture(scope="module")
def fakes(tmp_path_factory: pytest.TempPathFactory) -> Fakes:
    project = tmp_path_factory.mktemp("project")
    workspace = Workspace(project / "fakes")
    rest, _ = scaffold_tricky(workspace, project, auth=True, source=str(REPO))
    mcp, _ = build_mcp_fake(workspace, project / "server")
    return Fakes(project, workspace, rest, mcp)


def run(
    command: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed commands on generated files
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=timeout
    )


@pytest.mark.skipif(not RUFF.exists(), reason="ruff is not installed next to the interpreter")
@pytest.mark.parametrize("which", ["rest", "mcp"])
def test_generated_code_is_ruff_clean(fakes: Fakes, which: str) -> None:
    root = getattr(fakes, which).root
    checked = run([str(RUFF), "check", "--no-cache", "--output-format=concise", "."], root)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    formatted = run([str(RUFF), "format", "--no-cache", "--check", "--diff", "."], root)
    assert formatted.returncode == 0, formatted.stdout + formatted.stderr


LEAKY: frozenset[str] = frozenset(
    {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME", "COVERAGE_FILE"}
)


def subprocess_env(workspace: Workspace) -> dict[str, str]:
    """The environment for ``uv`` in the generated workspace: offline, this Python, no leaks."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in LEAKY and not key.startswith(("FAUXCALL_", "COV_CORE", "COVERAGE_"))
    }
    if not os.environ.get("CI"):  # a CI runner starts with an empty cache
        env["UV_OFFLINE"] = "1"
    env.update(
        UV_PYTHON=f"{sys.version_info.major}.{sys.version_info.minor}",
        MYPY_CACHE_DIR=str(workspace.root / ".mypy_cache"),
    )
    return env


def _skip_unless_ci(reason: str) -> NoReturn:
    """Skip locally; in CI (``CI`` is set, as on GitHub Actions) a skipped gate is a failure."""
    if os.environ.get("CI"):
        pytest.fail(f"cannot check the generated fakes in CI: {reason}")
    pytest.skip(reason)


@pytest.mark.slow
def test_generated_fakes_pass_their_own_gates(fakes: Fakes) -> None:
    uv = shutil.which("uv")
    if uv is None:
        _skip_unless_ci("uv is not on PATH")
    env = subprocess_env(fakes.workspace)
    synced = run([uv, "sync", "--all-packages", "--quiet"], fakes.workspace.root, env)
    if synced.returncode != 0 and "network" in (synced.stdout + synced.stderr).lower():
        _skip_unless_ci(f"uv cannot install offline (cold cache): {synced.stderr.strip()[-300:]}")
    assert synced.returncode == 0, synced.stdout + synced.stderr
    for fake in (fakes.rest, fakes.mcp):
        outputs = []
        for command in (
            ["ruff", "check", "--no-cache", "."],
            ["ruff", "format", "--no-cache", "--check", "."],
            ["mypy"],
            ["pytest", "-p", "no:cacheprovider", "-q", "--cov", "--cov-report=term-missing"],
        ):
            done = run([uv, "run", "--no-sync", *command], fake.root, env)
            report = f"{fake.name}: {' '.join(command)}\n{done.stdout}\n{done.stderr}"
            assert done.returncode == 0, report
            outputs.append(done.stdout)
        assert "Required test coverage of 95.0% reached" in outputs[-1]
        assert " failed" not in outputs[-1]


def test_a_checkout_is_referenced_relatively_from_inside(tmp_path: Path) -> None:
    checkout = tmp_path / "fauxcall-repo"
    inside = checkout / "examples" / "fakes"
    assert scaffold_module.checkout_reference(str(checkout), inside) == "../.."
    elsewhere = tmp_path / "project" / "fakes"
    assert scaffold_module.checkout_reference(str(checkout), elsewhere) == str(checkout)
    assert scaffold_module.checkout_reference(None, inside) is None
