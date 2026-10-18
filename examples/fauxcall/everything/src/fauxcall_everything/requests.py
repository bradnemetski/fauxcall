"""The checks the SDK makes on a request before any handler runs.

* The transport parses every message with ``JSONRPCMessageSchema``: a request whose ``params`` or
  ``_meta`` is malformed (a ``progressToken`` that is neither a string nor an integer, a related
  task without its id) is refused at the HTTP level, 400 ``Parse error: Invalid JSON-RPC
  message`` (recording 000486), the transport's recorded ``not_jsonrpc`` refusal.
* The protocol then parses the request with its method's schema: every issue, in the schema's
  member order, as one ``-32603`` error (recording 000487: task, name, then arguments).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall.runtime import EarlyResponse, RpcError
from starlette.responses import Response

from fauxcall_everything import zod

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from fauxcall.runtime import RequestContext
    from fauxcall.types import JsonObject, JsonValue

    Handler = Callable[[RequestContext, JsonObject], Awaitable[JsonValue]]

RELATED_TASK: Final = "io.modelcontextprotocol/related-task"


def _message(ctx: RequestContext) -> JsonObject:
    return ctx.json_body if isinstance(ctx.json_body, dict) else {}


def _valid_meta(params: JsonValue) -> bool:
    """``BaseRequestParamsSchema``'s ``_meta``: a progress token and a related task, if any."""
    if not isinstance(params, dict):
        return False
    if "_meta" not in params:
        return True
    meta = params["_meta"]
    if not isinstance(meta, dict):
        return False
    if "progressToken" in meta:
        token = meta["progressToken"]
        whole = (
            isinstance(token, int | float)
            and not isinstance(token, bool)
            and float(token).is_integer()
        )
        if not (isinstance(token, str) or whole):
            return False
    if RELATED_TASK in meta:
        related = meta[RELATED_TASK]
        if not isinstance(related, dict) or not isinstance(related.get("taskId"), str):
            return False
    return True


def check_message(ctx: RequestContext) -> None:
    """Refuse, as the transport does, a request whose ``params`` aren't valid JSON-RPC params."""
    message = _message(ctx)
    if "params" in message and not _valid_meta(message["params"]):
        mcp = ctx.model.mcp
        recorded = mcp.transport.refusals.get("not_jsonrpc") if mcp is not None else None
        if isinstance(recorded, dict):
            status = recorded.get("status")
            raise EarlyResponse(
                Response(
                    content=str(recorded.get("body") or "").encode(),
                    status_code=status if isinstance(status, int) else 400,
                    media_type=str(recorded.get("content_type") or "application/json"),
                )
            )


def params_issue(ctx: RequestContext) -> zod.Issue | None:
    """A method whose params are required, called without any: ``expected object`` at params."""
    message = _message(ctx)
    if "params" not in message:
        return zod.wrong_type("object", zod.MISSING, ["params"])
    return None


def refuse(issues: Sequence[zod.Issue]) -> None:
    """Raise the protocol's refusal of a malformed request, if there are issues."""
    if issues:
        raise RpcError(-32603, zod.issues_text(issues))


def guarded(handler: Handler) -> Handler:
    """``handler``, behind the transport's check of the message's params."""

    async def run(ctx: RequestContext, params: JsonObject) -> JsonValue:
        check_message(ctx)
        return await handler(ctx, params)

    run.__doc__ = handler.__doc__
    return run


async def recorded(_ctx: RequestContext, _params: JsonObject) -> JsonValue:
    """Let FauxCall answer (listings, ``ping``) once the message is checked."""
    return None
