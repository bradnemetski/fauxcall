"""Recording real APIs and MCP servers into diffable evidence (``data/recordings``)."""

from fauxcall.recording.mcp import McpIntrospection, McpRecorder, tool_is_mutating
from fauxcall.recording.recorder import (
    CallOutcome,
    NoPrivacy,
    PrivacyFilter,
    RealTarget,
    Recorder,
    RunReport,
    classify_body,
)
from fauxcall.recording.store import RecordingStore

__all__ = [
    "CallOutcome",
    "McpIntrospection",
    "McpRecorder",
    "NoPrivacy",
    "PrivacyFilter",
    "RealTarget",
    "Recorder",
    "RecordingStore",
    "RunReport",
    "classify_body",
    "tool_is_mutating",
]
