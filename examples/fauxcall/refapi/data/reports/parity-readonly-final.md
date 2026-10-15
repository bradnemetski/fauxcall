# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 66 scenarios.

Independent compliance **100%** over 118 of 169 calls. The others repeat recorded requests (51), which the fake answers from those recordings, or compare shape only (0).

Not covered by any scenario: `POST /v1/watchlists`, `PUT /v1/watchlists/{id}`, `PATCH /v1/watchlists/{id}`, `DELETE /v1/watchlists/{id}`, `POST /v1/uploads`, `DELETE /v1/uploads/{id}`.

| endpoint | compliance | weight |
|---|---|---|
| `GET /V1/tickers` | 100.00% | 11.5/11.5 |
| `GET /healthz` | 100.00% | 20.0/20.0 |
| `GET /nope` | 100.00% | 11.5/11.5 |
| `GET /openapi.json` | 100.00% | 102.0/102.0 |
| `GET /v1` | 100.00% | 11.5/11.5 |
| `GET /v1/Tickers/AAPL` | 100.00% | 11.5/11.5 |
| `GET /v1/exchanges` | 100.00% | 202.5/202.5 |
| `GET /v1/exchanges/{code}` | 100.00% | 157.5/157.5 |
| `GET /v1/nothing-here` | 100.00% | 23.0/23.0 |
| `GET /v1/search` | 100.00% | 424.0/424.0 |
| `GET /v1/stream/trades` | 100.00% | 145.0/145.0 |
| `GET /v1/tickers` | 100.00% | 1805.5/1805.5 |
| `GET /v1/tickers/{symbol}` | 100.00% | 263.0/263.0 |
| `GET /v1/tickers/{symbol}/quote` | 100.00% | 205.0/205.0 |
| `GET /v1/time` | 100.00% | 36.0/36.0 |
| `GET /v1/uploads` | 100.00% | 10.0/10.0 |
| `GET /v1/uploads/{id}` | 100.00% | 34.5/34.5 |
| `GET /v1/uploads/{id}/content` | 100.00% | 11.5/11.5 |
| `GET /v1/watchlists` | 100.00% | 20.0/20.0 |
| `GET /v1/watchlists/{id}` | 100.00% | 93.0/93.0 |
| `GET /v1x` | 100.00% | 11.5/11.5 |
| `HEAD /healthz` | 100.00% | 6.0/6.0 |
| `HEAD /v1/exchanges` | 100.00% | 6.0/6.0 |
| `HEAD /v1/time` | 100.00% | 6.0/6.0 |
| `OPTIONS /healthz` | 100.00% | 23.0/23.0 |
| `OPTIONS /openapi.json` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/tickers` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/uploads/x` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/watchlists` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/watchlists/5` | 100.00% | 11.5/11.5 |
