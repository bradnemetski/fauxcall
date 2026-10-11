# FauxCall reference API

A deliberately rich, **deterministic** market-data API that serves as the ground truth for
validating FauxCall end-to-end: FauxCall records it like any third-party API, generates a fake, and
runs parity tests real-vs-fake. Because we control it, every behavior is specified precisely in
**[GROUND_TRUTH.md](GROUND_TRUTH.md)**; that document, not the code, is what parity is judged
against.

It is a *real* API, not a fake, and it does not depend on the `fauxcall` package.

## What it exercises

| Feature | Where |
|---|---|
| API-key auth (full / read-only / revoked keys) | `X-API-Key` on every `/v1` route |
| Error envelopes for every error (400 instead of 422, 404, 405 with `Allow`) | everywhere |
| Rate limiting (30 per 10 s per key) with `X-RateLimit-*` and `Retry-After` | `/v1` |
| Deterministic per-path latency (15-60 ms) | `/v1` |
| Cross-endpoint consistency (tickers reference exchanges, quotes use exchange calendars) | exchanges, tickers, quotes |
| Market-hours semantics: time zones, DST, lunch breaks, holidays, early closes | quotes, MCP `market_status` |
| Volatile vs. frozen values (static prices, live timestamps) | quotes, `/v1/time` |
| Cursor pagination and filters | `GET /v1/tickers` |
| Function-like endpoint with free text | `GET /v1/search`, MCP `explain_ticker` |
| Stateful CRUD with ids, `Location`, timestamps | watchlists |
| Async jobs whose state derives from elapsed time; multipart upload; binary download | uploads |
| Server-Sent Events | `GET /v1/stream/trades` |
| WebSocket | `WS /v1/ws/quotes` |
| MCP server (tools, resource template, prompt) over Streamable HTTP | `/mcp` |
| Several timestamp formats (3, 6 and 7 fractional digits, Python `isoformat`) | throughout |

## Run it

From the repository root (the API is a uv workspace member):

```bash
uv run --package fauxcall-reference-api fauxcall-reference-api --port 9100
# or, once installed in the workspace environment (uv sync --all-packages):
uv run fauxcall-reference-api --port 9100
```

Options: `--host` (default `127.0.0.1`), `--port` (default `9100`), `--no-latency`,
`--log-level`. Every setting can also come from the environment with the `REFAPI_` prefix (e.g.
`REFAPI_LATENCY=false`, `REFAPI_API_KEYS='{"ci-key": "full"}'`); see
[GROUND_TRUTH.md section 19](GROUND_TRUTH.md#19-configuration).

```bash
curl -s localhost:9100/healthz
curl -s localhost:9100/v1/tickers/AAPL/quote -H 'X-API-Key: demo-key'
curl -s 'localhost:9100/v1/search?q=chips' -H 'X-API-Key: demo-key'
curl -sN 'localhost:9100/v1/stream/trades?symbol=AAPL&count=5' -H 'X-API-Key: demo-key'
```

Swagger UI: <http://127.0.0.1:9100/docs>, OpenAPI: <http://127.0.0.1:9100/openapi.json>, MCP
endpoint: `http://127.0.0.1:9100/mcp`.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | no auth |
| GET | `/v1/exchanges` | bare array |
| GET | `/v1/exchanges/{code}` | |
| GET | `/v1/tickers` | `exchange`, `sector`, `limit`, `cursor` |
| GET | `/v1/tickers/{symbol}` | case-insensitive |
| GET | `/v1/tickers/{symbol}/quote` | static price, session-aware `as_of` |
| GET | `/v1/search?q=` | top 10 by score |
| GET | `/v1/time` | actual current time |
| POST / GET | `/v1/watchlists` | 201 + `Location` |
| GET / PUT / PATCH / DELETE | `/v1/watchlists/{id}` | |
| POST / GET | `/v1/uploads` | multipart, 202, max 1 MiB |
| GET / DELETE | `/v1/uploads/{id}` | status derived from elapsed time |
| GET | `/v1/uploads/{id}/content` | only when `done` (else 409) |
| GET | `/v1/stream/trades` | SSE |
| WS | `/v1/ws/quotes?api_key=` | quote feed |
| POST / GET / DELETE | `/mcp` | MCP Streamable HTTP (no auth) |

## Develop

```bash
uv run pytest -p no:cacheprovider reference_api/tests --cov=fauxcall_reference_api --cov-report=term-missing
uv run ruff check reference_api && uv run ruff format --check reference_api
uv run mypy reference_api/src reference_api/tests
```

The tests inject a `ManualClock` (`create_app(settings, clock=clock, sleep=clock.sleep)`): time
only moves when a test says so, and sleeps (latency, SSE and WebSocket pacing) advance the clock
instantly, so every timing rule is checked exactly and the suite runs in seconds. They also keep
GROUND_TRUTH.md honest: its exchange, holiday, early-close and ticker tables are compared with the
seed data.

Layout (`src/fauxcall_reference_api/`):

| Module | Responsibility |
|---|---|
| `app.py` | `create_app()`: routers, MCP mount, error handlers, gateway |
| `gateway.py` | ASGI middleware: latency, auth, rate limit, read-only check, access log |
| `errors.py` | error envelope, validation-error mapping, 404/405 handling |
| `catalog.py` | seed data: exchanges with 2026 calendars, 60 tickers, search synonyms |
| `market.py` | trading-session arithmetic (open/closed, last close, next open) |
| `search.py`, `simulation.py` | search scoring; deterministic latency and trade walk |
| `state.py` | in-memory watchlists and time-derived upload jobs |
| `views.py`, `schemas.py` | response models shared by REST, WebSocket and MCP |
| `routes/` | REST, SSE and WebSocket endpoints |
| `mcp_server.py` | the MCP server (official `mcp` SDK v2) |
| `openapi.py` | OpenAPI refinements (400 not 422, rate-limit headers) |
| `clock.py`, `timefmt.py` | injectable time and exact timestamp rendering |
| `settings.py`, `logs.py`, `main.py` | configuration, logging, CLI |
