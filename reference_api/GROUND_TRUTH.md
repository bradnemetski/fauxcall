# Ground truth: FauxCall Reference Market API 1.0.0

This document specifies exactly how the reference API behaves. FauxCall records this API like any
third-party API, generates a fake, and runs parity tests real-vs-fake; **parity is judged against
this document**. Every rule here is asserted by `reference_api/tests`, and the exchange, holiday
and ticker tables below are checked against the code by `tests/test_refapi_catalog.py`.

Contents: [conventions](#1-conventions) · [request pipeline](#2-request-pipeline) ·
[authentication](#3-authentication) · [rate limiting](#4-rate-limiting) · [latency](#5-latency) ·
[errors](#6-errors) · [exchanges and calendars](#7-exchanges-and-trading-calendars) ·
[tickers](#8-tickers) · [quotes](#9-quotes) · [server time](#10-server-time) ·
[search](#11-search) · [watchlists](#12-watchlists) · [uploads](#13-uploads) ·
[SSE trades](#14-server-sent-events-trades) · [WebSocket quotes](#15-websocket-quotes) ·
[health](#16-health) · [MCP](#17-mcp-server) · [OpenAPI](#18-openapi) ·
[configuration](#19-configuration) · [not part of the contract](#20-not-part-of-the-contract)

## 1. Conventions

* **Base URL**: `http://127.0.0.1:9100` by default.
* **"now"** is the server's clock (system time in production, an injected clock in tests). All
  instants are computed in integer nanoseconds and rendered in **UTC**.
* **JSON** responses are compact (no whitespace), UTF-8 with non-ASCII characters emitted raw
  (`"Moët"`, never `"Moët"`), `Content-Type: application/json`. Object keys appear in the
  order documented here.
* **Paths are exact**: there are no trailing-slash redirects (`/v1/tickers/` is a 404).
* **Timestamp formats** (fractions are always *truncated*, never rounded):

| Format | Shape | Example | Used by |
|---|---|---|---|
| ISO-6 | `YYYY-MM-DDTHH:MM:SS.ffffffZ` | `2026-10-09T20:00:00.000000Z` | quote `as_of` (REST, WebSocket, MCP) |
| ISO-7 (.NET) | `YYYY-MM-DDTHH:MM:SS.fffffffZ` | `2026-10-11T01:17:26.8440216Z` | `GET /v1/time` `utc` |
| ISO-3 | `YYYY-MM-DDTHH:MM:SS.fffZ` | `2026-10-09T15:00:00.123Z` | watchlist `created_at`/`updated_at`, SSE trade `time` |
| Python isoformat | `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00` (fraction omitted when the microseconds are 0) | `2026-10-09T15:00:00+00:00`, `2026-10-09T15:00:00.123456+00:00` | upload `created_at`/`completed_at` |
| ISO-0 | `YYYY-MM-DDTHH:MM:SSZ` | `2026-10-09T20:00:00Z` | MCP `market_status` instants |

## 2. Request pipeline

Every HTTP request whose path is `/v1` or starts with `/v1/` goes through these steps **in this
order**; the first step that fails produces the response:

1. **Latency**: sleep for the path's fixed latency ([section 5](#5-latency)).
2. **Authentication**: `X-API-Key` ([section 3](#3-authentication)) - 401 or 403.
3. **Rate limit**: count the request against the key's window ([section 4](#4-rate-limiting)) - 429.
4. **Read-only check**: `POST`/`PUT`/`PATCH`/`DELETE` with a read-only key - 403.
5. **Routing**: unknown path - 404 `not_found`; known path, wrong method - 405
   `method_not_allowed`.
6. **Validation and the handler**: parameter/body errors - 400; resource errors - 404/409/413.

Consequences: an unauthenticated request to an unknown `/v1` path is a 401 (not a 404); a
read-only key's `DELETE` on an unknown watchlist is a 403 (not a 404); a malformed JSON body sent
without a key is a 401.

All other paths (`/healthz`, `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/mcp`, and any
unknown path outside `/v1`) skip steps 1-4 entirely: no latency, no key, no rate limit.

## 3. Authentication

The key is sent in the `X-API-Key` request header (case-sensitive value).

| Key | Access |
|---|---|
| `demo-key` | full: every method |
| `readonly-key` | `GET`, `HEAD`, `OPTIONS` only; `POST`/`PUT`/`PATCH`/`DELETE` - 403 `read_only_key` |
| `revoked-key` | always 403 `api_key_revoked` |

| Situation | Status | `code` | `message` |
|---|---|---|---|
| header absent, or present but empty | 401 | `missing_api_key` | `Missing API key. Send it in the X-API-Key header.` |
| any other unknown value (keys are case-sensitive: `DEMO-KEY` is unknown) | 401 | `invalid_api_key` | `Invalid API key.` |
| `revoked-key` | 403 | `api_key_revoked` | `This API key has been revoked.` |
| `readonly-key` + write method | 403 | `read_only_key` | `This API key is read-only; <METHOD> requests are not allowed.` |

401 and revoked-403 responses carry **no** rate-limit headers; the read-only 403 does (the key is
valid and the request was counted). There is no `WWW-Authenticate` header.

The WebSocket authenticates with the `api_key` query parameter instead
([section 15](#15-websocket-quotes)). The MCP endpoint has **no authentication**.

## 4. Rate limiting

* Per **API key**, at most **30 requests per window of 10 seconds**.
* **Fixed window, started by the first request**: a key's window begins at the instant of its
  first request after the previous window ended, and ends exactly 10 s later. A request at or after
  the end instant starts a new window.
* Every request that passes authentication is counted, **including** requests that end in 400,
  403 (read-only), 404, 405, 409 or 413. **Rejected (429) requests are not counted** and do not
  extend the window. Unauthenticated (401) and revoked-key (403) requests are never counted.
* Every counted or rejected response carries:

| Header | Value |
|---|---|
| `X-RateLimit-Limit` | `30` |
| `X-RateLimit-Remaining` | `30 - (requests counted in the current window, including this one)`; `0` on a 429 |
| `X-RateLimit-Reset` | `ceil((window_start + 10 s) / 1 s)`: integer epoch **seconds** (rounded up) when the window ends |
| `Retry-After` (429 only) | `max(1, ceil((window_start + 10 s - now) / 1 s))`: integer seconds |

* The 31st request in a window: **429**, `{"error": {"code": "rate_limited", "message": "Rate limit
  exceeded: 30 requests per 10 seconds.", "request_id": "..."}}`.
* Example: first request at `t = 1000.3 s` (epoch). Window `[1000.3, 1010.3)`; every response in
  it has `X-RateLimit-Reset: 1011`. A rejected request at `t = 1004.8` gets `Retry-After: 6`; at
  `t = 1005.3`, `Retry-After: 5`. A request at `t = 1010.3` opens a new window (`Remaining: 29`,
  `Reset: 1021`).
* Not rate limited: anything outside `/v1`, the WebSocket and MCP.

## 5. Latency

Each `/v1` HTTP request sleeps for a fixed, deterministic duration **before authentication**, so
401, 404 and 429 responses are delayed too:

```
latency_ms(method, path) = 15 + H("refapi-latency:" + METHOD + " " + path) mod 46     # 15..60 ms
H(text) = first 8 bytes of SHA-256(UTF-8 text) as a big-endian unsigned integer
```

`path` is the request path exactly as received, without the query string (so
`/v1/tickers/AAPL` and `/v1/tickers/aapl` differ). Examples:

| Method | Path | Latency (ms) |
|---|---|---|
| GET | /v1/exchanges | 33 |
| GET | /v1/tickers | 41 |
| GET | /v1/tickers/AAPL | 30 |
| GET | /v1/tickers/AAPL/quote | 29 |
| GET | /v1/search | 39 |
| GET | /v1/time | 18 |
| POST | /v1/watchlists | 24 |
| GET | /v1/watchlists | 46 |
| POST | /v1/uploads | 15 |
| GET | /v1/stream/trades | 48 |

Measured latency is this value plus processing/network time. Latency does not apply to paths
outside `/v1`, to the WebSocket or to MCP. It is disabled with `REFAPI_LATENCY=false` or
`--no-latency`.

## 6. Errors

Every error (4xx/5xx) has the body

```json
{"error": {"code": "<snake_case>", "message": "<text>", "request_id": "<uuid4>"}}
```

with a fourth key, `"param"` (always last), only for errors about one parameter. `request_id` is a
fresh random UUID4 per request.

| Status | `code` | `param` | `message` | When |
|---|---|---|---|---|
| 400 | `missing_parameter` | name | `Missing required parameter '<name>'.` | required query/body/form field absent |
| 400 | `invalid_parameter` | name | `Invalid value for parameter '<name>': <rule>.` | schema validation failed (rules below) |
| 400 | `invalid_parameter` | `exchange` | `Unknown exchange '<value>'.` | ticker list filter |
| 400 | `invalid_parameter` | `sector` | `Unknown sector '<value>'.` | ticker list filter |
| 400 | `invalid_cursor` | `cursor` | `Invalid or unknown cursor.` | ticker list |
| 400 | `invalid_json` | - | `Request body is not valid JSON.` | JSON content type, unparsable body |
| 400 | `invalid_body` | - | `Request body must be a JSON object.` | body missing, not an object, or not sent as JSON |
| 400 | `unknown_symbol` | `symbols` | `Unknown symbol '<value>'.` | watchlist bodies |
| 400 | `unknown_symbol` | `symbol` | `Unknown symbol '<value>'.` | `GET /v1/stream/trades` |
| 401 | `missing_api_key` / `invalid_api_key` | - | see [section 3](#3-authentication) | |
| 403 | `api_key_revoked` / `read_only_key` | - | see [section 3](#3-authentication) | |
| 404 | `not_found` | - | `No route matches <METHOD> <path>.` | no route for the path |
| 404 | `exchange_not_found` | - | `No exchange with code '<CODE>'.` (upper-cased) | |
| 404 | `ticker_not_found` | - | `No ticker with symbol '<SYMBOL>'.` (upper-cased) | |
| 404 | `watchlist_not_found` | - | `No watchlist with id <id>.` | |
| 404 | `upload_not_found` | - | `No upload with id '<id>'.` | |
| 405 | `method_not_allowed` | - | `Method <METHOD> is not allowed for <path>.` | plus `Allow` header |
| 409 | `not_ready` | - | `Upload '<id>' is <status>; content is available only when status is done.` | |
| 413 | `payload_too_large` | - | `File exceeds the 1 MiB (1048576 bytes) limit.` | |
| 429 | `rate_limited` | - | `Rate limit exceeded: 30 requests per 10 seconds.` | |
| 500 | `internal_error` | - | `An unexpected error occurred.` | never expected |

**Validation rules.** FastAPI's 422 never appears; schema validation failures are 400. Only the
**first** failure is reported, in the order path parameters, query parameters (declaration
order), body. `<rule>` depends only on the parameter (any kind of failure):

| Parameter | `<rule>` |
|---|---|
| `limit` (`GET /v1/tickers`) | `must be an integer between 1 and 100` |
| `count` (`GET /v1/stream/trades`) | `must be an integer between 1 and 50` |
| `q` (`GET /v1/search`) | `must be a string of 2 to 64 characters` |
| `id` (watchlist paths) | `must be an integer` |
| `name` (watchlist bodies) | `must be a string of 1 to 50 characters` |
| `symbols` (watchlist bodies) | `must be an array of strings` |
| `file` (`POST /v1/uploads`) | `must be a file` |
| `label` (`POST /v1/uploads`) | `must be a string` |

For body fields `param` is the top-level field name (an invalid `symbols[0]` reports `symbols`).
JSON bodies must be sent with a JSON content type (`application/json` or `*+json`); a body sent
without one is `invalid_body`.

**405.** `Allow` lists every method the path accepts, in the order `GET, HEAD, POST, PUT, PATCH,
DELETE, OPTIONS`, e.g. `PUT /v1/watchlists` - `Allow: GET, POST`; `POST /v1/watchlists/1001` -
`Allow: GET, PUT, PATCH, DELETE`. `HEAD` and `OPTIONS` are not supported on the API routes or
`/healthz` (405 with that path's `Allow` list, e.g. `OPTIONS /v1/watchlists` - `Allow: GET, POST`);
a `HEAD` response has no body. The framework pages `/openapi.json` and `/docs` do answer `HEAD`.

## 7. Exchanges and trading calendars

### Exchanges

| code | name | mic | timezone | currency | sessions (local) |
|---|---|---|---|---|---|
| XHKG | Hong Kong Stock Exchange | XHKG | Asia/Hong_Kong | HKD | 09:30-12:00, 13:00-16:00 |
| XLON | London Stock Exchange | XLON | Europe/London | GBP | 08:00-16:30 |
| XNAS | Nasdaq Stock Market | XNAS | America/New_York | USD | 09:30-16:00 |
| XNYS | New York Stock Exchange | XNYS | America/New_York | USD | 09:30-16:00 |
| XPAR | Euronext Paris | XPAR | Europe/Paris | EUR | 09:00-17:30 |
| XTKS | Tokyo Stock Exchange | XTKS | Asia/Tokyo | JPY | 09:00-11:30, 12:30-15:30 |

### Holidays (2026)

| exchange | 2026 holidays (MM-DD) |
|---|---|
| XHKG | 01-01, 02-17, 02-18, 02-19, 04-03, 04-06, 04-07, 05-01, 05-25, 06-19, 07-01, 10-01, 10-19, 12-25, 12-28 |
| XLON | 01-01, 04-03, 04-06, 05-04, 05-25, 08-31, 12-25, 12-28 |
| XNAS | 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25 |
| XNYS | 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25 |
| XPAR | 01-01, 04-03, 04-06, 05-01, 12-25 |
| XTKS | 01-01, 01-02, 01-12, 02-11, 02-23, 03-20, 04-29, 05-04, 05-05, 05-06, 07-20, 08-11, 09-21, 09-22, 09-23, 10-12, 11-03, 11-23, 12-31 |

NYSE/Nasdaq use the real 2026 NYSE calendar; the others are plausible. Holiday data exists for
**2026 only**: in any other year only weekends are non-trading days (so 2025-12-31 traded and
2027-01-01 would trade).

### Early closes (2026)

| exchange | date | close (local) |
|---|---|---|
| XNYS | 2026-11-27 | 13:00 |
| XNYS | 2026-12-24 | 13:00 |
| XNAS | 2026-11-27 | 13:00 |
| XNAS | 2026-12-24 | 13:00 |

### Session semantics

* A **trading day** is a Monday-Friday exchange-local date that is not a holiday.
* Each trading day has the sessions listed above. A session is the half-open interval
  **`[open, close)`** in exchange-local wall-clock time, converted to UTC with the IANA time zone
  (DST included). At the exact open instant the market is open; at the exact close instant it is
  already closed.
* On an early-close day every session is cut at the early-close time (a session that would start
  at or after it is dropped): NYSE on 2026-11-27 trades 09:30-13:00 New York time.
* **open** while some session contains now; otherwise **closed** (lunch breaks are closed).
* **last close**: the latest session close `<=` now (if now is exactly a close, that close).
* **next open**: the earliest session open strictly `>` now (while open, the next session's open).

Examples (UTC): Saturday 2026-10-10 - XNYS last close `2026-10-09T20:00:00Z`; Thanksgiving
2026-11-26 - XNYS last close `2026-11-25T21:00:00Z` (EST); 2026-11-27 14:00 New York - last close
`2026-11-27T18:00:00Z`; Tokyo 2026-10-09 12:00 JST (lunch) - last close `2026-10-09T02:30:00Z`,
next open `2026-10-09T03:30:00Z`; Saturday 2026-10-10 - XTKS next open `2026-10-13T00:00:00Z`
(Monday 10-12 is a holiday).

### Endpoints

* `GET /v1/exchanges` - 200, a **bare array** of every exchange sorted by `code`:
  `{"code", "name", "mic", "timezone", "currency", "sessions": [{"open": "HH:MM", "close": "HH:MM"}]}`.
  `sessions` are the regular sessions (early closes are not shown).
* `GET /v1/exchanges/{code}` - 200 with one exchange; `code` is case-insensitive (`xlon` works and
  returns `"code": "XLON"`); unknown - 404 `exchange_not_found`.

## 8. Tickers

### Tickers

| symbol | name | exchange | currency | sector | listed_on | price |
|---|---|---|---|---|---|---|
| 0005.HK | HSBC Holdings plc | XHKG | HKD | Financials | 1991-04-01 | 68.35 |
| 0388.HK | Hong Kong Exchanges and Clearing Limited | XHKG | HKD | Financials | 2000-06-27 | 298.40 |
| 0700.HK | Tencent Holdings Limited | XHKG | HKD | Communication Services | 2004-06-16 | 412.60 |
| 0981.HK | Semiconductor Manufacturing International | XHKG | HKD | Semiconductors | 2004-03-18 | 23.85 |
| 1211.HK | BYD Company Limited | XHKG | HKD | Automotive | 2002-07-31 | 287.20 |
| 6758.T | Sony Group Corporation | XTKS | JPY | Technology | 1958-12-01 | 2862.50 |
| 6861.T | Keyence Corporation | XTKS | JPY | Industrials | 1987-10-21 | 65890.00 |
| 7203.T | Toyota Motor Corporation | XTKS | JPY | Automotive | 1949-05-16 | 2741.50 |
| 7267.T | Honda Motor Co., Ltd. | XTKS | JPY | Automotive | 1957-12-01 | 1563.00 |
| 8035.T | Tokyo Electron Limited | XTKS | JPY | Semiconductors | 1980-10-01 | 24315.00 |
| 8306.T | Mitsubishi UFJ Financial Group, Inc. | XTKS | JPY | Financials | 2001-04-02 | 1687.50 |
| 9984.T | SoftBank Group Corp. | XTKS | JPY | Communication Services | 1994-07-22 | 8954.00 |
| 9988.HK | Alibaba Group Holding Limited | XHKG | HKD | Consumer Discretionary | 2019-11-26 | 83.15 |
| AAPL | Apple Inc. | XNAS | USD | Technology | 1980-12-12 | 227.48 |
| ADBE | Adobe Inc. | XNAS | USD | Technology | 1986-08-20 | 512.37 |
| AIR.PA | Airbus SE | XPAR | EUR | Industrials | 2000-07-10 | 142.36 |
| AMD | Advanced Micro Devices, Inc. | XNAS | USD | Semiconductors | 1972-09-27 | 158.92 |
| AMGN | Amgen Inc. | XNAS | USD | Healthcare | 1983-06-17 | 301.15 |
| AMZN | Amazon.com, Inc. | XNAS | USD | Consumer Discretionary | 1997-05-15 | 186.51 |
| AVGO | Broadcom Inc. | XNAS | USD | Semiconductors | 2009-08-06 | 171.24 |
| AZN.L | AstraZeneca PLC | XLON | GBP | Healthcare | 1999-04-06 | 118.46 |
| BA | The Boeing Company | XNYS | USD | Industrials | 1962-01-02 | 154.20 |
| BAC | Bank of America Corporation | XNYS | USD | Financials | 1973-09-04 | 39.68 |
| BARC.L | Barclays PLC | XLON | GBP | Financials | 1985-05-01 | 2.41 |
| BNP.PA | BNP Paribas SA | XPAR | EUR | Financials | 1993-10-18 | 61.74 |
| BP.L | BP p.l.c. | XLON | GBP | Energy | 1954-03-01 | 4.12 |
| CAT | Caterpillar Inc. | XNYS | USD | Industrials | 1929-12-02 | 352.17 |
| COST | Costco Wholesale Corporation | XNAS | USD | Consumer Staples | 1985-12-05 | 893.40 |
| CSCO | Cisco Systems, Inc. | XNAS | USD | Technology | 1990-02-16 | 54.83 |
| CVX | Chevron Corporation | XNYS | USD | Energy | 1921-06-24 | 147.55 |
| DIS | The Walt Disney Company | XNYS | USD | Communication Services | 1957-11-12 | 92.46 |
| F | Ford Motor Company | XNYS | USD | Automotive | 1956-03-07 | 10.58 |
| GM | General Motors Company | XNYS | USD | Automotive | 2010-11-18 | 47.91 |
| GOOGL | Alphabet Inc. Class A | XNAS | USD | Communication Services | 2004-08-19 | 163.24 |
| GS | The Goldman Sachs Group, Inc. | XNYS | USD | Financials | 1999-05-04 | 497.30 |
| HSBA.L | HSBC Holdings plc | XLON | GBP | Financials | 1992-07-10 | 7.08 |
| INTC | Intel Corporation | XNAS | USD | Semiconductors | 1971-10-13 | 22.71 |
| JNJ | Johnson & Johnson | XNYS | USD | Healthcare | 1944-09-25 | 158.74 |
| JPM | JPMorgan Chase & Co. | XNYS | USD | Financials | 1969-03-05 | 211.42 |
| KO | The Coca-Cola Company | XNYS | USD | Consumer Staples | 1919-09-05 | 69.83 |
| MC.PA | LVMH Moët Hennessy Louis Vuitton SE | XPAR | EUR | Consumer Discretionary | 1987-06-03 | 652.30 |
| META | Meta Platforms, Inc. | XNAS | USD | Communication Services | 2012-05-18 | 572.81 |
| MSFT | Microsoft Corporation | XNAS | USD | Technology | 1986-03-13 | 415.26 |
| NFLX | Netflix, Inc. | XNAS | USD | Communication Services | 2002-05-23 | 701.05 |
| NVDA | NVIDIA Corporation | XNAS | USD | Semiconductors | 1999-01-22 | 131.60 |
| PEP | PepsiCo, Inc. | XNAS | USD | Consumer Staples | 1972-06-01 | 168.32 |
| PFE | Pfizer Inc. | XNYS | USD | Healthcare | 1944-01-03 | 28.97 |
| PYPL | PayPal Holdings, Inc. | XNAS | USD | Financials | 2015-07-20 | 78.64 |
| QCOM | QUALCOMM Incorporated | XNAS | USD | Semiconductors | 1991-12-13 | 165.09 |
| RIVN | Rivian Automotive, Inc. | XNAS | USD | Automotive | 2021-11-10 | 11.87 |
| RR.L | Rolls-Royce Holdings plc | XLON | GBP | Industrials | 2011-05-23 | 5.37 |
| SAN.PA | Sanofi | XPAR | EUR | Healthcare | 1999-08-20 | 91.48 |
| SHEL.L | Shell plc | XLON | GBP | Energy | 2005-07-20 | 26.89 |
| TSLA | Tesla, Inc. | XNAS | USD | Automotive | 2010-06-29 | 249.83 |
| TSM | Taiwan Semiconductor Manufacturing Co., Ltd. | XNYS | USD | Semiconductors | 1997-10-08 | 186.12 |
| TTE.PA | TotalEnergies SE | XPAR | EUR | Energy | 1973-10-25 | 58.92 |
| ULVR.L | Unilever PLC | XLON | GBP | Consumer Staples | 1930-01-02 | 47.15 |
| V | Visa Inc. | XNYS | USD | Financials | 2008-03-19 | 281.66 |
| WFC | Wells Fargo & Company | XNYS | USD | Financials | 1978-01-03 | 58.21 |
| XOM | Exxon Mobil Corporation | XNYS | USD | Energy | 1920-03-31 | 117.93 |

Invariants: 60 tickers; every `exchange` is an exchange `code`; `currency` always equals that
exchange's currency; sectors are exactly Automotive, Communication Services, Consumer
Discretionary, Consumer Staples, Energy, Financials, Healthcare, Industrials, Semiconductors,
Technology. Symbols are matched case-insensitively everywhere and always returned upper-case.
Symbols sort by plain code-point order (digits before letters, `.` before both: `BA` < `BAC` <
`BARC.L`).

### `GET /v1/tickers`

Query: `exchange` (code, case-insensitive), `sector` (case-insensitive exact name), `limit`
(integer 1-100, default 20), `cursor`. Empty values (`?exchange=`) are treated as absent.

Response 200: `{"data": [ticker...], "next_cursor": "<cursor>" | null, "total": <int>}` where a
ticker is `{"symbol", "name", "exchange", "currency", "sector", "listed_on": "YYYY-MM-DD"}` (the
price is not part of it).

1. Validate `limit` (400 `invalid_parameter`), then `exchange` (400 `Unknown exchange '<value>'.`),
   then `sector` (400 `Unknown sector '<value>'.`), then `cursor` (400 `invalid_cursor`).
2. Filter by exchange and sector; `total` = number of matches (independent of the cursor).
3. Sort by symbol; keep symbols strictly greater than the cursor's `after` symbol.
4. `data` = the first `limit` of those; `next_cursor` is the cursor for the last item of `data`
   when more items remain, else `null`.

**Cursor**: base64url **without padding** of the compact JSON `{"after":"<SYMBOL>"}`, e.g.
`{"after":"AAPL"}` - `eyJhZnRlciI6IkFBUEwifQ`. Input cursors may be padded and may contain
whitespace in the JSON. A cursor is invalid (400 `invalid_cursor`) unless it decodes to a JSON
object whose `after` is a string naming an existing ticker (case-insensitive; it need not match
the current filters).

### `GET /v1/tickers/{symbol}`

200 with the ticker (canonical upper-case symbol); unknown - 404 `ticker_not_found`.

## 9. Quotes

`GET /v1/tickers/{symbol}/quote` - 200:

```json
{"symbol":"AAPL","price":"227.48","currency":"USD","market_state":"open","as_of":"2026-10-09T14:59:58.623456Z"}
```

* `price`: the ticker's price from the table, as a string with exactly 2 decimals. It **never
  changes**.
* `market_state`: `open` or `closed` per the ticker's exchange calendar
  ([section 7](#7-exchanges-and-trading-calendars)).
* `as_of` (ISO-6): while open, **now - 1.5 s** (truncated to microseconds; right after an open it
  can precede the open); while closed, the exchange's **last close** (`...:00.000000Z`). Weekends,
  holidays, lunch breaks and early closes are all respected.
* Unknown symbol - 404 `ticker_not_found`.

## 10. Server time

`GET /v1/time` - 200 `{"utc": "<ISO-7>", "unix_ms": <int>, "zone": "UTC"}`: always the actual
current time (one clock reading for both fields; `unix_ms` = floor(ns / 1e6)), e.g.
`{"utc":"2026-10-11T01:17:26.8440216Z","unix_ms":1791681446844,"zone":"UTC"}`.

## 11. Search

`GET /v1/search?q=<text>` - 200 `{"query": "<q exactly as sent>", "results": [{"symbol", "name",
"score"}]}`. `q` is required (400 `missing_parameter`) and must be 2-64 characters (400
`invalid_parameter`).

* **Tokens**: lower-case the text, then take runs of `[a-z0-9]` (anything else separates tokens,
  including non-ASCII letters: `Moët` - `mo`, `t`). No tokens - empty results.
* **Per-token score** for a ticker (the highest applicable):

| Score | Rule |
|---|---|
| 1.0 | token equals the symbol's root: the part before the first `.`, lower-cased (`7203` for `7203.T`, `aapl` for `AAPL`) |
| 0.9 | token has 2+ characters and is a prefix of the symbol's root |
| 0.8 | token equals a word (token) of the name |
| 0.6 | token has 3+ characters and is a prefix of a word of the name |
| 0.5 | token equals a word of the sector, or is a synonym of the ticker's sector |
| 0.0 | otherwise |

* **Ticker score** = mean of its per-token scores, rounded **half-up to 3 decimals**, emitted as a
  JSON number (`0.5`, `0.433`, `0.063`, `1.0`).
* Tickers scoring 0 are dropped; the rest are sorted by score descending, then symbol ascending;
  the first **10** are returned.
* **Synonyms** (token - sector): car, cars, auto, autos, automaker, vehicle, vehicles, ev -
  Automotive; chip, chips, chipmaker, semis, semiconductor - Semiconductors; bank, banks, banking,
  finance, fintech, payments - Financials; oil, gas, petroleum - Energy; pharma, drug, drugs,
  biotech, medicine - Healthcare; tech, software, cloud - Technology; food, beverage, beverages,
  grocery - Consumer Staples; retail, ecommerce, luxury - Consumer Discretionary; media, social,
  streaming, telecom - Communication Services; aerospace, aircraft, machinery - Industrials.

Examples: `q=bank` - BAC 0.8 (name word), then 0005.HK, 0388.HK, 8306.T, BARC.L, BNP.PA, GS,
HSBA.L, JPM, PYPL at 0.5. `q=Toyota car` - 7203.T 0.65, then 1211.HK, 7267.T, F, GM, RIVN, TSLA at
0.25.

## 12. Watchlists

In-memory, persisted for the life of the process, **shared by all keys**, never reset.

Representation: `{"id": <int>, "name": "<str>", "symbols": ["<SYMBOL>"...], "created_at":
"<ISO-3>", "updated_at": "<ISO-3>"}`.

* **Ids** start at **1001** and increase by 1 per create; deleted ids are never reused.
* **Symbols** are canonicalised (upper-case), duplicates removed keeping the first occurrence,
  order otherwise kept: `["aapl","MSFT","AAPL"]` - `["AAPL","MSFT"]`. The first unknown symbol (in
  request order) fails the request: 400 `unknown_symbol`, `param: "symbols"`.
* `name`: string of 1-50 characters (whitespace counts). Unknown body fields are ignored.

| Request | Success | Notes |
|---|---|---|
| `POST /v1/watchlists` `{"name", "symbols"?}` | **201** + `Location: /v1/watchlists/<id>` | `symbols` defaults to `[]`; `created_at` = `updated_at` = now |
| `GET /v1/watchlists` | 200, bare array sorted by id | |
| `GET /v1/watchlists/{id}` | 200 | |
| `PUT /v1/watchlists/{id}` `{"name", "symbols"}` | 200 | both required; replaces both; `updated_at` = now |
| `PATCH /v1/watchlists/{id}` `{"name"?, "symbols"?}` | 200 | present fields replace (symbols are replaced, not appended); `null` - 400; `{}` is allowed; `updated_at` = now even if nothing changed |
| `DELETE /v1/watchlists/{id}` | **204**, empty body | |

Unknown id - 404 `watchlist_not_found` (checked before symbols, after body validation). A
non-integer id - 400 `invalid_parameter` `param: "id"`. Writes with `readonly-key` - 403.

## 13. Uploads

Asynchronous jobs whose status is a pure function of the time elapsed since creation. In-memory,
shared by all keys.

Representation (keys in this order; `completed_at` and `error` are **absent**, not null, until
applicable):

```json
{"id":"<uuid4>","status":"processing","filename":"notes.txt","size":11,"label":null,"created_at":"2026-10-09T15:00:00+00:00","progress":50}
```

**`POST /v1/uploads`** (multipart/form-data): part `file` (required, must be a file part) and
optional text part `label`.

* Missing `file` (also when the body is not multipart) - 400 `missing_parameter` `param: "file"`;
  `file` sent as a text field - 400 `invalid_parameter` (`must be a file`).
* File content larger than **1,048,576 bytes** - **413** `payload_too_large` (exactly 1 MiB is
  accepted).
* Success: **202** with `status: "queued"`, `progress: 0`, `size` = byte length, `filename` as
  sent by the client (`""` if empty), `label` or `null`, `created_at` = now (Python isoformat).
  The part's `Content-Type` is stored (default `application/octet-stream`) but not shown.

**Status at elapsed time `e` = now - created:**

| `e` | `status` | `progress` | extra keys |
|---|---|---|---|
| `0 <= e < 2.0 s` | `queued` | 0 | |
| `2.0 s <= e < 8.0 s` | `processing` | `floor((e - 2 s) * 100 / 6 s)`: 0 at 2.0 s, 50 at 5.0 s, 99 just before 8.0 s | |
| `e >= 8.0 s`, file bytes contain ASCII `CORRUPT` (case-sensitive) | `failed` | 100 | `completed_at` = created + 8 s, `error: "corrupt_file"` |
| `e >= 8.0 s`, otherwise | `done` | 100 | `completed_at` = created + 8 s |

* `GET /v1/uploads` - 200, bare array, **newest first** (ties on `created_at`: most recently
  created first).
* `GET /v1/uploads/{id}` - 200; unknown - 404 `upload_not_found`.
* `GET /v1/uploads/{id}/content` - only when `done`: 200 with the original bytes, `Content-Type`
  exactly as uploaded (no charset added), `Content-Length`, and `Content-Disposition: attachment;
  filename="<name>"` where every character outside printable ASCII (0x20-0x7E), and `"` and `\`,
  becomes `_`. Queued, processing or failed - 409 `not_ready`.
* `DELETE /v1/uploads/{id}` - **204**, empty body; afterwards every endpoint for that id is 404.

## 14. Server-Sent Events: trades

`GET /v1/stream/trades?symbol=<symbol>&count=<n>`; `symbol` required (case-insensitive; unknown
- 400 `unknown_symbol` `param: "symbol"`), `count` integer 1-50, default 10.

Response 200, `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache`, plus
the rate-limit headers. The body is `count` events, then the stream ends:

```
id: 1
event: trade
data: {"symbol":"AAPL","price":"227.50","size":189,"time":"2026-10-09T15:00:00.000Z"}

```

* Lines end with `\n`; each event ends with a blank line; no comments, no `retry:`.
* **Timing**: event 1 is sent immediately, then one event every **200 ms**; the stream closes
  right after the last event (total about `(count - 1) * 200 ms`). `time` (ISO-3) is the clock when
  the event is produced.
* **Deterministic walk** (identical for every request, independent of time): with `p0` = the
  quote price and `h_n = H("refapi-trade:" + SYMBOL + ":" + n)` (H as in
  [section 5](#5-latency)):
  `p_n = round_half_up(p_{n-1} * (10000 + (h_n mod 21) - 10) / 10000, 2 decimals)` (each step is
  -0.10%..+0.10%), `size_n = 1 + (h_n >> 16) mod 1000`.
* AAPL's first five trades: `227.50`/189, `227.50`/321, `227.59`/705, `227.70`/528, `227.54`/275.

## 15. WebSocket quotes

`WS /v1/ws/quotes?api_key=<key>`. The server always accepts the handshake, then:

* **Auth** (query parameter only; the `X-API-Key` header is ignored): missing/empty, unknown or
  revoked key - the server immediately closes with code **4401** and reason `Missing api_key query
  parameter.` / `Invalid API key.` / `This API key has been revoked.`. `readonly-key` is allowed.
  No rate limiting, no latency.
* **Client messages** are JSON text frames, handled one at a time:

| Client sends | Server replies |
|---|---|
| `{"action": "ping"}` | `{"type":"pong"}` |
| `{"action": "subscribe", "symbols": [...]}` (non-empty array of known symbols) | `{"type":"subscribed","symbols":[canonical, de-duplicated, in order]}`, then the quote stream below |
| text that is not JSON | `{"type":"error","message":"Invalid JSON."}` |
| JSON that is not an object | `{"type":"error","message":"Messages must be JSON objects."}` |
| other/missing `action` | `{"type":"error","message":"Unknown action: <action as JSON>."}` (e.g. `Unknown action: "dance".`, `Unknown action: null.`) |
| `symbols` missing, empty, not an array, or with a non-string | `{"type":"error","message":"symbols must be a non-empty array of strings."}` |
| an unknown symbol | `{"type":"error","message":"Unknown symbol '<as sent>'."}` (first unknown) |
| a binary frame | `{"type":"error","message":"Binary messages are not supported."}` |

  Errors keep the connection open.
* **Quote stream** (after `subscribed`): 5 rounds; round 1 immediately, then one round every
  **250 ms**. Each round sends, for each subscribed symbol in order,
  `{"type":"quote","symbol":"AAPL","price":"227.48","as_of":"<ISO-6>"}` with the same semantics as
  the REST quote (all symbols in a round use one clock reading). After round 5 the server sends
  `{"type":"done"}` and closes normally (**1000**). Messages sent by the client during the stream
  are not processed.

## 16. Health

`GET /healthz` - 200 `{"status":"ok"}`. No auth, no rate limit, no latency.

## 17. MCP server

Streamable HTTP at **`/mcp`** exactly (`/mcp/` is a 404), built with the official `mcp` Python SDK
v2 (`MCPServer`) with the SDK's default transport settings: SSE-framed POST responses, stateful
sessions (`Mcp-Session-Id`) for the 2025-11-25 initialize handshake, and the 2026-07-28 protocol
(clients negotiating either work). **No authentication, no rate limit, no latency**;
DNS-rebinding (Host/Origin) checks are disabled.

* Server info: name `fauxcall-reference-api`, title `FauxCall Reference Market MCP`, version
  `1.0.0`; instructions: `FauxCall reference market-data server with deterministic test data. Use
  search_tickers to find symbols, get_quote for a ticker's quote (static price; the timestamp
  follows the exchange's trading sessions), market_status for whether an exchange is open with its
  next open and last close, and explain_ticker for a short templated answer about a ticker.
  Exchange details are resources at exchange://{code} (XHKG, XLON, XNAS, XNYS, XPAR, XTKS).`
* All tools have annotations `readOnlyHint: true`, `idempotentHint: true`, `openWorldHint: false`
  and an output schema. Successful results carry `structuredContent` and one text block: the JSON
  of the structured content indented by 2 spaces (for `explain_ticker`: `structuredContent` is
  `{"result": "<paragraph>"}` and the text block is the paragraph). A failed call returns
  `isError: true` with one text block `Error executing tool <name>: <message>`.

| Tool | Arguments | Result | Errors (`<message>`) |
|---|---|---|---|
| `get_quote` | `symbol` (string) | exactly the REST quote object | `Unknown symbol '<as sent>'.` |
| `search_tickers` | `query` (string), `limit` (integer, default 5, schema min 1 max 10) | `{"query", "results"}` as REST search, truncated to `limit` | `query must be 2 to 64 characters.`; `limit must be between 1 and 10.` |
| `market_status` | `exchange` (string, case-insensitive) | `{"exchange", "name", "timezone", "market_state", "session_closes_at", "next_open", "last_close"}` (ISO-0; `session_closes_at` is the current session's close, `null` when closed) | `Unknown exchange '<as sent>'.` |
| `explain_ticker` | `symbol`, `question` (strings) | a paragraph (below) | `Unknown symbol '<as sent>'.` |

Missing or mistyped arguments are rejected by the SDK's argument validation (`isError: true`;
that message text is not part of the contract).

**`explain_ticker`** answers `"<intro> <detail>"`, a function of the ticker and the question's
**topic only** (the question is never echoed). Topic: the first of these whose keywords contain a
token of the question (tokens as in search), else `general`: `price` (price, cost, worth, value,
valuation, expensive, cheap), `risk` (risk, risky, volatile, volatility, safe, safety), `listing`
(listed, listing, ipo, exchange, history, since, public), `business` (business, sector, industry,
products, do, does, make).

* intro: `<name> (<SYMBOL>) is listed on the <exchange name> (<CODE>) in the <sector> sector and
  trades in <currency>.`
* price: `Its reference price is <price> <currency>; this API never changes prices, so every quote
  reports the same value.`
* risk: `As a <sector> stock it carries sector-specific risk; simulated trades move at most 0.10%
  per trade around <price> <currency>.`
* listing: `It has been listed since <listed_on> and trades during <CODE> sessions (<sessions>
  <timezone>).` with sessions like `09:00-11:30 and 12:30-15:30`.
* business: `Its business is classified in the <sector> sector; search for '<sector>' to find its
  peers.`
* general: `Ask about its price, risk, listing or business for more detail.`

**Resource template** `exchange://{code}` (name `exchange`, title `Exchange`, MIME type
`application/json`): one text content holding the compact JSON of `GET /v1/exchanges/{code}`;
case-insensitive. Unknown code - JSON-RPC error **-32602** `Unknown exchange '<as sent>'.`. There
are no static resources.

**Prompt** `analyze_ticker` (argument `symbol`, required; title `Analyze a ticker`): one `user`
message, `Analyze <name> (<SYMBOL>), listed on the <exchange name> (<CODE>) in the <sector>
sector. Call get_quote for its quote and market_status for <CODE> trading hours, then summarize
price, market state and sector context in three bullet points.` Unknown symbol - JSON-RPC error
**-32602** `Unknown symbol '<as sent>'.`.

## 18. OpenAPI

`GET /openapi.json` (no auth): OpenAPI 3.1, title `FauxCall Reference Market API`, version
`1.0.0`. It documents every REST endpoint (the WebSocket and MCP cannot be expressed and are
described in `info.description`), the security scheme `ApiKeyAuth` (`apiKey` in header
`X-API-Key`, required on every `/v1` operation), the `ErrorEnvelope`/`ErrorDetail` schemas on every
4xx response (always `application/json`, never 422), the `X-RateLimit-*` headers on every `/v1`
response except 401 (plus `Retry-After` on 429, `Location` on the watchlist 201). Swagger UI is at
`/docs`; there is no ReDoc page.

## 19. Configuration

Environment variables (`REFAPI_` prefix) or CLI flags; defaults are the ground truth above.

| Variable | CLI | Default |
|---|---|---|
| `REFAPI_HOST` | `--host` | `127.0.0.1` |
| `REFAPI_PORT` | `--port` | `9100` |
| `REFAPI_LATENCY` | `--no-latency` | `true` |
| `REFAPI_LOG_LEVEL` | `--log-level` | `INFO` |
| `REFAPI_API_KEYS` (JSON object key - `full`/`readonly`/`revoked`) | - | `{"demo-key":"full","readonly-key":"readonly","revoked-key":"revoked"}` |
| `REFAPI_RATE_LIMIT_REQUESTS` | - | `30` |
| `REFAPI_RATE_LIMIT_WINDOW_SECONDS` | - | `10` |

## 20. Not part of the contract

`request_id` and upload `id` values (random UUID4s), the exact wall-clock latency on top of the
artificial delay, log output, response headers not named in this document (`date`, `server`,
`content-length` except on downloads), the text of SDK-generated MCP argument-validation errors,
and 500 responses (which should never happen).
