"""Tools that change the client's session: gzip resources, simulated logging, resource updates.

They may modify state (no ``readOnlyHint``): each call spends the approved budget on the real
server. The server keeps nothing beyond the session, which every scenario closes.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from everything_support import session_text

if TYPE_CHECKING:
    from fauxcall.testing import McpClient

GZIP = "gzip-file-as-resource"


def _data_url(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


async def test_gzip_resource_appears_in_the_session(mcp: McpClient) -> None:
    await mcp.listen()
    link: Any = await mcp.call_tool(
        GZIP, {"name": "notes.txt.gz", "data": _data_url("Parity notes: line one\nline two\n")}
    )
    assert isinstance(link, dict)
    uri = link["content"][0]["uri"]
    listing: Any = await mcp.request("resources/list", {})
    assert isinstance(listing, dict)
    assert listing["resources"][-1]["uri"] == uri
    read: Any = await mcp.request("resources/read", {"uri": uri})
    assert isinstance(read, dict)
    assert read["contents"][0]["mimeType"] == "application/gzip"
    await mcp.heard("notifications/resources/list_changed", count=1, within=5)


async def test_gzip_inline_resource_and_overwrite(mcp: McpClient) -> None:
    await mcp.listen()
    inline: Any = await mcp.call_tool(
        GZIP,
        {
            "name": "report.csv.gz",
            "data": "data:text/csv,id%2Cvalue%0A1%2C" + "abc" * 50 + "%0A",
            "outputType": "resource",
        },
    )
    assert isinstance(inline, dict)
    assert inline["content"][0]["type"] == "resource"
    await mcp.call_tool(GZIP, {"name": "report.csv.gz", "data": "data:,replaced"})
    read: Any = await mcp.request(
        "resources/read", {"uri": "demo://resource/session/report.csv.gz"}
    )
    assert isinstance(read, dict)
    changes: Any = await mcp.heard("notifications/resources/list_changed", count=4, within=2)
    assert len(changes) == 3  # registered, then replaced: removed and registered again
    listing: Any = await mcp.request("resources/list", {})
    assert isinstance(listing, dict)
    assert len(listing["resources"]) == 8


async def test_gzip_refusals(mcp: McpClient) -> None:
    unreachable: Any = await mcp.call_tool(
        GZIP, {"name": "x.gz", "data": "https://fauxcall-parity.invalid/missing.txt"}
    )
    assert isinstance(unreachable, dict)
    assert unreachable["isError"] is True
    bad_scheme: Any = await mcp.call_tool(GZIP, {"name": "x.gz", "data": "ws://example.com/socket"})
    assert isinstance(bad_scheme, dict)
    assert bad_scheme["isError"] is True
    invalid: Any = await mcp.call_tool(GZIP, {"data": "nota url", "outputType": "link"})
    assert isinstance(invalid, dict)
    assert invalid["isError"] is True
    error: Any = await mcp.request("resources/read", {"uri": "demo://resource/session/x.gz"})
    assert isinstance(error, dict)


async def test_gzip_default_source_is_linked(mcp: McpClient) -> None:
    link: Any = await mcp.call_tool(GZIP, {})
    assert isinstance(link, dict)
    assert link["content"][0] == {
        "name": "README.md.gz",
        "uri": "demo://resource/session/README.md.gz",
        "mimeType": "application/gzip",
        "type": "resource_link",
    }


async def test_simulated_logging(mcp: McpClient) -> None:
    await mcp.listen()
    started: Any = await mcp.call_tool(
        "toggle-simulated-logging", {}, compare=session_text("$.content[0].text")
    )
    assert isinstance(started, dict)
    await mcp.heard("notifications/message", count=2, within=8)
    stopped: Any = await mcp.call_tool(
        "toggle-simulated-logging", {}, compare=session_text("$.content[0].text")
    )
    assert isinstance(stopped, dict)
    assert "Stopped" in stopped["content"][0]["text"]


async def test_subscriber_updates(mcp: McpClient) -> None:
    await mcp.listen()
    await mcp.request("resources/subscribe", {"uri": "demo://resource/dynamic/text/21"})
    await mcp.request("resources/subscribe", {"uri": "demo://resource/static/document/startup.md"})
    acks: Any = await mcp.heard("notifications/message", count=2, within=5)
    assert [m["params"]["data"].split(" from session ")[0] for m in acks] == [
        "Received Subscribe Resource request for URI: demo://resource/dynamic/text/21",
        "Received Subscribe Resource request for URI: demo://resource/static/document/startup.md",
    ]
    await mcp.call_tool("toggle-subscriber-updates", {}, compare=session_text("$.content[0].text"))
    updates: Any = await mcp.heard(
        "notifications/resources/updated", count=2, within=5, values=True
    )
    assert len(updates) == 2
    await mcp.request("resources/unsubscribe", {"uri": "demo://resource/dynamic/text/21"})
    await mcp.heard("notifications/message", count=1, within=5)
    later: Any = await mcp.heard("notifications/resources/updated", count=1, within=8, values=True)
    assert later[0]["params"] == {"uri": "demo://resource/static/document/startup.md"}
    await mcp.call_tool("toggle-subscriber-updates", {}, compare=session_text("$.content[0].text"))
