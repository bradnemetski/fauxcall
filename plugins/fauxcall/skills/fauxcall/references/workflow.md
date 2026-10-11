# Workflow details and judgment calls

## Contents

- [Resuming](#resuming)
- [Free-form documentation](#free-form-documentation)
- [Ordering the endpoint loop](#ordering-the-endpoint-loop)
- [Large APIs](#large-apis)
- [Difficult real APIs](#difficult-real-apis)
- [Pitfalls](#pitfalls)

## Resuming

`fauxcall status <name>` shows every phase and endpoint step plus the next action. Re-invoking the
skill must continue from there, not start over. Before stopping (or when context runs long), always
leave a `fauxcall progress note` with what you were doing and what's next.

## Free-form documentation

When `init` finds no machine-readable spec it saves the page as `data/source/docs.md` (+ linked pages
in `docs-links.json`). Then:

1. Read the docs (follow the links that describe endpoints, auth, pagination, errors, rate limits).
2. Write an OpenAPI 3.1 document describing every endpoint to `data/source/openapi.json`: paths,
   methods, parameters (with types and constraints *as documented*), request bodies, responses
   (statuses + schemas from the examples), security schemes, `servers` with the real base URL.
3. `fauxcall spec import <name>` adds the endpoints to the model.
4. Treat that document as a hypothesis: recordings will confirm or correct it (update both).

## Ordering the endpoint loop

1. Independent collections (exchanges, categories, currencies) — their records are referenced by others.
2. Dependent collections (tickers → exchanges, pets → categories).
3. Computed / time endpoints.
4. Writes (needs approval; uses records from 1–2).
5. Async jobs.
6. Streams and WebSockets.

## Large APIs

Fake what agents will use. Agree on scope at the gate; record and model the chosen endpoints fully
rather than everything shallowly; leave the rest out of the model (the fake then answers them with
the real API's unknown-route response, which is honest).

## Difficult real APIs

- **Shared sandboxes** (Petstore): data changes between recording and parity runs. Record in one
  short run; in scenarios compare shapes and behavior, use `CompareOptions(values=False)` on live
  listings, and create the records a scenario needs inside the scenario.
- **Rate-limited / paid APIs**: lower `FAUXCALL_RECORD_RPS`, budget calls, prefer pagination walks over
  many small queries, never probe limits without approval.
- **Flaky APIs**: re-run failed recordings; in parity, rerun before concluding.
- **Time-dependent behavior you can't observe now** (market closed): record what you can; note the
  rest from the docs; test the fake's other mode by moving its clock.

## Pitfalls

- Replaying timestamps instead of giving them policies (the fake looks frozen).
- Two shapes of the same entity in one collection (list summary vs detail) — use `projection`.
- Inventing records to satisfy consistency checks — record them instead.
- Aggregates replayed from recordings — compute them from the world.
- Trusting the spec over the evidence — real APIs drift from their docs; the fake follows the API.
- Editing recordings by hand — re-record instead; recordings are evidence.
