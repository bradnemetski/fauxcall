# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 65 scenarios.

Independent compliance **100%** over 168 of 230 calls. The others repeat recorded requests (62), which the fake answers from those recordings, or compare shape only (0).

| endpoint | compliance | weight |
|---|---|---|
| `DELETE /v1/uploads/{id}` | 100.00% | 29.5/29.5 |
| `DELETE /v1/watchlists/{id}` | 100.00% | 53.0/53.0 |
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
| `GET /v1/tickers/{symbol}` | 100.00% | 293.0/293.0 |
| `GET /v1/tickers/{symbol}/quote` | 100.00% | 233.0/233.0 |
| `GET /v1/time` | 100.00% | 36.0/36.0 |
| `GET /v1/uploads` | 100.00% | 27.0/27.0 |
| `GET /v1/uploads/{id}` | 100.00% | 98.0/98.0 |
| `GET /v1/uploads/{id}/content` | 100.00% | 48.5/48.5 |
| `GET /v1/watchlists` | 100.00% | 56.0/56.0 |
| `GET /v1/watchlists/{id}` | 100.00% | 166.5/166.5 |
| `GET /v1x` | 100.00% | 11.5/11.5 |
| `HEAD /v1/exchanges` | 100.00% | 6.0/6.0 |
| `HEAD /v1/time` | 100.00% | 6.0/6.0 |
| `OPTIONS /v1/tickers` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/uploads/x` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/watchlists` | 100.00% | 11.5/11.5 |
| `OPTIONS /v1/watchlists/5` | 100.00% | 11.5/11.5 |
| `PATCH /v1/watchlists/{id}` | 100.00% | 81.0/81.0 |
| `POST /v1/uploads` | 100.00% | 117.5/117.5 |
| `POST /v1/watchlists` | 100.00% | 210.5/210.5 |
| `PUT /v1/watchlists/{id}` | 100.00% | 38.5/38.5 |
