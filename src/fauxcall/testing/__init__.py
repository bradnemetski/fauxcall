"""End-to-end parity testing: run scenarios against the real API and the fake, compare, report."""

from fauxcall.testing.client import ApiClient, CompareOptions, McpClient
from fauxcall.testing.compare import Comparison, Observation, compare
from fauxcall.testing.plugin import FakeUnderTest
from fauxcall.testing.report import ParityReport

__all__ = [
    "ApiClient",
    "CompareOptions",
    "Comparison",
    "FakeUnderTest",
    "McpClient",
    "Observation",
    "ParityReport",
    "compare",
]
