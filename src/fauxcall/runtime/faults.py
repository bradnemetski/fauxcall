"""Deterministic fault injection: errors, timeouts, delays, disconnects and 429s on demand.

Rules come from settings (``FAUXCALL_FAULTS`` / ``fauxcall.toml``) and can be replaced at runtime
through the admin API. Whether a call is hit depends only on the seed, the session, the rule and the
call count — so an evaluation that fails at the 3rd call fails at the 3rd call every time.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fauxcall.config import FaultRule


class FaultInjector:
    """Decides which calls get a fault."""

    def __init__(self, rules: list[FaultRule], seed: int) -> None:
        self._rules = list(rules)
        self._seed = seed
        self._counts: defaultdict[tuple[int, str], int] = defaultdict(int)

    @property
    def rules(self) -> list[FaultRule]:
        """The active rules (a copy)."""
        return list(self._rules)

    def replace(self, rules: list[FaultRule]) -> None:
        """Swap the active rules and restart call counting."""
        self._rules = list(rules)
        self._counts.clear()

    def reset_counts(self, session: str | None = None) -> None:
        """Forget call counts (for one session or all)."""
        if session is None:
            self._counts.clear()
            return
        for key in [k for k in self._counts if k[1] == session]:
            del self._counts[key]

    def _roll(self, index: int, session: str, count: int) -> float:
        digest = hashlib.sha256(f"{self._seed}:{session}:{index}:{count}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def pick(self, endpoint_id: str, session: str) -> FaultRule | None:
        """The first rule that fires for this call (counts every matching rule)."""
        chosen: FaultRule | None = None
        for index, rule in enumerate(self._rules):
            if rule.session is not None and rule.session != session:
                continue
            if not fnmatch.fnmatchcase(endpoint_id, rule.endpoint):
                continue
            self._counts[index, session] += 1
            count = self._counts[index, session]
            if rule.first_n is not None and count > rule.first_n:
                continue
            if rule.every_nth is not None and count % rule.every_nth != 0:
                continue
            if rule.probability < 1.0 and self._roll(index, session, count) >= rule.probability:
                continue
            if chosen is None:
                chosen = rule
        return chosen
