"""Handler behavior the recordings can't show on their own: other clients, other times, edges."""

from __future__ import annotations

import base64
import gzip
import math
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.runtime.app import runtime_of_app
from fauxcall.testing.asgi import streaming_client
from fauxcall.testing.support import settings_for_tests

from fauxcall_everything import tasks
from fauxcall_everything.app import build_app
from fauxcall_everything.handlers import CLIENT_REQUEST_TIMEOUT_S, decode_data_url, gzip_bytes
from fauxcall_everything.jsnum import (
    InvalidUrlError,
    js_number,
    js_string,
    local_time,
    normalize_url,
)
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

OCT_12_2026_03_31_23_UTC = 1_791_775_883_000_000_000


@asynccontextmanager
async def session(
    app: FastAPI, capabilities: dict[str, Any] | None = None
) -> AsyncIterator[McpWireClient]:
    """One MCP session with the fake, declaring ``capabilities``."""
    async with streaming_client(app) as client:
        wire = McpWireClient(str(client.base_url).rstrip("/") + "/mcp", client)
        await wire.initialize(capabilities=capabilities)
        try:
            yield wire
        finally:
            await wire.close()


def fake(**overrides: Any) -> FastAPI:
    return build_app(settings_for_tests(Settings, **overrides))


def freeze(app: FastAPI, at_ns: int) -> None:
    runtime_of_app(app).clocks.override("default").freeze(at_ns)


async def call(wire: McpWireClient, method: str, params: Any = None) -> dict[str, Any]:
    reply = (await wire.call(method, params)).response
    assert isinstance(reply, dict)
    return reply


def text_of(reply: dict[str, Any]) -> str:
    return str(reply["result"]["content"][0]["text"])


# -- tools that call back into the client --------------------------------------------------------


@pytest.mark.parametrize(
    ("capability", "tool", "arguments"),
    [
        ("sampling", "trigger-sampling-request", {"prompt": "Say hi"}),
        ("elicitation", "trigger-elicitation-request", {}),
    ],
)
async def test_capability_tools_time_out_for_clients_that_declare_them(
    capability: str, tool: str, arguments: dict[str, Any]
) -> None:
    app = fake()
    async with session(app, {capability: {}}) as wire:
        listed = await call(wire, "tools/list")
        assert tool in [t["name"] for t in listed["result"]["tools"]]
        reply = await call(wire, "tools/call", {"name": tool, "arguments": arguments})
    assert reply["result"] == {
        "content": [{"type": "text", "text": "MCP error -32001: Request timed out"}],
        "isError": True,
    }
    assert CLIENT_REQUEST_TIMEOUT_S in runtime_of_app(app).pauses


async def test_capability_tools_are_unknown_to_plain_clients() -> None:
    async with session(fake()) as wire:
        listed = await call(wire, "tools/list")
        reply = await call(wire, "tools/call", {"name": "get-roots-list", "arguments": {}})
    assert "get-roots-list" not in [t["name"] for t in listed["result"]["tools"]]
    assert text_of(reply) == "MCP error -32602: Tool get-roots-list not found"


async def test_sampling_request_validates_its_prompt() -> None:
    async with session(fake(), {"sampling": {}}) as wire:
        reply = await call(wire, "tools/call", {"name": "trigger-sampling-request"})
    assert text_of(reply).endswith(
        "trigger-sampling-request: Invalid input: expected string, received undefined at prompt"
    )


# -- tasks ---------------------------------------------------------------------------------------


async def test_task_lifecycle_follows_the_clock() -> None:
    app = fake()
    freeze(app, OCT_12_2026_03_31_23_UTC)
    async with session(app) as wire:
        created = await call(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "tides"}, "task": {}},
        )
        task_id = created["result"]["task"]["taskId"]
        assert created["result"]["task"]["ttl"] == tasks.TTL_MS
        seen = []
        for offset_s in (0.5, 1.5, 2.5, 3.5, 4.5):
            freeze(app, OCT_12_2026_03_31_23_UTC + int(offset_s * 1e9))
            got = (await call(wire, "tasks/get", {"taskId": task_id}))["result"]
            seen.append((got["status"], got["statusMessage"]))
        assert seen == [
            ("working", "Gathering sources..."),
            ("working", "Analyzing content..."),
            ("working", "Synthesizing findings..."),
            ("working", "Generating report..."),
            ("completed", "Generating report..."),
        ]
        assert got["createdAt"] == "2026-10-12T03:31:23.000Z"
        assert got["lastUpdatedAt"] == "2026-10-12T03:31:27.010Z"
        listed = await call(wire, "tasks/list", {})
        assert [t["taskId"] for t in listed["result"]["tasks"]] == [task_id]
        freeze(app, OCT_12_2026_03_31_23_UTC + 4_010_000_000 + 300_001_000_000)
        gone = await call(wire, "tasks/get", {"taskId": task_id})
        assert gone["error"]["message"].endswith("Failed to retrieve task: Task not found")
        assert (await call(wire, "tasks/list", {}))["result"] == {"tasks": [], "_meta": {}}


async def test_tasks_belong_to_their_session() -> None:
    app = fake()
    async with session(app) as first:
        created = await call(
            first,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "x"}, "task": {}},
        )
        task_id = created["result"]["task"]["taskId"]
        async with session(app) as second:
            other = await call(second, "tasks/get", {"taskId": task_id})
            assert other["error"]["code"] == -32602
            assert (await call(second, "tasks/list", {}))["result"]["tasks"] == []


async def test_result_of_a_task_on_a_frozen_clock_completes_it() -> None:
    app = fake(fast=False, latency_enabled=False)
    freeze(app, OCT_12_2026_03_31_23_UTC)
    runtime = runtime_of_app(app)
    async with session(app) as wire:  # really waits the 4 s the real server takes
        created = await call(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "ice"}, "task": {}},
        )
        task_id = created["result"]["task"]["taskId"]
        result = await call(wire, "tasks/result", {"taskId": task_id})
        assert result["result"]["content"][0]["text"].startswith("# Research Report: ice\n")
        status = (await call(wire, "tasks/get", {"taskId": task_id}))["result"]
    assert status["status"] == "completed"
    assert status["lastUpdatedAt"] == "2026-10-12T03:31:23.000Z"
    assert any(abs(p - 4.01) < 0.05 for p in runtime.pauses)


async def test_task_errors() -> None:
    async with session(fake()) as wire:
        assert (await call(wire, "tasks/get", {}))["error"]["code"] == -32603
        listed = await call(wire, "tasks/list", {"cursor": "abc"})
        cancel = await call(wire, "tasks/cancel", {"taskId": "nope"})
        result = await call(wire, "tasks/result", {"taskId": "nope"})
    assert listed["error"]["message"] == (
        "MCP error -32602: Failed to list tasks: Invalid cursor: abc"
    )
    assert cancel["error"]["message"] == "MCP error -32602: Task not found: nope"
    assert result["error"]["message"] == "MCP error -32602: Task not found: nope"


# -- gzip and session resources ------------------------------------------------------------------


async def test_gzip_resource_is_readable_in_its_session_only() -> None:
    app = fake()
    data = "data:text/plain;base64," + base64.b64encode(b"fauxcall").decode()
    async with session(app) as wire:
        made = await call(
            wire,
            "tools/call",
            {"name": "gzip-file-as-resource", "arguments": {"name": "f.gz", "data": data}},
        )
        assert made["result"]["content"][0]["uri"] == "demo://resource/session/f.gz"
        read = await call(wire, "resources/read", {"uri": "demo://resource/session/f.gz"})
        blob = read["result"]["contents"][0]["blob"]
        assert gzip.decompress(base64.b64decode(blob)) == b"fauxcall"
        async with session(app) as other:
            missing = await call(other, "resources/read", {"uri": "demo://resource/session/f.gz"})
    assert missing["error"]["code"] == -32602


async def test_gzip_of_an_unrecorded_host_fails_like_an_unreachable_one() -> None:
    async with session(fake()) as wire:
        reply = await call(
            wire,
            "tools/call",
            {
                "name": "gzip-file-as-resource",
                "arguments": {"name": "x.gz", "data": "https://unrecorded.example.org/x"},
            },
        )
    assert reply["result"] == {
        "content": [{"type": "text", "text": "fetch failed"}],
        "isError": True,
    }


def test_data_urls_decode_like_fetch() -> None:
    assert decode_data_url("data:,a%20b") == b"a b"
    assert decode_data_url("data:text/plain;BASE64,YWI=") == b"ab"
    assert decode_data_url("data:;base64,YW I") == b"ab"
    assert decode_data_url("data:;base64,Y") is None
    assert decode_data_url("data:;base64,@@") is None
    assert decode_data_url("data:text/plain") is None


def test_gzip_bytes_match_node() -> None:
    blob = base64.b64encode(gzip_bytes(b"Hello, World!")).decode()
    assert blob == "H4sIAAAAAAAAE/NIzcnJ11EIzy/KSVEEANDDSuwNAAAA"


# -- time and JavaScript semantics ---------------------------------------------------------------


async def test_dynamic_resource_prints_the_configured_local_time() -> None:
    app = fake(local_tz="Europe/Paris")
    freeze(app, OCT_12_2026_03_31_23_UTC)
    async with session(app) as wire:
        read = await call(wire, "resources/read", {"uri": "demo://resource/dynamic/text/9"})
    assert read["result"]["contents"][0]["text"] == (
        "Resource 9: This is a plaintext resource created at 5:31:23 AM"
    )


def test_local_time_is_twelve_hour() -> None:
    assert local_time(OCT_12_2026_03_31_23_UTC, "America/New_York") == "11:31:23 PM"
    assert local_time(OCT_12_2026_03_31_23_UTC, "UTC") == "3:31:23 AM"
    assert local_time(OCT_12_2026_03_31_23_UTC + 9 * 3600 * 10**9, "UTC") == "12:31:23 PM"


def test_js_numbers() -> None:
    assert js_string(math.nan) == "NaN"
    assert js_string(math.inf) == "Infinity"
    assert js_string(-math.inf) == "-Infinity"
    assert js_number("-Infinity") == -math.inf
    assert js_number("Infinity") == math.inf
    assert js_number("0o17") == 15


def test_urls_normalize_like_whatwg() -> None:
    assert normalize_url("HTTP:Example.com/x") == "http://example.com/x"
    assert normalize_url("demo://r/a b#frag ment") == "demo://r/a%20b#frag%20ment"
    with pytest.raises(InvalidUrlError):
        normalize_url("http://")
    with pytest.raises(InvalidUrlError):
        normalize_url("1nvalid:scheme")


# -- protocol-level validation -------------------------------------------------------------------


async def test_protocol_parameter_errors_list_zod_issues() -> None:
    async with session(fake()) as wire:
        prompt = await call(wire, "prompts/get", {"name": "args-prompt", "arguments": [1]})
        complete = await call(
            wire,
            "completion/complete",
            {"ref": {"type": "ref/prompt"}, "argument": {"name": "x", "value": ""}},
        )
        level = await call(wire, "logging/setLevel", {"level": "notice"})
        unsubscribe = await call(wire, "resources/unsubscribe", {"uri": "demo://x"})
    assert '"expected": "record"' in prompt["error"]["message"]
    assert '"code": "invalid_union"' in complete["error"]["message"]  # recorded 000411
    assert level["result"] == {}
    assert unsubscribe["result"] == {}


async def test_completions_for_templates_and_unknown_refs() -> None:
    async with session(fake()) as wire:
        blob = await call(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
                "argument": {"name": "other", "value": "3"},
            },
        )
    assert blob["result"] == {"completion": {"values": [], "hasMore": False}}  # recorded 000414


async def test_fast_result_completes_the_task_for_good() -> None:
    app = fake()
    freeze(app, OCT_12_2026_03_31_23_UTC)
    async with session(app) as wire:
        created = await call(
            wire,
            "tools/call",
            {"name": "simulate-research-query", "arguments": {"topic": "kelp"}, "task": {}},
        )
        task_id = created["result"]["task"]["taskId"]
        assert "content" in (await call(wire, "tasks/result", {"taskId": task_id}))["result"]
        status = (await call(wire, "tasks/get", {"taskId": task_id}))["result"]
        cancel = await call(wire, "tasks/cancel", {"taskId": task_id})
    assert status["status"] == "completed"
    assert cancel["error"]["message"].endswith("terminal status: completed")


async def test_tasks_list_pages_by_ten() -> None:
    app = fake()
    async with session(app) as wire:
        made = [
            (
                await call(
                    wire,
                    "tools/call",
                    {"name": "simulate-research-query", "arguments": {"topic": str(i)}, "task": {}},
                )
            )["result"]["task"]["taskId"]
            for i in range(12)
        ]
        first = (await call(wire, "tasks/list", {}))["result"]
        second = (await call(wire, "tasks/list", {"cursor": first["nextCursor"]}))["result"]
        null = await call(wire, "tasks/list", {"cursor": None})
    assert list(first) == ["tasks", "nextCursor", "_meta"]
    assert first["nextCursor"] == made[9]
    assert [t["taskId"] for t in second["tasks"]] == made[10:]
    assert "nextCursor" not in second
    assert '"received null"' not in null["error"]["message"]
    assert "expected string, received null" in null["error"]["message"]


async def test_null_task_is_refused_by_the_protocol() -> None:
    async with session(fake()) as wire:
        reply = await call(
            wire, "tools/call", {"name": "echo", "arguments": {"message": "x"}, "task": None}
        )
    assert reply["error"]["code"] == -32603
    assert "expected object, received null" in reply["error"]["message"]


def test_data_url_fragment_and_whitespace() -> None:
    assert decode_data_url("data:,abc#frag") == b"abc"
    assert decode_data_url("  data:,ab\ncd\tef  ") == b"abcdef"


def test_urls_resolve_dots_and_validate_hosts() -> None:
    assert normalize_url("demo://r/a/../b/%2E/c") == "demo://r/b/c"
    assert normalize_url("demo://r/a/..") == "demo://r/"
    assert normalize_url("https://example.com:443") == "https://example.com/"
    assert normalize_url("http://example.com:8080/x") == "http://example.com:8080/x"
    for bad in ("demo://a b/x", "demo://r:70000/x", "demo://r:12a/x"):
        with pytest.raises(InvalidUrlError):
            normalize_url(bad)
