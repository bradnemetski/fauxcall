"""How parity compares the values that differ between any two runs of the real server itself.

* texts naming the client's ``Mcp-Session-Id`` (the toggles' answers): same shape, same length;
* the time of day dynamic resources are stamped with (``created at 10:29:02 PM``), as text or
  base64: the real run and the fake run happen seconds apart;
* task timestamps: each close to its own response's time;
* the random ``elicitationId`` of a URL-elicitation prerequisite.

Task ids are learned by the parity harness from the creating call, so later calls compare
through them; they need nothing here.
"""

from __future__ import annotations

from typing import Final

from fauxcall.model.fields import NowPolicy, VolatilePolicy
from fauxcall.testing import CompareOptions
from fauxcall.timefmt import TimeFormat

ISO_MS: Final = TimeFormat(kind="iso", fraction_digits=3)


def _volatile(*paths: str) -> list[VolatilePolicy]:
    return [VolatilePolicy(path=path, scope="body") for path in paths]


def _now(*paths: str) -> list[NowPolicy]:
    return [NowPolicy(path=path, scope="body", format=ISO_MS) for path in paths]


SESSION_TEXT: Final = CompareOptions(policies=[*_volatile("$.content[*].text")])
"""A tool answer that names the session (toggle-simulated-logging, toggle-subscriber-updates)."""

STAMPED_REFERENCE: Final = CompareOptions(
    policies=[*_volatile("$.content[*].resource.text", "$.content[*].resource.blob")]
)
"""``get-resource-reference``: the embedded resource carries the time of day."""

STAMPED_READ: Final = CompareOptions(
    policies=[*_volatile("$.contents[*].text", "$.contents[*].blob")]
)
"""``resources/read`` of a dynamic resource."""

STAMPED_PROMPT: Final = CompareOptions(
    policies=[
        *_volatile("$.messages[*].content.resource.text", "$.messages[*].content.resource.blob")
    ]
)
"""``prompts/get`` of ``resource-prompt``: its embedded resource carries the time of day."""

CREATED_TASK: Final = CompareOptions(policies=[*_now("$.task.createdAt", "$.task.lastUpdatedAt")])
"""``tools/call`` with ``task``: a task created now."""

TASK: Final = CompareOptions(policies=[*_now("$.createdAt", "$.lastUpdatedAt")])
"""``tasks/get`` and ``tasks/cancel``."""

TASKS: Final = CompareOptions(policies=[*_now("$.tasks[*].createdAt", "$.tasks[*].lastUpdatedAt")])
"""``tasks/list``."""

PREREQUISITE: Final = CompareOptions(policies=[*_volatile("$.data.elicitations[*].elicitationId")])
"""``trigger-url-elicitation``'s -32042 error: a fresh prerequisite id each time."""
