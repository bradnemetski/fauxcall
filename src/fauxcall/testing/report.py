"""Compliance reporting: per scenario, per endpoint, overall — and the ≥98% gate."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from fauxcall.storage import atomic_write_text, write_json

if TYPE_CHECKING:
    from pathlib import Path

    from fauxcall.model.api import ApiModel
    from fauxcall.model.endpoints import EndpointModel
    from fauxcall.testing.compare import Comparison
    from fauxcall.types import JsonValue

_PARAM: Final = re.compile(r"\{[^{}/]+\}")
_REFUSED: Final = frozenset({401, 403})
_ESCAPED_PARAM: Final = re.compile(r"\\\{[^/]+?\\\}")


def specificity(endpoint: EndpointModel) -> tuple[int, int, str]:
    """Sort key: more literal characters first, then fewer parameters (the real routers' rule)."""
    literal = len(_PARAM.sub("", endpoint.path))
    return (-literal, len(_PARAM.findall(endpoint.path)), endpoint.path)


def endpoint_matcher(model: ApiModel | None) -> list[tuple[str, re.Pattern[str]]]:
    """Regexes mapping concrete ``METHOD /path`` labels to endpoint ids, most specific first.

    ``/pet/findByStatus`` is tried before ``/pet/{petId}`` so a literal route wins, like on the
    real server. Paths match case-insensitively when the model's routing says so.
    """
    if model is None:
        return []
    flags = re.IGNORECASE if model.routing.case_insensitive else 0
    patterns = []
    for endpoint in sorted(model.endpoints, key=specificity):
        regex = _ESCAPED_PARAM.sub("[^/]+", re.escape(endpoint.path))
        patterns.append((endpoint.id, re.compile(f"^{endpoint.method.value} {regex}/?$", flags)))
    return patterns


def endpoint_for(label: str, matchers: list[tuple[str, re.Pattern[str]]]) -> str:
    """The endpoint a call belongs to (the label without its query when nothing matches).

    Absolute URLs (``GET https://api.example.com/pets/1``) are reduced to their path first. MCP
    labels (``MCP tools/call:ask``, ``MCP resources/read:file:///a?b``) already are endpoint ids.
    """
    if label.startswith("MCP "):
        return label
    concrete = label.split("?", 1)[0]
    method, _, target = concrete.partition(" ")
    if "://" in target:
        concrete = f"{method} {urlsplit(target).path or '/'}"
    for endpoint_id, pattern in matchers:
        if pattern.match(concrete):
            return endpoint_id
    return concrete


def _percent(value: float) -> str:
    """``0.98`` -> ``98%``, ``0.975`` -> ``97.5%`` (no misleading rounding of thresholds)."""
    return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


@dataclass(slots=True)
class ParityReport:
    """Everything compared in one test session."""

    threshold: float
    scenarios: dict[str, list[Comparison]] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)

    def add(self, scenario: str, comparisons: list[Comparison]) -> None:
        """Add one scenario's comparisons."""
        self.scenarios[scenario] = comparisons

    @property
    def passed(self) -> float:
        """Passed weight across everything."""
        return sum(c.passed for cs in self.scenarios.values() for c in cs)

    @property
    def total(self) -> float:
        """Total weight across everything."""
        return sum(c.total for cs in self.scenarios.values() for c in cs)

    @property
    def score(self) -> float:
        """Overall compliance."""
        return 1.0 if self.total == 0 else self.passed / self.total

    def _independent(self) -> list[Comparison]:
        return [
            c for cs in self.scenarios.values() for c in cs if not c.replayed and not c.shape_only
        ]

    @property
    def independent_score(self) -> float:
        """Compliance without the calls that repeat recorded requests or compare shape only.

        How the fake does on requests it has never seen, values included.
        """
        kept = self._independent()
        total = sum(c.total for c in kept)
        return 1.0 if total == 0 else sum(c.passed for c in kept) / total

    def call_counts(self) -> dict[str, int]:
        """Calls compared, and how many repeat a recording or compare shape only."""
        calls = [c for cs in self.scenarios.values() for c in cs]
        return {
            "compared": len(calls),
            "replayed": sum(c.replayed for c in calls),
            "shape_only": sum(c.shape_only and not c.replayed for c in calls),
            "independent": len(self._independent()),
        }

    def not_covered(self, model: ApiModel | None) -> list[str]:
        """Endpoints, tools and MCP listings no scenario compared."""
        if model is None:
            return []
        expected = [e.id for e in model.endpoints if e.role != "websocket"]
        if model.mcp is not None:
            expected.append("MCP tools/list")
            if model.mcp.resources:
                expected.append("MCP resources/list")
            if model.mcp.prompts:
                expected.append("MCP prompts/list")
            expected += [f"MCP tools/call:{tool.name}" for tool in model.mcp.tools]
        covered = set(self.by_endpoint(model))
        return [endpoint for endpoint in expected if endpoint not in covered]

    def refusal_checked(self, model: ApiModel | None) -> bool | None:
        """Whether a scenario compared a refused credential (``None``: the API takes none)."""
        if model is None or model.auth is None or not model.auth.schemes:
            return None
        return any(c.real_status in _REFUSED for cs in self.scenarios.values() for c in cs)

    @property
    def ok(self) -> bool:
        """Whether the gate passes (every scenario ran on both sides, score at the threshold)."""
        return self.score >= self.threshold and not self.unmatched

    def by_endpoint(self, model: ApiModel | None) -> dict[str, tuple[float, float]]:
        """``endpoint -> (passed, total)``."""
        matchers = endpoint_matcher(model)
        totals: defaultdict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for comparisons in self.scenarios.values():
            for comparison in comparisons:
                key = endpoint_for(comparison.label, matchers)
                totals[key][0] += comparison.passed
                totals[key][1] += comparison.total
        return {key: (value[0], value[1]) for key, value in sorted(totals.items())}

    def to_json(self, model: ApiModel | None) -> JsonValue:
        """Machine-readable report."""
        return {
            "score": round(self.score, 5),
            "independent_score": round(self.independent_score, 5),
            "calls": dict(self.call_counts()),
            "not_covered": [str(endpoint) for endpoint in self.not_covered(model)],
            "refusal_checked": self.refusal_checked(model),
            "threshold": self.threshold,
            "ok": self.ok,
            "unmatched_scenarios": list(self.unmatched),
            "endpoints": {
                key: {
                    "passed": round(p, 3),
                    "total": round(t, 3),
                    "score": round(p / t if t else 1.0, 5),
                }
                for key, (p, t) in self.by_endpoint(model).items()
            },
            "scenarios": {
                name: [
                    {
                        "call": c.label,
                        "score": round(c.score, 5),
                        "failures": [
                            {"check": f.name, "weight": f.weight, "detail": f.detail}
                            for f in c.failures()
                        ],
                    }
                    for c in comparisons
                ]
                for name, comparisons in self.scenarios.items()
            },
        }

    def to_markdown(self, model: ApiModel | None) -> str:
        """Human-readable report."""
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            f"# Parity report — {verdict}",
            "",
            (
                f"Overall compliance **{_percent(self.score)}** (threshold "
                f"{_percent(self.threshold)}) across {len(self.scenarios)} scenarios."
            ),
            "",
            *self._honesty(model),
            "| endpoint | compliance | weight |",
            "|---|---|---|",
        ]
        for key, (passed, total) in self.by_endpoint(model).items():
            score = passed / total if total else 1.0
            lines.append(f"| `{_cell(key)}` | {score:.2%} | {passed:.1f}/{total:.1f} |")
        failing = [
            (name, comparison, failure)
            for name, comparisons in self.scenarios.items()
            for comparison in comparisons
            for failure in comparison.failures()
        ]
        if failing:
            lines += [
                "",
                "## Failed checks",
                "",
                "| scenario | call | check | detail |",
                "|---|---|---|---|",
            ]
            for name, comparison, failure in failing[:200]:
                detail = _cell(failure.detail)[:160]
                lines.append(
                    f"| {_cell(name)} | `{_cell(comparison.label)}` | {_cell(failure.name)} "
                    f"| {detail} |"
                )
        if self.unmatched:
            lines += ["", "## Scenarios that ran on only one side", ""]
            lines += [f"- {name}" for name in self.unmatched]
        return "\n".join(lines) + "\n"

    def _honesty(self, model: ApiModel | None) -> list[str]:
        """What the score rests on: independent calls, coverage, refused credentials."""
        counts = self.call_counts()
        lines = []
        if counts["compared"] and counts["independent"] < counts["compared"]:
            summary = (
                f"Independent compliance **{_percent(self.independent_score)}** over "
                f"{counts['independent']} of {counts['compared']} calls. The others repeat "
                f"recorded requests ({counts['replayed']}), which the fake answers from those "
                f"recordings, or compare shape only ({counts['shape_only']})."
            )
            lines += [summary, ""]
        missing = self.not_covered(model)
        if missing:
            lines += [
                "Not covered by any scenario: " + ", ".join(f"`{_cell(m)}`" for m in missing) + ".",
                "",
            ]
        if self.refusal_checked(model) is False:
            lines += [
                "No scenario checks a refused credential (a 401 or 403): add one per way to fail.",
                "",
            ]
        return lines

    def write(self, directory: Path, model: ApiModel | None) -> tuple[Path, Path]:
        """Write ``parity.json`` and ``parity.md``."""
        json_path, md_path = directory / "parity.json", directory / "parity.md"
        write_json(json_path, self.to_json(model))
        atomic_write_text(md_path, self.to_markdown(model))
        return json_path, md_path
