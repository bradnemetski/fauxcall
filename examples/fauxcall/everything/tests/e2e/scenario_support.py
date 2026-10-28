"""Shared helpers for the Everything server scenarios: comparison policies and assertions.

Values the server makes up or reads from its clock differ between the two sides by nature: task
ids and times, elicitation ids, and the local time of day embedded in dynamic resources. These
policies compare them by behavior (a time near the answer's, an id of the same shape) and leave
every other value compared exactly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from fauxcall.model.fields import IgnorePolicy, NowPolicy, RandomPolicy
from fauxcall.testing import CompareOptions
from fauxcall.timefmt import TimeFormat

if TYPE_CHECKING:
    from fauxcall.types import JsonObject, JsonValue

ISO_MS: Final = TimeFormat(kind="iso", fraction_digits=3, tz_style="Z")
NEW_YORK: Final = ZoneInfo("America/New_York")
LOCAL_TIME: Final = re.compile(r"(\d{1,2}):(\d{2}):(\d{2}) (AM|PM)$")


def task_policies(prefix: str) -> list[NowPolicy | RandomPolicy]:
    """Policies for one task object at ``prefix`` (``$.task``, ``$``, ``$.tasks[*]``)."""
    return [
        NowPolicy(path=f"{prefix}.createdAt", scope="body", format=ISO_MS),
        NowPolicy(path=f"{prefix}.lastUpdatedAt", scope="body", format=ISO_MS),
        RandomPolicy(path=f"{prefix}.taskId", scope="body", generator="hex", length=32),
    ]


def task_compare(prefix: str = "$") -> CompareOptions:
    """Compare a task answer: ids by shape, times by distance to the answer."""
    return CompareOptions(policies=list(task_policies(prefix)))


def ignoring(*paths: str) -> CompareOptions:
    """Compare everything except these body paths (live text checked by the scenario itself)."""
    return CompareOptions(policies=[IgnorePolicy(path=path, scope="body") for path in paths])


ELICITATION_ID: Final = CompareOptions(
    policies=[
        RandomPolicy(path="$.data.elicitations[*].elicitationId", scope="body", generator="uuid4")
    ]
)


def text_of(result: JsonValue, index: int = 0) -> str:
    """The text of a tool result's content block."""
    assert isinstance(result, dict), result
    content = result["content"]
    assert isinstance(content, list)
    block = content[index]
    assert isinstance(block, dict)
    return str(block["text"])


def assert_recent_local_time(text: str) -> None:
    """The text ends with a time of day near now on the server's clock (America/New_York)."""
    match = LOCAL_TIME.search(text)
    assert match is not None, text
    hour, minute, second, half = match.groups()
    now = datetime.now(NEW_YORK)
    hour24 = int(hour) % 12 + (12 if half == "PM" else 0)
    stamp = now.replace(hour=hour24, minute=int(minute), second=int(second), microsecond=0)
    drift = abs((now - stamp).total_seconds())
    assert min(drift, 86400 - drift) < 120, (text, now.isoformat())


def assert_task(task: JsonValue, status: str) -> JsonObject:
    """A task object as the server shows it, in the given status."""
    assert isinstance(task, dict), task
    assert re.fullmatch(r"[0-9a-f]{32}", str(task["taskId"])), task
    assert task["status"] == status, task
    assert task["ttl"] == 300000
    assert task["pollInterval"] == 1000
    created = datetime.fromisoformat(str(task["createdAt"]))
    assert abs((datetime.now(UTC) - created).total_seconds()) < 600
    return task
