# Testing: unit tests and the ≥ 98 % parity gate

Two suites, both generated as a starting point by `fauxcall scaffold` and grown by you:

- **Unit** (`tests/unit/`, offline, ≥ 95 % coverage of the fake's package): app boot, every recorded
  stateless request replayed against the fake (`test_recorded.py`), and tests for every custom handler.
- **End-to-end** (`tests/e2e/`): scenarios that run against the fake (default), the real API
  (`--target=real`) or both with comparison (`--target=parity`).

## Contents

- [Writing scenarios](#writing-scenarios)
- [What to cover](#what-to-cover)
- [Running parity](#running-parity)
- [Reading the report](#reading-the-report)
- [Unit tests for handlers](#unit-tests-for-handlers)

## Writing scenarios

Write each scenario once; the `api` fixture talks to whichever side is under test and logs every call.

```python
async def test_list_then_get_every_item(api: ApiClient) -> None:
    listing = await api.get("/api/v1/Books")
    assert listing.status_code == 200
    for book in listing.json()[:5]:
        detail = await api.get(f"/api/v1/Books/{book['id']}")
        assert detail.json()["title"] == book["title"]  # consistency, on both sides
```

- Assertions should hold for the real API **and** the fake (they check behavior, not recorded values).
- Parity compares the two runs call by call: status, content type, contract headers, body shape,
  values per field policy (time fields by behavior, random/volatile by type, pseudonyms via the key).
- `compare=CompareOptions(values=False)` for calls whose values legitimately differ (a shared sandbox's
  live listing); `lengths=False` when list lengths differ by design; `policies=[…]` for extra per-call
  field policies.
- Personal data in inputs: `api.pii("johndoe", "username")` gives the real value on the real API and
  its pseudonym on the fake.
- Writes against the real API need `--allow-real-writes` (and the user's approval); always clean up in
  the scenario (`try/finally`) or in a fixture.
- MCP: the `mcp` fixture (`await mcp.call_tool("ask_question", {...})`, `await mcp.list_tools()`).

## What to cover

Aim for dozens of scenarios, per endpoint and across endpoints:

1. Happy paths with realistic variations of every parameter.
2. Every error the real API produces (missing/invalid parameters, unknown ids, invalid bodies,
   wrong methods, unknown routes, auth missing/invalid/denied).
3. Cross-endpoint consistency: list → detail; every reference resolves; aggregates match the data;
   writes visible (or not) in reads exactly as on the real API.
4. Time: "now" fields close to the response time; market fields during/outside sessions (assert
   relative to the clock, not to fixed values); relative fields keep their distance.
5. Pagination: walk all pages; totals; cursors; limit bounds.
6. Async jobs: submit, poll until terminal, then use the result (with timeouts generous enough for the
   real API).
7. Streams/WebSockets: event types, ordering, framing.
8. Rate limits (only on the fake unless approved) and fault-injection behavior via `/__fauxcall`.

## Running parity

```bash
fauxcall verify <name>                                 # unit + e2e on the fake, with coverage
fauxcall verify <name> --target parity                 # e2e on both sides + compliance report
fauxcall verify <name> --target parity --allow-real-writes
```

The run fails below the threshold (`--parity-threshold`, default 0.98). The report is written to
`data/reports/parity.md` and `.json`. Calls to the real API are spaced to the model's strictest rate
limit (record `rate_limits` before running parity), and a `429` is waited out and retried, so scenarios
compare real answers rather than throttling; test rate limiting itself on the fake.

## Reading the report

For every failed check: decide whether the fake or the test is wrong.

| failure | usual fix |
|---|---|
| `status` | missing error case/template, wrong role, wrong `on_invalid`, routing quirk |
| `shape` | wrong extract/projection/envelope; a field the real API adds (write `defaults`) |
| `content-type` | `analyze endpoint --apply` not run, or a template's `content_type` |
| value of a time field | missing/wrong policy or format; wrong calendar/zone |
| value of a static field | stale world (re-record, rebuild) or a shared sandbox (use `values=False`, say why) |
| `call made` | the fake run stopped early (an exception or a skipped write) |

Explain every remaining failure in `fauxcall progress note`; the user sees them in the final report.

## Unit tests for handlers

Test custom handlers in-process (fast, offline), e.g.:

```python
async def test_latest_converts_base(client: httpx2.AsyncClient) -> None:
    body = (await client.get("/v1/latest", params={"base": "USD"})).json()
    assert body["base"] == "USD"
    assert body["rates"]["EUR"] == pytest.approx(1 / 1.1206, rel=1e-4)
```

Use `Settings(fast=True)` and drive time through the admin API (`POST /__fauxcall/clock`) instead of
sleeping.
