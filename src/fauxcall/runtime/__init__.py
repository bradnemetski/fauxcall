"""The engine every generated fake runs on.

Generated fakes import from here::

    from fauxcall.runtime import RequestContext, create_app, handle, handle_websocket

MCP tool handlers can also raise ``RpcError`` (a JSON-RPC error) or ``EarlyResponse`` (an HTTP
reply), and keep per-session state in ``ctx.state``.
"""

from fauxcall.runtime.app import (
    FakeRuntime,
    create_app,
    handle,
    handle_websocket,
    register_tool,
    runtime_of,
)
from fauxcall.runtime.context import EarlyResponse, RequestContext
from fauxcall.runtime.mcp_server import RpcError

__all__ = [
    "EarlyResponse",
    "FakeRuntime",
    "RequestContext",
    "RpcError",
    "create_app",
    "handle",
    "handle_websocket",
    "register_tool",
    "runtime_of",
]
