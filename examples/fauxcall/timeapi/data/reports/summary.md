# Time API Docs — FauxCall fake `timeapi`

Source: https://timeapi.io/swagger/index.html (swagger_ui) · real API: https://timeapi.io

## Coverage

- endpoints: 28 (computed 25, replay 3)
- recordings: 576 exchanges
- collections: none; relations: 0
- field policies: now 28, random 17, computed 45, relative 2
- jobs: 0 · calendars: 0 · rate limits: 0 · error templates: 8

## Parity

Compliance **100.00%** (threshold 98%) — PASS.

## Open items

- none in the model

## Known gaps (from the build log and the independent review)

- **IP → zone**: no GeoIP database. 55 recorded addresses answer exactly; other public IPv4s
  borrow a recorded /24 or /16 neighbour, everything else (incl. IPv6) answers `UTC`.
- **Coordinates**: timezonefinder polygons ≈ the server's; one probed Antarctic sector differs and
  is overridden; other border points may differ.
- **tz database drift**: pinned to tzdata 2026.2 (Casablanca evidence). Edmonton/Casablanca e2e
  canaries flag a server upgrade. Europe/Dublin with negative DST *active* (after 2026-10-25) is
  modeled, not yet recorded.
- **Unrecorded, ASSUMED** (see model notes): body-binding errors (415, empty/malformed JSON,
  non-string fields), DST-gap handling with `earlier`/`later`, custom-calculation error order,
  translate for languages other than de/fr/ja/en/nl/es/it/pt/ko/th/fa.
- **Parity scope**: the final run (100%, 192 scenarios) covered GETs; the last all-endpoint run
  (incl. every POST) scored 99.96% with its one failure fixed. The approved POST budget is used up
  (converttimezone 20/20), so POST parity cannot be re-run without a new approval.
- Reports: `parity.md` (final), `parity-run2-with-posts.md`, `parity-run3.md`.
