"""Everything a request handler needs, built once per app."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fauxcall_reference_api.catalog import Catalog, default_catalog
from fauxcall_reference_api.ratelimit import RateLimiter
from fauxcall_reference_api.state import UploadStore, WatchlistStore

if TYPE_CHECKING:
    from fauxcall_reference_api.clock import Clock, Sleep
    from fauxcall_reference_api.settings import Settings


@dataclass(slots=True)
class AppContext:
    """Configuration, injected time, seed data and in-memory state."""

    settings: Settings
    clock: Clock
    sleep: Sleep
    catalog: Catalog = field(default_factory=default_catalog)
    watchlists: WatchlistStore = field(default_factory=WatchlistStore)
    uploads: UploadStore = field(default_factory=UploadStore)
    limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.limiter = RateLimiter(
            limit=self.settings.rate_limit_requests,
            window_seconds=self.settings.rate_limit_window_seconds,
        )

    def now_ns(self) -> int:
        """The current instant from the injected clock."""
        return self.clock.now_ns()
