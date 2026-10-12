from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from fauxcall.model.api import ApiModel, RealTarget, RoutingModel, SourceInfo
from fauxcall.model.auth import AuthModel, AuthScheme
from fauxcall.model.endpoints import EndpointModel, StreamModel
from fauxcall.model.mcp import McpModel, McpToolModel
from fauxcall.testing.compare import Comparison
from fauxcall.testing.report import (
    ParityReport,
    endpoint_for,
    endpoint_matcher,
    specificity,
)
from fauxcall.types import HttpMethod

if TYPE_CHECKING:
    from pathlib import Path


def endpoint(method: str, path: str) -> EndpointModel:
    return EndpointModel(id=f"{method} {path}", method=HttpMethod(method), path=path)


def api_model(*endpoints: EndpointModel, case_insensitive: bool = False) -> ApiModel:
    return ApiModel(
        name="petstore",
        title="Petstore",
        source=SourceInfo(
            kind="openapi",
            url="https://petstore.example.com/openapi.json",
            fetched_at=datetime(2026, 10, 10, tzinfo=UTC),
        ),
        real=RealTarget(base_url="https://petstore.example.com"),
        routing=RoutingModel(case_insensitive=case_insensitive),
        endpoints=list(endpoints),
    )


PETSTORE = api_model(
    endpoint("GET", "/api/v3/pet/{petId}"),
    endpoint("GET", "/api/v3/pet/findByStatus"),
    endpoint("DELETE", "/api/v3/pet/{petId}"),
    endpoint("GET", "/api/v3/user/{user-name}"),
)
FRANKFURTER = api_model(
    endpoint("GET", "/v1/{date}"),
    endpoint("GET", "/v1/{start_date}..{end_date}"),
    endpoint("GET", "/v1/latest"),
)


def comparison(label: str, *results: tuple[float, bool]) -> Comparison:
    made = Comparison(label)
    for index, (weight, passed) in enumerate(results):
        made.add(f"check {index}", weight, passed, f"detail {index} | piped")
    return made


def test_specificity_prefers_literal_paths() -> None:
    ordered = sorted(FRANKFURTER.endpoints, key=specificity)
    assert [e.path for e in ordered] == [
        "/v1/latest",
        "/v1/{start_date}..{end_date}",
        "/v1/{date}",
    ]


def test_no_model_no_matchers() -> None:
    assert endpoint_matcher(None) == []
    assert endpoint_for("GET /pets/1?x=1", []) == "GET /pets/1"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("GET /api/v3/pet/42", "GET /api/v3/pet/{petId}"),
        ("GET /api/v3/pet/42/", "GET /api/v3/pet/{petId}"),
        ("GET /api/v3/pet/findByStatus?status=sold", "GET /api/v3/pet/findByStatus"),
        ("DELETE /api/v3/pet/7", "DELETE /api/v3/pet/{petId}"),
        ("GET /api/v3/user/jane.doe", "GET /api/v3/user/{user-name}"),
        ("GET https://petstore.example.com/api/v3/pet/1?x=y", "GET /api/v3/pet/{petId}"),
        ("GET https://petstore.example.com", "GET /"),
        ("POST /api/v3/pet/1", "POST /api/v3/pet/1"),
        ("GET /API/V3/PET/1", "GET /API/V3/PET/1"),
        ("MCP tools/call:ask", "MCP tools/call:ask"),
        ("MCP resources/read:file:///tmp/a?b=1", "MCP resources/read:file:///tmp/a?b=1"),
    ],
)
def test_labels_map_to_endpoints(label: str, expected: str) -> None:
    assert endpoint_for(label, endpoint_matcher(PETSTORE)) == expected


def test_literal_routes_win_whatever_the_declaration_order() -> None:
    matchers = endpoint_matcher(FRANKFURTER)
    assert endpoint_for("GET /v1/latest", matchers) == "GET /v1/latest"
    assert endpoint_for("GET /v1/2026-01-01..2026-01-31", matchers) == (
        "GET /v1/{start_date}..{end_date}"
    )
    assert endpoint_for("GET /v1/2026-01-01", matchers) == "GET /v1/{date}"


def test_case_insensitive_routing() -> None:
    model = api_model(endpoint("GET", "/api/v1/Books/{id}"), case_insensitive=True)
    assert endpoint_for("GET /api/v1/books/1", endpoint_matcher(model)) == "GET /api/v1/Books/{id}"
    strict = api_model(endpoint("GET", "/api/v1/Books/{id}"))
    assert endpoint_for("GET /api/v1/books/1", endpoint_matcher(strict)) == "GET /api/v1/books/1"


def test_empty_report_passes() -> None:
    report = ParityReport(threshold=0.98)
    assert report.score == 1.0
    assert report.total == 0
    assert report.ok
    assert report.by_endpoint(None) == {}


def test_scores_and_threshold() -> None:
    report = ParityReport(threshold=0.9)
    report.add("tests/e2e/test_pets.py::test_get", [comparison("GET /api/v3/pet/1", (9, True))])
    report.add(
        "tests/e2e/test_pets.py::test_find",
        [comparison("GET /api/v3/pet/findByStatus?status=sold", (1, False), (4, True))],
    )
    assert report.passed == 13
    assert report.total == 14
    assert report.score == pytest.approx(13 / 14)
    assert report.ok
    assert not ParityReport(threshold=0.95, scenarios=dict(report.scenarios)).ok
    report.add("tests/e2e/test_pets.py::test_find", [comparison("GET /x", (1, True))])
    assert report.score == 1.0  # a scenario added again replaces its comparisons
    unmatched = ParityReport(threshold=0.9, scenarios=dict(report.scenarios), unmatched=["only"])
    assert not unmatched.ok


def test_by_endpoint_groups_calls() -> None:
    report = ParityReport(threshold=0.98)
    report.add(
        "s1",
        [
            comparison("GET /api/v3/pet/1", (5, True), (1, False)),
            comparison("GET /api/v3/pet/2", (5, True)),
            comparison("MCP tools/call:ask", (2, True)),
        ],
    )
    assert report.by_endpoint(PETSTORE) == {
        "GET /api/v3/pet/{petId}": (10.0, 11.0),
        "MCP tools/call:ask": (2.0, 2.0),
    }
    assert report.by_endpoint(None) == {
        "GET /api/v3/pet/1": (5.0, 6.0),
        "GET /api/v3/pet/2": (5.0, 5.0),
        "MCP tools/call:ask": (2.0, 2.0),
    }


def _sample_report() -> ParityReport:
    report = ParityReport(threshold=0.975)
    report.add(
        "tests/e2e/test_pets.py::test_get[a|b]",
        [comparison("GET /api/v3/pet/1", (5, True), (1, False)), Comparison("GET /empty")],
    )
    report.unmatched.append("tests/e2e/test_pets.py::test_only_fake")
    return report


def test_json_report() -> None:
    data: Any = _sample_report().to_json(PETSTORE)
    assert data["score"] == round(5 / 6, 5)
    assert data["threshold"] == 0.975
    assert data["ok"] is False
    assert data["unmatched_scenarios"] == ["tests/e2e/test_pets.py::test_only_fake"]
    assert data["endpoints"] == {
        "GET /api/v3/pet/{petId}": {"passed": 5.0, "total": 6.0, "score": round(5 / 6, 5)},
        "GET /empty": {"passed": 0.0, "total": 0.0, "score": 1.0},
    }
    calls = data["scenarios"]["tests/e2e/test_pets.py::test_get[a|b]"]
    assert calls[0] == {
        "call": "GET /api/v3/pet/1",
        "score": round(5 / 6, 5),
        "failures": [{"check": "check 1", "weight": 1, "detail": "detail 1 | piped"}],
    }
    assert calls[1] == {"call": "GET /empty", "score": 1.0, "failures": []}


def test_markdown_report() -> None:
    text = _sample_report().to_markdown(PETSTORE)
    assert text.startswith("# Parity report — FAIL\n")
    assert "Overall compliance **83.33%** (threshold 97.5%) across 1 scenarios." in text
    assert "| `GET /api/v3/pet/{petId}` | 83.33% | 5.0/6.0 |" in text
    assert "| `GET /empty` | 100.00% | 0.0/0.0 |" in text
    assert "## Failed checks" in text
    assert (
        "| tests/e2e/test_pets.py::test_get[a\\|b] | `GET /api/v3/pet/1` | check 1 "
        "| detail 1 \\| piped |"
    ) in text
    assert text.endswith(
        "## Scenarios that ran on only one side\n\n- tests/e2e/test_pets.py::test_only_fake\n"
    )


def test_markdown_report_when_everything_passes() -> None:
    report = ParityReport(threshold=0.98)
    report.add("s", [comparison("GET /x", (1, True))])
    text = report.to_markdown(None)
    assert text.startswith("# Parity report — PASS\n")
    assert "(threshold 98%)" in text
    assert "Failed checks" not in text
    assert "only one side" not in text


def test_long_details_are_cut_in_markdown() -> None:
    report = ParityReport(threshold=0.98)
    failing = Comparison("GET /x")
    failing.add("shape", 3.0, passed=False, detail="x" * 500)
    report.add("s", [failing])
    row = next(line for line in report.to_markdown(None).splitlines() if "| shape |" in line)
    assert row.endswith("| " + "x" * 160 + " |")


def test_write(tmp_path: Path) -> None:
    json_path, md_path = _sample_report().write(tmp_path / "reports", PETSTORE)
    assert json_path == tmp_path / "reports" / "parity.json"
    assert md_path == tmp_path / "reports" / "parity.md"
    assert json.loads(json_path.read_text())["ok"] is False
    assert md_path.read_text().startswith("# Parity report — FAIL")


def marked(
    label: str,
    *results: tuple[float, bool],
    replayed: bool = False,
    shape_only: bool = False,
    status: int | None = 200,
) -> Comparison:
    made = comparison(label, *results)
    made.replayed, made.shape_only, made.real_status = replayed, shape_only, status
    return made


def test_the_independent_score_leaves_out_replays_and_shape_only_calls() -> None:
    report = ParityReport(threshold=0.98)
    report.add(
        "s",
        [
            marked("GET /api/v3/pet/1", (5, True), replayed=True),
            marked("GET /api/v3/pet/2", (4, True), (1, False)),
            marked("GET /api/v3/pet/findByStatus", (5, True), shape_only=True),
            marked("GET /api/v3/pet/3", (5, True), replayed=True, shape_only=True),
        ],
    )
    assert report.independent_score == 0.8  # only pet/2 counts
    assert report.call_counts() == {
        "compared": 4,
        "replayed": 2,
        "shape_only": 1,  # a replayed call counts as replayed only
        "independent": 1,
    }
    markdown = report.to_markdown(PETSTORE)
    assert (
        "Independent compliance **80%** over 1 of 4 calls. The others repeat recorded requests "
        "(2), which the fake answers from those recordings, or compare shape only (1)."
    ) in markdown
    data = report.to_json(PETSTORE)
    assert isinstance(data, dict)
    assert (data["independent_score"], data["calls"]) == (0.8, report.call_counts())
    nothing = ParityReport(threshold=0.98)
    nothing.add("s", [marked("GET /x", (1, True), replayed=True)])
    assert nothing.independent_score == 1.0
    assert "Independent compliance" not in ParityReport(threshold=0.98).to_markdown(None)


def test_coverage_gaps_and_refused_credentials() -> None:
    report = ParityReport(threshold=0.98)
    report.add("s", [marked("GET /api/v3/pet/1", (1, True))])
    assert report.not_covered(PETSTORE) == [
        "GET /api/v3/pet/findByStatus",
        "DELETE /api/v3/pet/{petId}",
        "GET /api/v3/user/{user-name}",
    ]
    assert report.not_covered(None) == []
    assert report.refusal_checked(PETSTORE) is None  # it takes no credentials
    locked = PETSTORE.model_copy(update={"auth": AuthModel(schemes=[AuthScheme(kind="bearer")])})
    assert report.refusal_checked(locked) is False
    markdown = report.to_markdown(locked)
    assert "Not covered by any scenario: `GET /api/v3/pet/findByStatus`," in markdown
    assert "No scenario checks a refused credential (a 401 or 403)" in markdown
    report.add("refused", [marked("GET /api/v3/pet/1", (1, True), status=401)])
    assert report.refusal_checked(locked) is True
    data = report.to_json(locked)
    assert isinstance(data, dict)
    assert data["refusal_checked"] is True
    assert data["not_covered"] == report.not_covered(locked)


def test_mcp_coverage_expects_listings_and_every_tool() -> None:
    model = PETSTORE.model_copy(
        update={
            "endpoints": [],
            "kind": "mcp",
            "mcp": McpModel(
                tools=[
                    McpToolModel(definition={"name": "a"}),
                    McpToolModel(definition={"name": "b"}),
                ],
                resources=[{"uri": "x://1"}],
                prompts=[{"name": "p"}],
            ),
        }
    )
    report = ParityReport(threshold=0.98)
    report.add("s", [marked("MCP tools/call:a", (1, True)), marked("MCP tools/list", (1, True))])
    assert report.not_covered(model) == [
        "MCP resources/list",
        "MCP prompts/list",
        "MCP tools/call:b",
    ]
    websocket = api_model(
        EndpointModel(
            id="GET /ws",
            method=HttpMethod.GET,
            path="/ws",
            role="websocket",
            stream=StreamModel(kind="websocket"),
        )
    )
    assert report.not_covered(websocket) == []  # streams are compared elsewhere


def test_resource_reads_group_under_their_template() -> None:
    model = PETSTORE.model_copy(
        update={
            "endpoints": [],
            "kind": "mcp",
            "mcp": McpModel(
                resource_templates=[
                    {"uriTemplate": "demo://items/{id}/text", "name": "item"},
                    {"uriTemplate": "demo://static", "name": "plain"},
                    "odd",
                ]
            ),
        }
    )
    matchers = endpoint_matcher(model)
    assert endpoint_for("MCP resources/read:demo://items/7/text", matchers) == (
        "MCP resources/read:demo://items/{id}/text"
    )
    assert endpoint_for("MCP resources/read:demo://other", matchers) == (
        "MCP resources/read:demo://other"
    )
    assert endpoint_for("MCP tools/call:ask", matchers) == "MCP tools/call:ask"
