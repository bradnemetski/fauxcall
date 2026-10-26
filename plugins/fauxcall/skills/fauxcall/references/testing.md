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
- Ids the server makes up in a write (`id`, `…_id`, `…Id`, `uuid`, `random`/`counter` fields) are
  compared by type, and later calls of the scenario are compared through them: the fake's
  `No watchlist with id 1001.` matches the real `… id 1009.` when the scenario created both. No
  `values=False` is needed for that.
- Polling is fine: identical reads in a row (waiting for a job) count once, with their last answer,
  however many times each side polled. Streams (SSE) are compared by event names, data shapes and
  counts, not by their live values.
- `compare=CompareOptions(values=False)` for calls whose values legitimately differ (a shared sandbox's
  live listing); `lengths=False` when list lengths differ by design; `policies=[…]` for extra per-call
  field policies.
- Parse timestamps, never cut them at a fixed length: many APIs drop trailing zeros from
  fractions (`13.0478700Z` comes back as `13.04787Z`), so a slice that works most runs fails on the
  ones that end in zero.
- Personal data in inputs: `api.pii("johndoe", "username")` gives the real value on the real API and
  its pseudonym on the fake.
- Writes against the real API need `--allow-real-writes` and spend the user's up-front approval
  (`data/approvals.json`) exactly as recording does: each write uses one call of the budget covering
  its endpoint, and a write no approval covers (or whose budget is used up) skips the scenario.
  Budget your writes across recording *and* parity runs; always clean up in the scenario
  (`try/finally`) or in a fixture.
- MCP: the `mcp` fixture (`await mcp.call_tool("ask_question", {...})`, `await mcp.list_tools()`).
  For tools that ask the client (sampling, elicitation, roots), open the session first with
  `await mcp.open(capabilities={...}, replies={method: result})`; for what the server sends on
  its own, `await mcp.listen()` then `await mcp.heard(method, count=…)`, and `progress=True` on
  calls that report progress; see `mcp.md`.
- `fauxcall verify <fake> --fake-url http://127.0.0.1:<port>` runs the scenarios against a fake
  that is already running (`fauxcall up`), e.g. to check it as clients will reach it.

## What to cover

Aim for dozens of scenarios, per endpoint and across endpoints:

1. Happy paths with realistic variations of every parameter, **new** ones too: a request copied
   from a recording mostly checks the recording against itself (the fake answers it from that
   recording), so the report counts those calls apart. Vary ids, dates, queries and arguments.
2. Every error the real API produces (missing/invalid parameters, unknown ids, invalid bodies,
   wrong methods, unknown routes, auth missing/invalid/denied).
3. Cross-endpoint consistency: list → detail; every reference resolves; aggregates match the data;
   writes visible (or not) in reads exactly as on the real API. For every write that can be undone,
   a full cycle: create → read it back → update or delete → read again (with
   `--allow-real-writes` and the parity budget), so the fake's state is compared, not just answers.
4. Time: "now" fields close to the response time; market fields during/outside sessions (assert
   relative to the clock, not to fixed values); relative fields keep their distance.
5. Pagination: walk all pages; totals; cursors; limit bounds.
6. Async jobs: submit, poll until terminal, then use the result (with timeouts generous enough for the
   real API).
7. Streams/WebSockets: event types, ordering, framing.
8. Rate limits (only on the fake unless approved) and fault-injection behavior via `/__fauxcall`.
9. MCP: the listings (`tools/list`, and `resources/list`, `prompts/list` when the server has them)
   and every tool, plus a refused credential: one scenario per way to fail (no credential, a bad
   one) whenever the service takes credentials.

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

Next to the overall score, the report gives the **independent** score: compliance without the calls
that repeat a recorded request or compare shape only. If many calls are left out, the overall score
says little: write scenarios with new inputs until the independent score also clears the bar. The
report also lists what no scenario covered and whether any checks a refused credential; cover those
too. Tell the user both scores.

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

To test emulated latency, read the pauses the runtime took (`runtime_of_app(app).pauses`, the
latest ones in order) and assert only an upper bound: the fake waits for the recorded time *minus the time already spent* answering, so a pause can
be anything up to the sampled latency, or skipped when the answer already took that long.
`assert all(p <= max(recorded) for p in pauses)` is stable; a lower bound above zero or an exact
number of pauses fails whenever the machine is slow.

Shared fixtures go in `tests/conftest.py`. A `conftest.py` in `tests/unit` or in `tests/e2e` is fine,
but not in both: mypy sees two modules named `conftest`.
