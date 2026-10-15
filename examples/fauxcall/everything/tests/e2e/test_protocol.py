"""How the server's schemas refuse malformed requests, with inputs no recording used.

The SDK checks every message twice: its transport refuses one that isn't JSON-RPC with HTTP 400
(a progress token that is neither a string nor an integer), then the request's own schema
answers -32603 with zod's issues (a ``task`` that isn't an object, a completion without a value,
a cursor that isn't text). Tools check their arguments with zod too, numbers JavaScript can't
hold included. Recorded by the probes in ``protocol-schema``, ``async-bad-replies`` and
``research-late``; varied here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from scenario_support import dig, task_times, text, tool_call

if TYPE_CHECKING:
    from fauxcall.testing import McpClient
    from fauxcall.types import JsonObject, JsonValue

NOT_JSON_RPC = {"code": -32700, "message": "Parse error: Invalid JSON-RPC message"}
COMPLETABLE: JsonObject = {"type": "ref/prompt", "name": "completable-prompt"}


def _issues(refusal: JsonValue) -> list[JsonObject]:
    """The zod issues a -32603 refusal carries as JSON text."""
    assert dig(refusal, "code") == -32603
    found = json.loads(str(dig(refusal, "message")))
    assert isinstance(found, list)
    return found


@pytest.mark.parametrize(
    ("task", "path", "received"),
    [
        (False, ["params", "task"], "expected object, received boolean"),
        ("later", ["params", "task"], "expected object, received string"),
        ([], ["params", "task"], "expected object, received array"),
        ({"ttl": None}, ["params", "task", "ttl"], "expected number, received null"),
        ({"ttl": "1m"}, ["params", "task", "ttl"], "expected number, received string"),
    ],
)
async def test_a_task_must_be_an_object_with_a_numeric_ttl(
    mcp: McpClient, task: JsonValue, path: list[str], received: str
) -> None:
    refused = await mcp.request(
        "tools/call", {"name": "echo", "arguments": {"message": "hi"}, "task": task}
    )
    assert _issues(refused) == [
        {
            "expected": received.split()[1].rstrip(","),
            "code": "invalid_type",
            "path": path,
            "message": f"Invalid input: {received}",
        }
    ]


async def test_a_task_true_as_the_docs_say_is_refused(mcp: McpClient) -> None:
    """The server's own features.md says ``task: true``; its schema wants an object."""
    refused = await tool_call(
        mcp, {"name": "simulate-research-query", "arguments": {"topic": "kelp"}, "task": True}
    )
    assert [dig(issue, "message") for issue in _issues(refused)] == [
        "Invalid input: expected object, received boolean"
    ]


async def test_extra_task_members_pass_the_schema(mcp: McpClient) -> None:
    refused = await mcp.request(
        "tools/call",
        {"name": "get-sum", "arguments": {"a": 2, "b": 3}, "task": {"ttl": 9, "mode": "x"}},
    )
    assert dig(refused, "code") == -32602
    assert str(dig(refused, "message")).startswith(
        "MCP error -32602: Invalid task creation result: "
    )


@pytest.mark.parametrize(
    ("meta", "valid"),
    [
        ({"progressToken": "abc"}, True),
        ({"progressToken": 42}, True),
        ({"progressToken": 2.5}, False),
        ({"progressToken": None}, False),
        ({"progressToken": [1]}, False),
        ({"io.modelcontextprotocol/related-task": {"taskId": 7}}, False),
        ({"anything": {"at": "all"}}, True),
        ("meta", False),
    ],
)
async def test_meta_is_checked_by_the_transport(
    mcp: McpClient, meta: JsonValue, *, valid: bool
) -> None:
    answered = await mcp.request("ping", {"_meta": meta})
    assert answered == ({} if valid else NOT_JSON_RPC)


@pytest.mark.parametrize(
    ("method", "params", "task_id"),
    [
        ("logging/setLevel", {"level": "notice"}, "t"),
        ("ping", {}, "0" * 32),
        ("tools/list", {}, "nope"),
        ("prompts/list", {}, "x-1"),
        ("resources/templates/list", {}, "x-2"),
        ("resources/read", {"uri": "demo://resource/static/document/README.md"}, "x-3"),
        ("tools/call", {"name": "get-weather", "arguments": {}}, "x-4"),  # an unknown tool too
    ],
)
async def test_a_request_naming_an_unknown_task_is_refused(
    mcp: McpClient, method: str, params: JsonObject, task_id: str
) -> None:
    """The SDK looks a related task up before any handler (000520-000525)."""
    related: JsonObject = {"io.modelcontextprotocol/related-task": {"taskId": task_id}, "extra": 1}
    refused = await mcp.request(method, {**params, "_meta": related})
    assert refused == {"code": -32602, "message": f"MCP error -32602: Task not found: {task_id}"}


async def test_an_unknown_method_naming_a_task_is_not_found(mcp: McpClient) -> None:
    related: JsonObject = {"io.modelcontextprotocol/related-task": {"taskId": "t"}}
    refused = await mcp.request("resources/watch", {"_meta": related})
    assert refused == {"code": -32601, "message": "Method not found"}


@pytest.mark.parametrize(
    ("params", "paths"),
    [
        ({"protocolVersion": "2025-06-18", "capabilities": {}}, [["params", "clientInfo"]]),
        (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"sampling": {"tools": True}},
                "clientInfo": {"name": "agent", "version": 2},
            },
            [["params", "capabilities", "sampling", "tools"], ["params", "clientInfo", "version"]],
        ),
    ],
)
async def test_an_initialize_inside_a_session_is_parsed_like_a_request(
    mcp: McpClient, params: JsonObject, paths: list[list[str]]
) -> None:
    """Not a handshake for the transport: its handler refuses it with zod's issues."""
    refused = await mcp.request("initialize", params)
    assert [dig(issue, "path") for issue in _issues(refused)] == paths


async def test_an_infinite_ttl_is_named(mcp: McpClient) -> None:
    refused = await mcp.request(
        "tools/call", {"name": "echo", "arguments": {"message": "x"}, "task": {"ttl": -(10**400)}}
    )
    assert _issues(refused) == [
        {
            "expected": "number",
            "code": "invalid_type",
            "received": "-Infinity",
            "path": ["params", "task", "ttl"],
            "message": "Invalid input: expected number, received -Infinity",
        }
    ]


async def test_a_tool_call_with_a_bad_progress_token(mcp: McpClient) -> None:
    refused = await mcp.request(
        "tools/call",
        {"name": "get-sum", "arguments": {"a": 1, "b": 2}, "_meta": {"progressToken": False}},
    )
    assert refused == NOT_JSON_RPC


@pytest.mark.parametrize(
    ("params", "issues"),
    [
        (
            {"ref": COMPLETABLE, "argument": {"name": "department"}},
            [(["params", "argument", "value"], "expected string, received undefined")],
        ),
        (
            {"ref": COMPLETABLE, "argument": {"value": "En"}},
            [(["params", "argument", "name"], "expected string, received undefined")],
        ),
        (
            {"ref": COMPLETABLE, "argument": {"name": "department", "value": None}},
            [(["params", "argument", "value"], "expected string, received null")],
        ),
        (
            {
                "ref": COMPLETABLE,
                "argument": {"name": "name", "value": "A"},
                "context": {"arguments": {"department": True}},
            },
            [
                (
                    ["params", "context", "arguments", "department"],
                    "expected string, received boolean",
                )
            ],
        ),
        (
            {
                "ref": {"type": "ref/prompt", "name": "nobody"},
                "argument": {"name": "name", "value": ""},
                "context": {"arguments": []},
            },
            [(["params", "context", "arguments"], "expected record, received array")],
        ),
    ],
)
async def test_completion_requests_are_checked_before_the_prompt(
    mcp: McpClient, params: JsonObject, issues: list[tuple[list[str], str]]
) -> None:
    refused = await mcp.request("completion/complete", params)
    found = [(dig(i, "path"), str(dig(i, "message"))) for i in _issues(refused)]
    assert found == [(path, f"Invalid input: {received}") for path, received in issues]


@pytest.mark.parametrize("cursor", [10, True, None])
async def test_a_task_cursor_is_text(mcp: McpClient, cursor: JsonValue) -> None:
    refused = await mcp.request("tasks/list", {"cursor": cursor})
    assert [dig(i, "path") for i in _issues(refused)] == [["params", "cursor"]]


@pytest.mark.parametrize(
    ("arguments", "bad"),
    [
        ({"city": 75, "state": True}, ["city", "state"]),
        ({"city": "Lyon", "state": 5}, ["state"]),
    ],
)
async def test_every_bad_prompt_argument_is_reported(
    mcp: McpClient, arguments: JsonObject, bad: list[str]
) -> None:
    refused = await mcp.request("prompts/get", {"name": "args-prompt", "arguments": arguments})
    assert [dig(i, "path") for i in _issues(refused)] == [
        ["params", "arguments", name] for name in bad
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"a": 10**400, "b": 1}, "Invalid input: expected number, received Infinity at a"),
        ({"a": 4, "b": -(10**309)}, "Invalid input: expected number, received -Infinity at b"),
    ],
)
async def test_numbers_javascript_cannot_hold(
    mcp: McpClient, arguments: JsonObject, message: str
) -> None:
    result = await mcp.call_tool("get-sum", arguments)
    assert dig(result, "isError") is True
    assert text(result) == (
        f"MCP error -32602: Input validation error: Invalid arguments for tool get-sum: {message}"
    )


async def test_a_clarification_that_is_not_text_fails_the_task(mcp: McpClient) -> None:
    """The report calls ``clarification.includes``: a number has no such method (000504)."""
    await mcp.open(
        capabilities={"elicitation": {}},
        replies={"elicitation/create": {"action": "accept", "content": {"interpretation": 3}}},
    )
    created = await tool_call(
        mcp,
        {
            "name": "simulate-research-query",
            "arguments": {"topic": "Python packaging", "ambiguous": True},
            "task": {},
        },
        compare=task_times("$.task"),
    )
    task_id = dig(created, "task", "taskId")
    result = await mcp.request("tasks/result", {"taskId": task_id})
    assert result == {"code": -32603, "message": f"Task {task_id} has no result stored"}
    failed = await mcp.request("tasks/get", {"taskId": task_id}, compare=task_times())
    assert (dig(failed, "status"), dig(failed, "statusMessage")) == (
        "failed",
        "TypeError: state.clarification.includes is not a function",
    )


ASYNC_CAPABLE: JsonObject = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


@pytest.mark.parametrize(
    ("replies", "paths"),
    [
        (
            {"sampling/createMessage": {"task": {"taskId": "t", "status": 1}}},
            [["task", "status"]],
        ),
        (
            {"sampling/createMessage": {"role": "assistant", "model": "m"}},
            [["task"]],
        ),
    ],
)
async def test_async_sampling_refuses_malformed_replies(
    mcp: McpClient, replies: JsonObject, paths: list[list[str]]
) -> None:
    await mcp.open(capabilities=ASYNC_CAPABLE, replies=replies)
    result = await mcp.call_tool("trigger-sampling-request-async", {"prompt": "Name a river"})
    assert dig(result, "isError") is True
    issues = json.loads(text(result))
    union = dig(issues, 0)
    assert dig(union, "code") == "invalid_union"
    assert [dig(i, "path") for i in dig(union, "errors", 0)] == paths  # type: ignore[union-attr]


async def test_async_elicitation_refuses_a_poll_without_status(mcp: McpClient) -> None:
    await mcp.open(
        capabilities=ASYNC_CAPABLE,
        replies={
            "elicitation/create": {"task": {"taskId": "e-1", "status": "working"}},
            "tasks/get": {"statusMessage": "still thinking", "status": None},
        },
    )
    result = await mcp.call_tool("trigger-elicitation-request-async", {})
    assert dig(result, "isError") is True
    assert json.loads(text(result)) == [
        {
            "expected": "string",
            "code": "invalid_type",
            "path": ["status"],
            "message": "Invalid input: expected string, received null",
        }
    ]
