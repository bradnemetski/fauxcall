"""Pytest setup: how to reach this fake (in-process) and the real API it imitates (``.env``).

FauxCall's parity plugin is enabled in ``pyproject.toml`` (``-p fauxcall.testing.plugin``).

Two helpers for the scenarios, both logging into the scenario's comparison like ``mcp`` does:

- ``open_session``: another MCP session (optionally declaring client capabilities);
- ``call_extended``: ``tools/call`` with params ``call_tool`` doesn't take (``task``, ``_meta``),
  spending the real side's write approval exactly as ``call_tool`` does.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from fauxcall.mcpwire.client import McpWireClient
from fauxcall.testing.client import CompareOptions, McpClient
from fauxcall.testing.support import fake_under_test

from fauxcall_everything.app import build_app
from fauxcall_everything.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fauxcall.testing import FakeUnderTest
    from fauxcall.types import JsonValue

    type OpenSession = Callable[..., Any]
    type CallExtended = Callable[..., Awaitable[JsonValue]]


@pytest.fixture(scope="session")
def fauxcall_fake() -> FakeUnderTest:
    """This fake (fast mode) and the real API (base URL and credentials from ``.env``)."""
    return fake_under_test(Settings, build_app)


@pytest.fixture
def open_session(mcp: McpClient) -> OpenSession:
    """Open another MCP session on the same side, declaring ``capabilities``."""

    @asynccontextmanager
    async def opened(
        capabilities: dict[str, Any] | None = None, timeout_s: float = 60.0
    ) -> AsyncIterator[McpClient]:
        wire = McpWireClient(mcp.wire.url, mcp.wire.http, timeout_s=timeout_s)
        client = McpClient(
            mcp.target,
            wire,
            allow_writes=mcp.allow_writes,
            mutating=mcp.mutating,
            log=mcp.log,
            pace=mcp.pace,
            budget=mcp.budget,
            varies=mcp.varies,
        )
        await wire.initialize(capabilities=capabilities)
        client._open = True  # initialized above, with the capabilities
        try:
            yield client
        finally:
            await client.close()

    return opened


@pytest.fixture
def call_extended() -> CallExtended:
    """``tools/call`` with extra params, spending the real write approval like ``call_tool``."""

    async def call(
        client: McpClient,
        name: str,
        arguments: dict[str, JsonValue],
        *,
        compare: CompareOptions | None = None,
        **extra: JsonValue,
    ) -> JsonValue:
        if client.target == "real" and client.mutating(name):
            if not client.allow_writes:
                pytest.skip(f"tool {name!r} may modify state: needs --allow-real-writes")
            refused = client.budget.spend("POST", f"tools/call:{name}") if client.budget else None
            if refused is not None:
                pytest.skip(f"real write refused: {refused}")
        params: dict[str, JsonValue] = {"name": name, "arguments": arguments, **extra}
        return await client.request(
            "tools/call", params, label=f"tools/call:{name}", compare=compare
        )

    return call
