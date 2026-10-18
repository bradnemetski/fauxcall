"""Tool, resource, prompt and completion behavior beyond the recorded calls (from the server's
source), checked in-process: data URLs, task augmentation, refused or odd client answers,
client-side task outcomes, URL normalization, and malformed requests."""

from __future__ import annotations

import base64
import gzip
from typing import TYPE_CHECKING, Any, Final

import pytest
from fauxcall.testing.support import settings_for_tests
from wire_helpers import client, rpc, tool

from fauxcall_everything import tasks, tools
from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fauxcall.types import JsonObject

SAMPLING: Final[JsonObject] = {"sampling": {}}
ELICITATION: Final[JsonObject] = {"elicitation": {"form": {}, "url": {}}}
CLIENT_TASKS: Final[JsonObject] = {
    "sampling": {},
    "elicitation": {},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(tasks, "TICK", 0.05)
    return build_app(settings_for_tests(Settings))


def text_of(result: Any, index: int = 0) -> str:
    return str(result["content"][index]["text"])


# -- gzip ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "content"),
    [
        ("data:,plain", b"plain"),
        ("data:text/plain;base64,SGk=", b"Hi"),
        ("data:text/plain;base64,SG k", b"Hi"),  # forgiving: whitespace dropped, padding optional
        ("data:;base64,SGk=#fragment", b"Hi"),
        ("data:text/plain,%E2%9C%93%20ok", "✓ ok".encode()),
    ],
)
async def test_gzip_compresses_data_urls(app: FastAPI, data: str, content: bytes) -> None:
    async with client(app, listen=False) as wire:
        result = await tool(
            wire, "gzip-file-as-resource", {"name": "x.gz", "data": data, "outputType": "resource"}
        )
    blob = result["content"][0]["resource"]["blob"]
    assert gzip.decompress(base64.b64decode(blob)) == content


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("data:text/plain", "fetch failed"),  # no comma
        ("data:;base64,S", "fetch failed"),  # one character too many
        ("data:;base64,S*==", "fetch failed"),
        ("https://example.com/file.txt", "fetch failed"),  # never fetched: the fake is offline
        (
            "file:///etc/hosts",
            (
                "Error processing file file:///etc/hosts: Unsupported URL protocol for "
                "file:///etc/hosts. Only http, https, and data URLs are supported."
            ),
        ),
    ],
)
async def test_gzip_failures(app: FastAPI, data: str, message: str) -> None:
    async with client(app, listen=False) as wire:
        result = await tool(wire, "gzip-file-as-resource", {"name": "x.gz", "data": data})
    assert result == {"content": [{"type": "text", "text": message}], "isError": True}


async def test_gzip_refuses_what_exceeds_the_fetch_limit(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "GZIP_MAX_FETCH_SIZE", 3)
    async with client(app, listen=False) as wire:
        result = await tool(wire, "gzip-file-as-resource", {"name": "x", "data": "data:,four"})
    assert text_of(result) == "Response from data:,four exceeds 3 bytes"


async def test_gzip_default_serves_the_recorded_readme(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        await tool(wire, "gzip-file-as-resource", {})
        read = await rpc(wire, "resources/read", {"uri": "demo://resource/session/README.md.gz"})
        completion = await rpc(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/session/README.md.gz"},
                "argument": {"name": "x", "value": ""},
            },
        )
    readme = gzip.decompress(base64.b64decode(read.result["contents"][0]["blob"]))
    assert readme.startswith(b"# Model Context Protocol servers")
    assert completion.result == {"completion": {"values": [], "hasMore": False}}


# -- task augmentation -----------------------------------------------------------------------------


INVALID_TASK: Final = (
    'MCP error -32602: Invalid task creation result: [\n  {\n    "expected": "object",\n'
    '    "code": "invalid_type",\n    "path": [\n      "task"\n    ],\n'
    '    "message": "Invalid input: expected object, received undefined"\n  }\n]'
)


async def test_a_tool_without_tasks_runs_and_is_refused_as_a_task(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        toggled = await rpc(
            wire, "tools/call", {"name": "toggle-simulated-logging", "arguments": {}, "task": {}}
        )
        again = await tool(wire, "toggle-simulated-logging")
        unknown = await rpc(wire, "tools/call", {"name": "nope", "arguments": {}, "task": {}})
        hidden = await rpc(
            wire, "tools/call", {"name": "trigger-sampling-request", "arguments": {}, "task": {}}
        )
    assert toggled.error == {"code": -32602, "message": INVALID_TASK}
    assert text_of(again).startswith("Stopped simulated logging")  # the first one did start it
    assert unknown.error == hidden.error == {"code": -32602, "message": INVALID_TASK}


@pytest.mark.parametrize(
    ("task", "path"),
    [
        ("soon", ["params", "task"]),
        (None, ["params", "task"]),
        ({"ttl": "1"}, ["params", "task", "ttl"]),
    ],
)
async def test_malformed_task_members(app: FastAPI, task: Any, path: list[str]) -> None:
    async with client(app, listen=False) as wire:
        reply = await rpc(
            wire, "tools/call", {"name": "echo", "arguments": {"message": "x"}, "task": task}
        )
    assert reply.error["code"] == -32603
    assert f'"path": [\n      "{path[0]}"' in reply.error["message"]


async def test_url_elicitation_errors_pass_through_a_task_request(app: FastAPI) -> None:
    async with client(app, ELICITATION, listen=False) as wire:
        reply = await rpc(
            wire,
            "tools/call",
            {
                "name": "trigger-url-elicitation",
                "arguments": {"url": "https://x.y", "errorPath": True},
                "task": {},
            },
        )
    assert reply.error["code"] == -32042


# -- sampling and elicitation ----------------------------------------------------------------------


async def test_sampling_reports_invalid_and_timed_out_answers(app: FastAPI) -> None:
    replies = {
        "sampling/createMessage": {
            "model": "m",
            "role": "robot",
            "content": {"type": "text", "text": "x"},
        }
    }
    async with client(app, SAMPLING, replies, listen=False) as wire:
        bad = await tool(wire, "trigger-sampling-request", {"prompt": "x", "maxTokens": 2.5})
    assert bad["isError"] is True
    assert '"code": "invalid_value"' in text_of(bad)


async def test_elicitation_reports_invalid_answers(app: FastAPI) -> None:
    replies = {"elicitation/create": {"action": "later"}}
    async with client(app, ELICITATION, replies, listen=False) as wire:
        form = await tool(wire, "trigger-elicitation-request")
        url = await tool(wire, "trigger-url-elicitation", {"url": "https://x.y"})
    assert form["isError"] is True
    assert url["isError"] is True
    assert "Invalid option" in text_of(form)


async def test_elicitation_accept_lists_what_the_user_gave(app: FastAPI) -> None:
    content = {
        "name": "",
        "check": False,
        "integer": 0,
        "number": 2.5,
        "petType": "pet-2",
        "color": "red",
    }
    replies = {"elicitation/create": {"action": "accept", "content": content}}
    async with client(app, ELICITATION, replies, listen=False) as wire:
        result = await tool(wire, "trigger-elicitation-request")
    assert text_of(result, 1) == (
        "User inputs:\n- Agreed to terms: false\n- Favorite Color: red\n"
        "- Favorite Integer: 0\n- Favorite Number: 2.5\n- Pet Type: pet-2"
    )


async def test_a_client_that_never_answers_times_out(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "REQUEST_TIMEOUT_S", 0.2)
    async with client(app, SAMPLING, listen=False) as wire:
        wire.on_request = None  # a client that never answers
        result = await tool(wire, "trigger-sampling-request", {"prompt": "x"})
    assert result == {
        "content": [{"type": "text", "text": "MCP error -32001: Request timed out"}],
        "isError": True,
    }


# -- client-side tasks -----------------------------------------------------------------------------


async def test_async_sampling_polls_until_its_limit(app: FastAPI) -> None:
    replies = {
        "sampling/createMessage": {"task": {"taskId": "c1", "status": "working"}},
        "tasks/get": {"status": "working"},
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        result = await tool(wire, "trigger-sampling-request-async", {"prompt": "x"})
    text = text_of(result)
    assert text.startswith(
        "[TIMEOUT] Task timed out after 60 poll attempts\n\nProgress:\nTask created: c1\n"
    )
    assert text.count("Poll ") == 60


async def test_async_sampling_reports_a_cancelled_task_and_bad_polls(app: FastAPI) -> None:
    replies: dict[str, Any] = {
        "sampling/createMessage": {"task": {"taskId": "c2", "status": "cancelled"}},
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        cancelled = await tool(wire, "trigger-sampling-request-async", {"prompt": "x"})
    assert text_of(cancelled) == "[CANCELLED] No message\n\nProgress:\nTask created: c2"
    replies = {
        "sampling/createMessage": {"task": {"taskId": "c3", "status": "working"}},
        "tasks/get": {"state": "?"},
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        bad = await tool(wire, "trigger-sampling-request-async", {"prompt": "x"})
    assert bad["isError"] is True
    assert '"path": [\n      "status"' in text_of(bad)


async def test_async_sampling_refusals(app: FastAPI) -> None:
    replies: dict[str, Any] = {"sampling/createMessage": {"error": {"code": -2, "message": "no"}}}
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        refused = await tool(wire, "trigger-sampling-request-async", {"prompt": "x"})
        bad = await tool(wire, "trigger-sampling-request-async", {"prompt": 1})
    assert refused == {"content": [{"type": "text", "text": "MCP error -2: no"}], "isError": True}
    assert bad["isError"] is True
    replies = {"sampling/createMessage": {"nothing": True}}
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        odd = await tool(wire, "trigger-sampling-request-async", {"prompt": "x"})
    assert odd["isError"] is True


async def test_async_elicitation_logs_input_required_polls_sparingly(app: FastAPI) -> None:
    polls = iter([*["input_required"] * 11, "completed"])

    async def answer(request: Any) -> Any:
        method = request["method"]
        if method == "elicitation/create":
            body: Any = {"task": {"taskId": "e1", "status": "input_required"}}
        elif method == "tasks/get":
            body = {"status": next(polls)}
        else:
            body = {
                "action": "accept",
                "content": {"name": "N", "favoriteColor": "", "agreeToTerms": False},
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": body}

    async with client(app, CLIENT_TASKS, listen=False) as wire:
        wire.on_request = answer
        result = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(result, 1) == "User inputs:\n- Name: N\n- Agreed to terms: false"
    progress = text_of(result, 2)
    assert "Poll 1: input_required\nPoll 10: input_required\nPoll 12: completed" in progress


@pytest.mark.parametrize(
    ("final", "first"),
    [
        ({"action": "cancel"}, "[CANCELLED] User cancelled the elicitation dialog."),
        ({"action": "other"}, "\nProgress:"),
    ],
)
async def test_async_elicitation_outcomes(app: FastAPI, final: JsonObject, first: str) -> None:
    replies = {
        "elicitation/create": {"task": {"taskId": "e2", "status": "completed"}},
        "tasks/result": final,
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        result = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(result).startswith(first)


async def test_async_elicitation_failure_and_refusal(app: FastAPI) -> None:
    replies = {
        "elicitation/create": {"task": {"taskId": "e3", "status": "working"}},
        "tasks/get": {"status": "failed", "statusMessage": "gone"},
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        failed = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(failed) == "[FAILED] gone\n\nProgress:\nTask created: e3\nPoll 1: failed - gone"
    async with client(app, CLIENT_TASKS, {"elicitation/create": {"oops": 1}}, listen=False) as wire:
        bad = await tool(wire, "trigger-elicitation-request-async")
        refused_replies = {"elicitation/create": {"error": {"code": -3, "message": "x"}}}
        wire.on_request = None
    assert bad["isError"] is True
    async with client(app, CLIENT_TASKS, refused_replies, listen=False) as wire:
        refused = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(refused) == "MCP error -3: x"


async def test_async_elicitation_times_out_after_600_polls(app: FastAPI) -> None:
    replies = {
        "elicitation/create": {"task": {"taskId": "e4", "status": "working"}},
        "tasks/get": {"status": "working"},
    }
    async with client(app, CLIENT_TASKS, replies, listen=False) as wire:
        result = await tool(wire, "trigger-elicitation-request-async")
    assert text_of(result).startswith("[TIMEOUT] Task timed out after 600 poll attempts")


# -- other tools -----------------------------------------------------------------------------------


async def test_long_running_operation_odd_steps(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        none = await tool(wire, "trigger-long-running-operation", {"duration": 0, "steps": 0})
        tokened = await rpc(
            wire,
            "tools/call",
            {
                "name": "trigger-long-running-operation",
                "arguments": {"duration": 1, "steps": 1},
                "_meta": {"progressToken": None},
            },
        )
    assert text_of(none) == "Long running operation completed. Duration: 0 seconds, Steps: 0."
    # a null progress token isn't valid JSON-RPC params: the transport refuses it (000486)
    assert tokened.error == {"code": -32700, "message": "Parse error: Invalid JSON-RPC message"}


async def test_resource_reference_numbers_print_like_javascript(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        big = await tool(wire, "get-resource-reference", {"resourceId": 1e21})
        frac = await tool(wire, "get-resource-reference", {"resourceId": 2.5})
    assert text_of(big) == "Returning resource reference for Resource 1e+21:"
    assert big["content"][1]["resource"]["uri"] == "demo://resource/dynamic/text/1e+21"
    assert text_of(frac) == "Invalid resourceId: 2.5. Must be a finite positive integer."


async def test_tiny_image(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        result = await tool(wire, "get-tiny-image", {"ignored": 1})
    assert [block["type"] for block in result["content"]] == ["text", "image", "text"]


# -- resources, prompts, completions ---------------------------------------------------------------


async def test_resource_uris_are_read_as_new_url_writes_them(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        upper = await rpc(
            wire, "resources/read", {"uri": "DEMO://resource/static/document/features.md"}
        )
        padded = await rpc(wire, "resources/read", {"uri": "demo://resource/dynamic/text/01"})
        invalid = await rpc(wire, "resources/read", {"uri": "no scheme"})
        comma = await rpc(wire, "resources/read", {"uri": "demo://resource/dynamic/text/1,2"})
    assert upper.result["contents"][0]["uri"] == "demo://resource/static/document/features.md"
    assert padded.result["contents"][0]["text"].startswith("Resource 1: This is a plaintext")
    assert padded.result["contents"][0]["uri"] == "demo://resource/dynamic/text/01"
    assert invalid.error == {"code": -32603, "message": "Invalid URL"}
    assert comma.error["code"] == -32602


async def test_malformed_requests(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        unsubscribe = await rpc(wire, "resources/unsubscribe", {"uri": 1})
        level = await rpc(wire, "logging/setLevel", {"level": 3})
        record = await rpc(wire, "prompts/get", {"name": "args-prompt", "arguments": ["x"]})
        ref = await rpc(
            wire, "completion/complete", {"ref": "x", "argument": {"name": "a", "value": "b"}}
        )
        argument = await rpc(
            wire,
            "completion/complete",
            {"ref": {"type": "ref/prompt", "name": "args-prompt"}, "argument": {"name": "city"}},
        )
        prompt_ref = await rpc(
            wire,
            "completion/complete",
            {"ref": {"type": "ref/prompt"}, "argument": {"name": "a", "value": "b"}},
        )
    for reply in (unsubscribe, level, record, ref, argument, prompt_ref):
        assert reply.error["code"] == -32603
    assert '"received undefined"' not in ref.error["message"]
    assert "expected string, received undefined" in argument.error["message"]


async def test_prompts_and_completions_beyond_the_recordings(app: FastAPI) -> None:
    async with client(app, listen=False) as wire:
        no_state = await rpc(
            wire, "prompts/get", {"name": "args-prompt", "arguments": {"city": "Oslo", "state": ""}}
        )
        resource = await rpc(
            wire,
            "prompts/get",
            {
                "name": "resource-prompt",
                "arguments": {"resourceType": "Blob", "resourceId": "0x10"},
            },
        )
        both = await rpc(wire, "prompts/get", {"name": "completable-prompt", "arguments": {}})
        team = await rpc(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "completable-prompt"},
                "argument": {"name": "name", "value": "K"},
                "context": {"arguments": {"department": "Support"}},
            },
        )
        other = await rpc(
            wire,
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/dynamic/blob/{resourceId}"},
                "argument": {"name": "resourceId", "value": "1e2"},
            },
        )
    assert no_state.result["messages"][0]["content"]["text"] == "What's weather in Oslo?"
    assert resource.result["messages"][0]["content"]["text"].startswith(
        "This prompt includes the Blob resource with id: 16."
    )
    assert both.error["message"] == (
        "MCP error -32602: Invalid arguments for prompt completable-prompt: "
        "Invalid input: expected string, received undefined at department\n"
        "Invalid input: expected string, received undefined at name"
    )
    assert team.result == {"completion": {"values": ["Kim"], "total": 1, "hasMore": False}}
    assert other.result == {"completion": {"values": ["1e2"], "total": 1, "hasMore": False}}


async def test_completion_values_are_capped_at_a_hundred() -> None:
    from fauxcall_everything.resources import _completed  # noqa: PLC0415 - the helper itself

    capped = _completed([str(i) for i in range(150)])
    assert capped["completion"] == {
        "values": [str(i) for i in range(100)],
        "total": 150,
        "hasMore": True,
    }
