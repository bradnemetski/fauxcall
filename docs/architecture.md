# How FauxCall works

FauxCall has three parts:

- **The skills** (`plugins/fauxcall/skills/`) tell an agent, usually Claude Code, how to build a
  fake: what to record, how to read the evidence, when to ask the user.
- **The `fauxcall` command** (`src/fauxcall/cli/`) does the mechanical work. It discovers specs,
  records exchanges, analyzes them, builds the dataset and generates the project.
- **The engine** (`src/fauxcall/runtime/`) is the library every generated fake runs on. A fake is
  mostly data: a model, recordings and a world. The engine turns that data into a live API or MCP
  server.

The split keeps judgment and mechanics apart. The agent decides, for example, that `updated_at`
follows the clock or that an envelope's `next` field is a cursor. The tools apply that decision
the same way every time.

## The build pipeline

```text
 discover ─► plan ─► approve ─► record ─► analyze ─► model + world ─► scaffold ─► test ─► serve
 (spec, docs,    (per        (once, for   (paced,     (shapes, time,    (the consistent  (FastAPI   (unit, replay,
  MCP probe)     endpoint)    mutations)   redacted)   relations, jobs)  dataset)          project)   e2e parity)
```

1. **Discover** (`fauxcall init`): identify the input. It can be an OpenAPI or Swagger document,
   a Swagger UI or Redoc page (the spec URL is found in the page or its scripts), a free-form
   documentation page, or a remote MCP server (Streamable HTTP or the older SSE transport). The
   source is snapshotted into the fake.
2. **Plan** (`fauxcall plan template`): for each endpoint, the agent writes a plan of real calls.
   It includes samples, parameter variations, invalid inputs, unknown ids, auth failures, routing
   quirks, pagination walks, and polls of async jobs.
3. **Approve** (`fauxcall plan approve`): calls that change data are listed once, up front, for the
   user to approve, with what stays behind on the real service. Recording runs exactly the
   reviewed calls (a changed or new one needs approving again); parity runs get a per-endpoint
   budget. The recorder refuses any mutating call without an approval.
4. **Record** (`fauxcall record`): calls are paced (`record_rps`) and capped (`record_max_calls`).
   Every exchange is stored with timing, headers, the exact body bytes and any stream events.
   Credentials are redacted, and personal data is replaced with consistent pseudonyms (a keyed
   HMAC, so the same email always maps to the same pseudonym).
5. **Analyze** (`fauxcall analyze …`, `fauxcall sql`): turn recordings into evidence. That means
   inferred schemas, field profiles and time formats, which fields follow the clock, relations
   between collections, job state machines and their timing, write semantics (is a POST
   persisted? what does it echo?), error shapes and the API's JSON spelling.
6. **Model and world** (`data/model.json`, `fauxcall world build`): the agent writes the model, and
   the world is built from recorded records. Relations and invariants are checked, and missing
   referenced records are recorded rather than invented.
7. **Scaffold** (`fauxcall scaffold`): generates an ordinary uv/FastAPI project. It has one route
   per real endpoint, custom handlers for computed behavior, tests, a Dockerfile, compose and CI.
8. **Test** (`fauxcall verify`): unit tests, replays of every recorded request, and end-to-end
   scenarios. These run against both the real service and the fake, and must agree on at least
   98% of weighted checks.

Every step is checkpointed in `data/progress.json`, so an interrupted build resumes where it
stopped.

## A fake's data

```text
data/
├── model.json          # endpoints, field policies, collections, relations, jobs, errors, auth …
├── recordings/         # one JSON file per real exchange (plus binary sidecars)
├── world/              # the dataset, one JSONL file per collection
├── series/             # optional recorded value series (e.g. prices to replay)
├── source/             # the spec or documentation snapshot
├── plans/              # recording plans, and approvals.json for mutations
├── jev-cache.jsonl     # cached Jev answers (commit it)
├── progress.json       # build checkpoints
└── reports/            # parity reports and the build summary
```

Everything is JSON, so it diffs well and reviews well. There is no database: the engine keeps state
in memory, and `fauxcall sql` loads recordings into a throwaway in-memory SQLite for ad-hoc
queries.

## The model

`data/model.json` is one validated document (`fauxcall model schema` prints its schema). Its main
parts:

- **Endpoints** each have a **role** that picks generic behavior: `list`, `get`, `search`,
  `create`, `replace`, `update`, `delete`, `job_submit`, `job_status`, `download`, `upload`,
  `sse`, `websocket`, `replay`, `echo`, or `computed`/`custom` for hand-written handlers. Each also
  has its parameters (validated the way the real API validates them), response variants with
  contract headers, error cases, pagination and the recorded latency.
- **Field policies** say how values behave over time:
  - `now` follows the clock.
  - `relative` keeps its distance to now.
  - `calendar` is live during market hours and frozen at the last tick overnight.
  - `periodic` snaps to a period.
  - `random` and `counter` are new on every response.
  - `echo` is copied from the request.
  - `series` replays recorded values.
  - `volatile`, `pseudonym`, `computed` and `ignore` mark how values are compared and produced.

  Every timestamp is rendered in its recorded format: precision, time zone style, epoch units or
  HTTP dates.
- **Collections, relations and invariants** describe the world. For example, every ticker's
  `exchange` is an existing exchange code, and every upload appears in the status listing.
- **Jobs** are state machines with measured durations and outcome probabilities, which advance
  with the fake's clock.
- **Calendars** hold trading sessions, holidays, early closes and publication times, in the
  market's own time zone.
- **Errors, auth, rate limits, routing and default headers** come from the real API's probes.
- **MCP** keeps the tool, resource and prompt definitions verbatim, plus the transport's exact
  behavior.

## The runtime

Each request goes through one pipeline:

```text
faults ─► auth ─► rate limits ─► parameters ─► role handler or custom handler ─► headers ─► latency ─► journal
```

- **Sessions.** The world is an immutable base with a copy-on-write overlay per session, selected
  by `X-Fauxcall-Session`. Parallel evaluation runs don't see each other's writes, and each
  session can have its own clock.
- **Determinism.** Every random value comes from a generator seeded by the setting `seed`, the
  session and the request's sequence number, so repeating the same calls gives the same answers.
- **Unseen requests.** An id that doesn't exist gets the real API's 404. Data is never invented at
  request time, because invented data couldn't stay consistent across endpoints or across call
  orders. Larger datasets are generated at build time instead, deterministically and within the
  model's relations.
- **Streams.** SSE and chunked responses replay recorded events with their original spacing, and
  timestamps inside events are rendered as "now". A WebSocket sends its recorded greeting, then
  answers each client message with the server messages recorded after the same message.
- **MCP.** FauxCall speaks the MCP wire protocol itself (JSON-RPC over Streamable HTTP or SSE), so
  session ids, content types, error objects and protocol-version negotiation match the real server
  byte for byte. Tool calls are answered from the closest matching recording, or by custom code. An
  OAuth-protected server gets a browserless authorization server that signs in configured users and
  refuses denied ones.
- **Admin API.** Under `/__fauxcall`, it provides reset, clock control, the call journal, fault
  injection, users, snapshots and the model.

## Matching and Jev

When a request has no exact recording (a new question to an "ask" tool, for instance), the engine
picks the closest recording. It first filters to recordings that agree on the inputs that must
match exactly, such as the repository. With a Jev key, Jev judges which remaining recording is the
best stand-in. Without one, the recording sharing the most words wins. Jev also decides semantic
outputs (the sentiment an API would assign) and async-job outcomes when configured to. Answers are
cached and committed, so a fake behaves the same without a key.

## Parity testing

The generated pytest plugin runs each scenario against the real service and then against the fake,
and compares the two responses with weighted checks:

| Check | Weight |
|---|---|
| Status code | 5 |
| Response shape (schema of the body) | 3 |
| Content type | 1 |
| Values that should match exactly | 1 |
| List lengths | 1 |
| Contract headers | 0.5 |
| Values that only need to look right (prices, timestamps, ids) | 0.5 |

Field policies tell the comparison what to expect. A `now` field must be within a tolerance of the
current time. A `volatile` price must have the right type and format, not the same value. Below
98% overall, the run fails, and the report lists every difference by endpoint.

## Privacy

- Credentials never reach recordings, logs or the journal. Configured secret values are scrubbed
  wherever they appear.
- Personal data is pseudonymized while recording. Names, emails, phone numbers, addresses and
  other categories are replaced with realistic stand-ins derived from a per-fake key, so the same
  person keeps the same pseudonym in every recording and every endpoint.
