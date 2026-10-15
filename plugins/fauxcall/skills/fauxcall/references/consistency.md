# Consistency: one world behind every endpoint

The fake serves a single **world**: collections of records (`data/world/<collection>.jsonl`) built
from recordings. Every endpoint reads and writes that world, so references always resolve: a ticker's
exchange exists in `/exchanges`; a created pet appears in `/pets/findByStatus`; an upload shows up in
its status endpoint. Each evaluation session gets its own copy-on-write view (`X-Fauxcall-Session`).

## Contents

- [Collections](#collections)
- [Build, check, repeat](#build-check-repeat)
- [Relations and closure](#relations-and-closure)
- [Both directions, and what writes do](#both-directions-and-what-writes-do)
- [Invariants and aggregates](#invariants-and-aggregates)
- [Async jobs](#async-jobs)
- [Expansion](#expansion)

## Collections

```json
"collections": [
  {"name": "pets", "key": "id", "key_type": "int",
   "sources": ["GET /api/v3/pet/findByStatus", "GET /api/v3/pet/{petId}"],
   "extract": {"GET /api/v3/pet/findByStatus": "$[*]", "GET /api/v3/pet/{petId}": "$"}},
  {"name": "tickers", "key": "symbol", "key_type": "str",
   "extract": {"GET /v1/tickers": "$.data[*]", "GET /v1/tickers/{symbol}": "$"}}
]
```

- `key` is the record's identifying field; `key_type` `int` | `str` | `uuid` (path values like `"42"`
  find integer keys).
- `extract` says where records sit in each source endpoint's response. The same entity seen from a
  list and from a detail endpoint must have **one** shape: if the list returns a summary, extract from
  the detail endpoint and give the list endpoint a `projection`.
- `purposes` (default: sample, variation, edge, pagination, time_profile, series) decide which
  recordings feed the world. Add `write_readback` / `job_poll` only when the real server keeps those
  resources (then they also exist in the real API during parity runs).
- `order`: `recorded` (default), `key_asc`, `key_desc`.
- A record is a JSON **object** holding its own key; an array at the extract path contributes its
  items. Maps keyed by id (`{"AUD": "Australian Dollar"}`, `{"rates": {"AUD": 1.62}}`) are not split
  into records: keep the map inside a larger record (one record per date with its whole `rates`
  map, key `date`) and answer lookups and conversions in a `computed` handler, or serve the map
  unchanged with `replay`.

## Build, check, repeat

```bash
fauxcall world build <name>     # writes data/world/*.jsonl, then checks relations/invariants
fauxcall world check <name>
```

A record seen several times keeps its first position and its freshest content.

## Relations and closure

```bash
fauxcall analyze relations <name>          # value-overlap candidates across endpoints
fauxcall analyze relations <name> --world  # … across built collections
```

Confirm candidates with the docs (coverage alone can mislead), then:

```json
"relations": [{"name": "ticker_exchange", "from_collection": "tickers", "from_field": "exchange",
               "to_collection": "exchanges", "evidence": "100% of 60 tickers; docs: 'MIC of the listing exchange'"}]
```

`from_field` may traverse arrays (`tags[*].id`); `to_field` defaults to the target key.
When `world check` reports dangling references, **record the missing entities** (plan a few more
calls for exactly those ids) and rebuild — never invent them, never delete the referencing records
to make the check pass (the real API would still return them). `required: false` only when the real
data genuinely contains dangling references (say so in `evidence`).

## Both directions, and what writes do

A relation is checked forward (every company's exchange exists) and, when you say so, in reverse
(every exchange has a company). Three rules per relation say how the real API keeps it; each
starts undecided (`null`), and `fauxcall model check` lists what is still undecided. Decide every
one before scaffolding, and say why in `rules_evidence`.

| rule | question | values |
|---|---|---|
| `min_per_target` | Must every target be referenced? | `1`: every author has a book, every exchange lists a company; `0`: a city needs no team — **unless it's a sports API**, where every city in it has a team |
| `on_missing` | A write pointing at a target that doesn't exist? | `refuse` (the endpoint's `missing_reference` error case, e.g. a 404 "author not found") or `accept` (stored anyway) |
| `on_delete` | Deleting a target that is still referenced? | `restrict` (the endpoint's `still_referenced` error case, e.g. 409), `cascade` (its referrers go too), `set_null` (references cleared; removed from lists), `ignore` (left dangling) |

```json
{"name": "book_author", "from_collection": "books", "from_field": "authorId", "to_collection": "authors",
 "min_per_target": 1, "on_missing": "refuse", "on_delete": "restrict",
 "evidence": "every book's authorId is in /authors (200 of 200)",
 "rules_evidence": "all 48 recorded authors have books, and the docs list authors 'with their books'; POST /books with authorId 999 got 404 (000311); DELETE /authors/3 with books got 409 (000315)"}
```

**Evidence for the reverse.** `fauxcall world check` prints each relation both ways
(`48 of 48 authors referenced (fewest referrers: 2)`); `analyze relations` shows the same for
candidates (`reverse`, `unreferenced`). Full coverage in a sample can be chance, and gaps can be
sampling: weigh it with what the domain says and the docs describe. In a sports API, a city exists
because a team plays there; in a weather API, cities exist on their own.

**Evidence for writes.** Record it, don't assume it: plan a create (or update) pointing at a target
that doesn't exist, and a delete of a target that is still referenced, with cleanup (see
[recording](recording.md#writes-read-backs-and-cleanup)). Then map the error cases:
`{"when": "missing_reference", "template": "author_not_found"}` on the write endpoint,
`{"when": "still_referenced", "template": "author_has_books"}` on the delete endpoint. Without a
recorded error, the fake answers a generic 422 or 409, which won't match the real API.

**What is checked when.** The world as built (and expanded) must satisfy both directions. After
writes, a world can't always (an author created through the API has no books yet, on the real
API too), so the fake's tests check, after every scenario, the references that writes keep (rules
`refuse` and `restrict`/`cascade`/`set_null`) and the invariants; a handler that leaves a dangling
reference or a duplicate fails the scenario. Custom handlers that write use `ctx.refuse_missing`
and `ctx.delete` to follow the same rules (see [model](model.md#custom-handlers)).

## Invariants and aggregates

```json
"invariants": [
  {"name": "unique_symbols", "kind": "unique", "collection": "tickers", "field": "symbol", "description": "…"},
  {"name": "inventory_matches", "kind": "custom", "collection": "pets",
   "function": "fauxcall_petstore.handlers:inventory_invariant", "description": "…"}
]
```

A custom check is `def inventory_invariant(world: SessionWorld) -> list[str]` (one message per
problem). Keep it in the fake's `handlers.py`: `fauxcall world check` finds the fake's package even
before the fake is installed (after `fauxcall scaffold`).

Aggregates (counts per status, totals, balances) are **computed from the world** in a custom handler,
never replayed — otherwise they drift the moment an agent creates a record. Example: Petstore's
`GET /store/inventory` = counts of pets by `status`.

## Async jobs

```json
"jobs": [{"name": "upload", "collection": "uploads", "state_field": "status", "progress_field": "progress",
          "states": [{"name": "queued", "duration": {"kind": "fixed", "seconds": 2.0}},
                     {"name": "processing", "duration": {"kind": "empirical", "samples": [5.8, 6.1, 6.4]}, "progress": [0, 100]},
                     {"name": "done", "terminal": true, "progress": [100, 100]},
                     {"name": "failed", "terminal": true}],
          "outcomes": [{"state": "done", "probability": 0.97}, {"state": "failed", "probability": 0.03}],
          "timestamps": {"created_at": "created", "completed_at": "completed"},
          "time_format": {"kind": "iso", "fraction_digits": 3}, "measured_from": ["000412", "000431"]}]
```

The submit endpoint (`role: job_submit`, `job: upload`) creates the record; the status endpoint
(`role: job_status`) — and any `get`/`list` on the job collection — shows the state at the fake
clock's current time, deterministic per job id. Freeze or accelerate the clock to test agents'
polling. If outcomes depend on the input (e.g. corrupt files fail), use a custom handler or Jev
(`outcome_decider: "jev"` + the endpoint's `semantic.outcome`).

## Expansion

When the real API reports totals larger than you could record (e.g. `"total": 10342`), expand the
collection at build time — generated records cloned from recorded ones, with chosen fields varied,
relations kept valid, then frozen:

```json
"expansion": {"target_count": 10342, "seed": 1, "vary": ["name", "exchange"], "note": "total from GET /v1/tickers"}
```

```bash
fauxcall world expand <name> tickers
```

Expansion keeps both directions: references are drawn from existing targets, and targets short of
their `min_per_target` get the first new referrers (their field must be in `vary`). Expand targets
before their referrers: new exchanges first, then new companies to list on them.

Never expand to paper over missing evidence for records agents will look up by id.
