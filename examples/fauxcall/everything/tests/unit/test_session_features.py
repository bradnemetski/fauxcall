"""What a client turns on in its session: logging, resource updates, gzip resources, log levels."""

from __future__ import annotations

import base64
import zlib

import pytest
from mcp_session import session, texts

from fauxcall_everything.resources import data_url_body, gzip_bytes

GZIP = "gzip-file-as-resource"


async def test_simulated_logging_ticks_on_the_clock() -> None:
    async with session(listen=True, frozen=True) as client:
        started = await client.tool("toggle-simulated-logging")
        assert texts(started)[0].startswith("Started simulated, random-leveled logging for session")
        assert texts(started)[0].endswith("messages should arrive further apart. ")
        first = await client.heard("notifications/message")
        assert first[0]["params"]["data"].endswith(f"message - SessionId {client.wire.session_id}")
        await client.advance(15.0)
        assert len(await client.heard("notifications/message", count=3)) == 3
        stopped = await client.tool("toggle-simulated-logging")
        assert texts(stopped) == [f"Stopped simulated logging for session {client.wire.session_id}"]
        await client.advance(10.0)
        assert await client.heard("notifications/message", within=0.3) == []


async def test_log_level_filters_simulated_messages() -> None:
    async with session(listen=True, frozen=True) as client:
        assert await client.call("logging/setLevel", {"level": "emergency"}) == {}
        await client.tool("toggle-simulated-logging")
        await client.advance(400.0)
        messages = await client.heard("notifications/message", count=100, within=0.5)
        assert messages
        assert {m["params"]["level"] for m in messages} == {"emergency"}


async def test_subscriber_updates_follow_subscriptions() -> None:
    async with session(listen=True, frozen=True) as client:
        for uri in ("demo://a", "demo://b"):
            assert await client.call("resources/subscribe", {"uri": uri}) == {}
        acks = await client.heard("notifications/message", count=2)
        assert acks[0]["params"] == {
            "level": "info",
            "data": f"Received Subscribe Resource request for URI: demo://a from session "
            f"{client.wire.session_id}",
        }
        started = await client.tool("toggle-subscriber-updates")
        assert "at a 5 second pace" in texts(started)[0]
        now = await client.heard("notifications/resources/updated", count=2)
        assert [m["params"]["uri"] for m in now] == ["demo://a", "demo://b"]
        await client.call("resources/unsubscribe", {"uri": "demo://a"})
        await client.call("resources/unsubscribe", {"uri": "demo://never"})
        await client.advance(5.0)
        later = await client.heard("notifications/resources/updated")
        assert later[0]["params"] == {"uri": "demo://b"}
        stopped = await client.tool("toggle-subscriber-updates")
        assert texts(stopped)[0].startswith("Stopped simulated resource updates for session")
        protocol = await client.call("resources/subscribe", {})
        assert protocol["code"] == -32603


async def test_gzip_session_resources() -> None:
    async with session(listen=True) as client:
        source = "data:text/plain;base64," + base64.b64encode(b"hello gzip").decode()
        link = await client.tool(GZIP, {"name": "a.gz", "data": source})
        assert link["content"][0] == {
            "name": "a.gz",
            "uri": "demo://resource/session/a.gz",
            "mimeType": "application/gzip",
            "type": "resource_link",
        }
        read = await client.call("resources/read", {"uri": "demo://resource/session/a.gz"})
        blob = base64.b64decode(read["contents"][0]["blob"])
        assert zlib.decompress(blob, 31) == b"hello gzip"
        assert blob[9] == 0x13
        await client.tool(GZIP, {"name": "a.gz", "data": "data:,again", "outputType": "resource"})
        listing = await client.call("resources/list", {})
        assert listing["resources"][-1] == {
            "uri": "demo://resource/session/a.gz",
            "name": "a.gz",
            "mimeType": "application/gzip",
        }
        assert len(await client.heard("notifications/resources/list_changed", count=3)) == 3
        complete = await client.call(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "demo://resource/session/a.gz"},
                "argument": {"name": "x", "value": ""},
            },
        )
        assert complete == {"completion": {"values": [], "hasMore": False}}


async def test_gzip_serves_only_what_the_real_server_fetched() -> None:
    async with session() as client:
        recorded = await client.tool(GZIP, {"outputType": "resource"})
        blob = base64.b64decode(recorded["content"][0]["resource"]["blob"])
        assert zlib.decompress(blob, 31).startswith(b"# Model Context Protocol servers")
        example = await client.tool(
            GZIP, {"name": "e.gz", "data": "https://example.com/", "outputType": "resource"}
        )
        assert b"Example Domain" in zlib.decompress(
            base64.b64decode(example["content"][0]["resource"]["blob"]), 31
        )
        unknown = await client.tool(GZIP, {"data": "https://example.org/never-fetched.txt"})
        assert texts(unknown) == ["fetch failed"]
        assert unknown["isError"] is True


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("data:,plain%20text", b"plain text"),
        ("data:text/plain;base64,aGk=", b"hi"),
        ("data:text/plain;base64,aGk", b"hi"),
        ("data:text/plain;base64, a G k = ", b"hi"),
        ("data:text/plain;BASE64,aGk=", b"hi"),
        ("data:text/plain;base64,a", None),
        ("data:text/plain;base64,a$b=", None),
        ("data:no-comma", None),
        ("data:,with#fragment", b"with"),
    ],
)
def test_data_url_bodies(url: str, body: bytes | None) -> None:
    assert data_url_body(url) == body


def test_gzip_bytes_match_the_server() -> None:
    expected = base64.b64decode("H4sIAAAAAAAAE/NIzcnJ11EIzy/KSVEEANDDSuwNAAAA")
    assert gzip_bytes(b"Hello, World!") == expected


async def test_gzip_refusals() -> None:
    async with session() as client:
        bad_base64 = await client.tool(GZIP, {"data": "data:text/plain;base64,%%%"})
        assert texts(bad_base64) == ["fetch failed"]
        scheme = await client.tool(GZIP, {"data": "mailto:someone@example.com"})
        assert texts(scheme)[0].startswith("Error processing file mailto:someone@example.com:")
        invalid = await client.tool(GZIP, {"data": "::not a url::", "name": 4})
        assert texts(invalid)[0] == (
            "MCP error -32602: Input validation error: Invalid arguments for tool "
            "gzip-file-as-resource: Invalid input: expected string, received number at name\n"
            "Invalid URL at data"
        )


async def test_set_level_refuses_unknown_levels() -> None:
    async with session() as client:
        error = await client.call("logging/setLevel", {"level": "verbose"})
        assert error["code"] == -32603
        assert '"invalid_value"' in error["message"]


async def test_reads_refused_like_the_sdk() -> None:
    async with session() as client:
        assert (await client.call("resources/read", {"uri": "::"}))["message"] == "Invalid URL"
        unknown = await client.call("resources/read", {"uri": "demo://resource/dynamic/text/x"})
        assert unknown["message"] == "Unknown resource: demo://resource/dynamic/text/x"
        protocol = await client.call("resources/read", {"uri": 3})
        assert '"received number"' not in protocol["message"]
        assert "expected string, received number" in protocol["message"]
