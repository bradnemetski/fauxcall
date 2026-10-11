from __future__ import annotations

from fauxcall.config import FaultRule
from fauxcall.runtime.faults import FaultInjector

PETS = "GET /v1/pets/{petId}"
ORDERS = "POST /v1/orders"


def fired(
    injector: FaultInjector, calls: int, *, endpoint: str = PETS, session: str = "s"
) -> list[int]:
    """1-based call numbers that got a fault."""
    return [n for n in range(1, calls + 1) if injector.pick(endpoint, session) is not None]


def test_no_rules_no_faults() -> None:
    assert fired(FaultInjector([], seed=0), 5) == []


def test_glob_matching_on_the_endpoint_id() -> None:
    rule = FaultRule(endpoint="GET /v1/pets/*", status=503)
    injector = FaultInjector([rule], seed=0)
    assert injector.pick(PETS, "s") is rule
    assert injector.pick(ORDERS, "s") is None
    assert injector.pick("get /v1/pets/{petId}", "s") is None  # case-sensitive
    assert FaultInjector([FaultRule()], seed=0).pick(ORDERS, "s") is not None  # default "*"


def test_first_n() -> None:
    injector = FaultInjector([FaultRule(first_n=2)], seed=0)
    assert fired(injector, 6) == [1, 2]


def test_every_nth() -> None:
    injector = FaultInjector([FaultRule(every_nth=3)], seed=0)
    assert fired(injector, 10) == [3, 6, 9]


def test_first_n_and_every_nth_combine() -> None:
    injector = FaultInjector([FaultRule(first_n=6, every_nth=2)], seed=0)
    assert fired(injector, 12) == [2, 4, 6]


def test_probability_is_deterministic_per_seed_session_and_count() -> None:
    rule = FaultRule(probability=0.3)
    first = fired(FaultInjector([rule], seed=42), 300)
    again = fired(FaultInjector([rule], seed=42), 300)
    other_seed = fired(FaultInjector([rule], seed=43), 300)
    other_session = fired(FaultInjector([rule], seed=42), 300, session="t")
    assert first == again
    assert first != other_seed
    assert first != other_session
    assert 0.2 < len(first) / 300 < 0.4


def test_probability_extremes() -> None:
    assert fired(FaultInjector([FaultRule(probability=0.0)], seed=1), 50) == []
    assert len(fired(FaultInjector([FaultRule(probability=1.0)], seed=1), 50)) == 50


def test_session_filter_and_per_session_counts() -> None:
    injector = FaultInjector([FaultRule(session="a", first_n=1)], seed=0)
    assert fired(injector, 3, session="b") == []
    assert fired(injector, 3, session="a") == [1]
    shared = FaultInjector([FaultRule(first_n=1)], seed=0)
    assert fired(shared, 2, session="a") == [1]
    assert fired(shared, 2, session="b") == [1]  # counts are per session


def test_first_matching_rule_wins_but_every_matching_rule_counts() -> None:
    once = FaultRule(first_n=1, status=500)
    even = FaultRule(every_nth=2, status=502)
    injector = FaultInjector([once, even], seed=0)
    assert injector.pick(PETS, "s") is once  # `even` counted its 1st call too
    assert injector.pick(PETS, "s") is even  # `even`'s 2nd call
    assert injector.pick(PETS, "s") is None
    assert injector.pick(PETS, "s") is even


def test_when_several_rules_fire_the_first_is_used() -> None:
    first, second = FaultRule(status=500), FaultRule(status=502, first_n=1)
    injector = FaultInjector([first, second], seed=0)
    assert injector.pick(PETS, "s") is first
    injector.replace([second])
    assert injector.pick(PETS, "s") is second  # counting restarted


def test_rules_is_a_copy_and_replace_restarts_counting() -> None:
    rule = FaultRule(first_n=1)
    injector = FaultInjector([rule], seed=0)
    injector.rules.clear()
    assert injector.rules == [rule]
    assert fired(injector, 2) == [1]
    replacement = FaultRule(first_n=1, status=418)
    injector.replace([replacement])
    assert injector.rules == [replacement]
    assert injector.pick(PETS, "s") is replacement
    injector.replace([])
    assert injector.pick(PETS, "s") is None


def test_reset_counts_for_one_session_or_all() -> None:
    injector = FaultInjector([FaultRule(first_n=1)], seed=0)
    assert fired(injector, 2, session="a") == [1]
    assert fired(injector, 2, session="b") == [1]
    injector.reset_counts("a")
    assert fired(injector, 1, session="a") == [1]
    assert fired(injector, 1, session="b") == []
    injector.reset_counts()
    assert fired(injector, 1, session="b") == [1]
