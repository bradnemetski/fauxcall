"""Shared pieces of the everything server's scenarios: capabilities, client answers, comparisons."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fauxcall.model.fields import IgnorePolicy, NowPolicy, RandomPolicy
from fauxcall.testing import CompareOptions
from fauxcall.timefmt import TimeFormat

from fauxcall_everything.client_tools import CLIENT_TIMEOUT

if TYPE_CHECKING:
    from fauxcall.types import JsonObject

ALL_CAPABILITIES: Final[JsonObject] = {
    "sampling": {},
    "elicitation": {"form": {}, "url": {}},
    "roots": {"listChanged": True},
    "tasks": {"requests": {"sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
}
"""Everything a client can declare: every conditional tool is listed."""
ISO_MS: Final = TimeFormat(kind="iso", fraction_digits=3, tz_style="Z")
TASK_TIMES: Final = [
    NowPolicy(path=path, scope="body", format=ISO_MS)
    for path in ("$.createdAt", "$.lastUpdatedAt", "$.task.createdAt", "$.task.lastUpdatedAt")
]
"""Task timestamps follow each side's clock (compared within the clock tolerance)."""
LISTED_TASK_TIMES: Final = [
    NowPolicy(path=f"$.tasks[*].{name}", scope="body", format=ISO_MS)
    for name in ("createdAt", "lastUpdatedAt")
]
TIMEOUT: Final = CLIENT_TIMEOUT


def made_now(*paths: str) -> CompareOptions:
    """Texts that hold the time they were made (``created at 5:14:53 PM``): compared by length.

    A scenario runs on the real server, then on the fake: the seconds differ.
    """
    return CompareOptions(
        policies=[RandomPolicy(path=path, scope="body", generator="alnum") for path in paths]
    )


def session_text(*paths: str) -> CompareOptions:
    """Texts that name the client's session id (made up by each server): compared by length."""
    return made_now(*paths)


def ignoring(*paths: str) -> CompareOptions:
    """Values made up at random on each side (random log levels …): not compared."""
    return CompareOptions(policies=[IgnorePolicy(path=path, scope="body") for path in paths])


def text_reply(text: str, model: str = "claude-test", stop: str = "endTurn") -> JsonObject:
    """A client's sampling answer."""
    return {
        "role": "assistant",
        "model": model,
        "stopReason": stop,
        "content": {"type": "text", "text": text},
    }
