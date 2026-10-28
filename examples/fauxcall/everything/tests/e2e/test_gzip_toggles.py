"""Tools that change session state: gzip session resources, simulated logging, subscriptions.

Each spends the approved write budget on the real server (``--allow-real-writes``); the state
they create lives in the MCP session only. Their effects are checked through other methods
(``resources/list``, ``resources/read``) and what the server pushes on the GET stream.
"""

from __future__ import annotations

import base64
import gzip
from typing import TYPE_CHECKING, Any

from scenario_support import ignoring, text_of

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

GZIP = "gzip-file-as-resource"
SESSION = "demo://resource/session"
NOTES = "Parity notes: the same line, again and again.\n" * 12


def data_url(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


def gunzipped(result: object, path: tuple[str, ...]) -> bytes:
    node = result
    for key in path:
        assert isinstance(node, dict | list)
        node = node[int(key)] if isinstance(node, list) else node[key]
    assert isinstance(node, str)
    return gzip.decompress(base64.b64decode(node))


async def test_gzip_makes_session_resources(mcp: McpClient) -> None:
    await mcp.listen()
    made: Any = await mcp.call_tool(
        GZIP, {"name": "notes.txt.gz", "data": data_url(NOTES), "outputType": "resource"}
    )
    assert gunzipped(made, ("content", "0", "resource", "blob")) == NOTES.encode()
    linked: Any = await mcp.call_tool(GZIP, {"name": "greeting.gz", "data": "data:,Hi%20there%21"})
    assert isinstance(linked, dict)
    assert linked["content"][0] == {
        "name": "greeting.gz",
        "uri": f"{SESSION}/greeting.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }
    read: Any = await mcp.request("resources/read", {"uri": f"{SESSION}/greeting.gz"})
    assert gunzipped(read, ("contents", "0", "blob")) == b"Hi there!"
    await mcp.call_tool(GZIP, {"name": "notes.txt.gz", "data": "data:text/csv,a%2Cb%0A1%2C2"})
    listing: Any = await mcp.request("resources/list", {})
    assert isinstance(listing, dict)
    assert [r["name"] for r in listing["resources"][7:]] == ["greeting.gz", "notes.txt.gz"]
    replaced: Any = await mcp.request("resources/read", {"uri": f"{SESSION}/notes.txt.gz"})
    assert gunzipped(replaced, ("contents", "0", "blob")) == b"a,b\n1,2"
    changes: Any = await mcp.heard("notifications/resources/list_changed", count=4, within=5)
    assert len(changes) == 4


async def test_gzip_refusals(mcp: McpClient) -> None:
    ftp: Any = await mcp.call_tool(
        GZIP, {"name": "f.gz", "data": "ftp://ftp.example.org/readme.txt"}
    )
    assert text_of(ftp).startswith("Error processing file ftp://ftp.example.org/readme.txt")
    unreachable: Any = await mcp.call_tool(
        GZIP, {"name": "u.gz", "data": "https://fauxcall-parity.invalid/file.txt"}
    )
    assert text_of(unreachable) == "fetch failed"
    await mcp.call_tool(GZIP, {"name": "x.gz", "data": "plain words", "outputType": "link"})
    still_none: Any = await mcp.request("resources/read", {"uri": f"{SESSION}/u.gz"})
    assert isinstance(still_none, dict)
    assert still_none["code"] == -32602


async def test_gzip_default_fetches_the_servers_readme(mcp: McpClient) -> None:
    fetched: Any = await mcp.call_tool(
        GZIP, {"outputType": "resource"}, compare=ignoring("$.content[0].resource.blob")
    )
    text = gunzipped(fetched, ("content", "0", "resource", "blob")).decode()
    assert text.startswith("# Model Context Protocol servers")


async def test_simulated_logging(mcp: McpClient) -> None:
    await mcp.listen()
    started: Any = await mcp.call_tool("toggle-simulated-logging", {})
    assert text_of(started).startswith("Started simulated, random-leveled logging for session ")
    messages: Any = await mcp.heard("notifications/message", count=2, within=8)
    for message in messages:
        assert isinstance(message, dict)
        assert message["params"]["level"] in {
            "debug",
            "info",
            "notice",
            "warning",
            "error",
            "critical",
            "alert",
            "emergency",
        }
        assert " - SessionId " in message["params"]["data"]
    stopped: Any = await mcp.call_tool("toggle-simulated-logging", {})
    assert text_of(stopped).startswith("Stopped simulated logging for session ")


async def test_subscriptions_and_updates(mcp: McpClient) -> None:
    await mcp.listen()
    features = "demo://resource/static/document/features.md"
    dynamic = "demo://resource/dynamic/text/5"
    assert await mcp.request("resources/subscribe", {"uri": features}) == {}
    assert await mcp.request("resources/subscribe", {"uri": dynamic}) == {}
    acknowledged: Any = await mcp.heard("notifications/message", count=2, within=5)
    assert all("Received Subscribe Resource request for URI" in str(m) for m in acknowledged)
    await mcp.call_tool("toggle-subscriber-updates", {})
    first: Any = await mcp.heard("notifications/resources/updated", count=2, within=4, values=True)
    assert [m["params"]["uri"] for m in first if isinstance(m, dict)] == [features, dynamic]
    await mcp.request("resources/unsubscribe", {"uri": features})
    await mcp.heard("notifications/message", count=1, within=5)
    later: Any = await mcp.heard("notifications/resources/updated", count=1, within=8, values=True)
    assert [m["params"]["uri"] for m in later if isinstance(m, dict)] == [dynamic]
    await mcp.call_tool("toggle-subscriber-updates", {})
    await mcp.request("resources/subscribe", {"uri": 12})


async def test_log_level_filters_what_the_server_sends(mcp: McpClient) -> None:
    await mcp.listen()
    assert await mcp.request("logging/setLevel", {"level": "warning"}) == {}
    hidden = "demo://resource/dynamic/text/11"
    await mcp.request("resources/subscribe", {"uri": hidden})  # its info ack is below warning
    assert await mcp.request("logging/setLevel", {"level": "info"}) == {}
    shown = "demo://resource/dynamic/text/12"
    await mcp.request("resources/subscribe", {"uri": shown})
    acks: Any = await mcp.heard("notifications/message", count=1, within=5)
    assert shown in acks[0]["params"]["data"]
    assert hidden not in acks[0]["params"]["data"]
