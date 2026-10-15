# FauxCall Reference Market API — FauxCall fake `refapi`

Source: http://127.0.0.1:9100/docs (swagger_ui) · real API: http://127.0.0.1:9100

## Coverage

- endpoints: 21 (replay 1, list 1, custom 17, computed 1, websocket 1)
- recordings: 859 exchanges
- collections: exchanges, tickers, quotes, watchlists, uploads; relations: 3
- field policies: random 21, calendar 1, now 17
- jobs: 0 · calendars: 6 · rate limits: 1 · error templates: 28

## Parity

Compliance **100.00%** (threshold 98%) — PASS.
Independent of recordings: **100.00%** over 168 of 230 calls.

## Open items

- none in the model
- GET /healthz: generic replay
- GET /v1/exchanges: generic list
