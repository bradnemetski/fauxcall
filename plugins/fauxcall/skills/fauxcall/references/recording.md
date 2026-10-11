# Recording: what to capture, and how

Recordings are the evidence everything else is built from. Record **more than you think you need**:
a fake can only be as faithful as its evidence. Every call costs real API quota, so be deliberate
(the CLI paces requests — `FAUXCALL_RECORD_RPS`, default 2/s — and stops at a budget,
`FAUXCALL_RECORD_MAX_CALLS`, default 2000 per run).

## Contents

- [Plan files](#plan-files)
- [What to record for each kind of endpoint](#what-to-record-for-each-kind-of-endpoint)
- [Captures: chaining calls](#captures-chaining-calls)
- [Writes, read-backs and cleanup](#writes-read-backs-and-cleanup)
- [Async jobs: polling](#async-jobs-polling)
- [Streams and WebSockets](#streams-and-websockets)
- [Probes: errors, auth, routing](#probes-errors-auth-routing)
- [Privacy](#privacy)

## Plan files

One JSON file per endpoint (or per flow) in `fauxcall/<name>/data/plans/`. Start from
`fauxcall plan template <name> "<endpoint id>" --purpose sample`, then edit. Validate with
`fauxcall plan check <name> <file>`; run with `fauxcall record <name> <file>`.

```json
{
  "name": "get-pet",
  "created_at": "2026-10-11T10:00:00Z",
  "calls": [
    {
      "id": "pet-1",
      "endpoint": "GET /api/v3/pet/{petId}",
      "purpose": "sample",
      "request": {"method": "GET", "path": "/api/v3/pet/{petId}", "path_params": {"petId": "1"}}
    },
    {
      "id": "pet-time",
      "endpoint": "GET /api/v3/pet/{petId}",
      "purpose": "time_profile",
      "request": {"method": "GET", "path": "/api/v3/pet/1"},
      "repeat": 4,
      "interval_ms": 15000
    }
  ]
}
```

`request` fields: `method`, `path` (template or concrete), `path_params`, `query` (list of
`[name, value]` pairs — repeats allowed), `headers`, `json_body` | `text_body` + `content_type` |
`form` + `files` (multipart: `{"field": "file", "filename": "a.csv", "content_type": "text/csv",
"text": "a,b\n1,2"}` or `"path"`/`"base64"`), and `auth`: `"default"` (credentials from `.env`),
`"none"` or `"invalid"` (probes). Call fields: `id`, `endpoint` (the model's endpoint id),
`purpose`, `repeat`, `interval_ms`, `capture`, `poll`, `stream`, `cleanup`, `mutating` (override),
`tags`, `note`.

Purposes matter: world building only uses `sample`, `variation`, `edge`, `pagination`,
`time_profile` and `series` by default; `invalid`, `auth`, `write_readback`, `job_poll`, `cleanup`
recordings are evidence for errors and behavior, not data.

## What to record for each kind of endpoint

| endpoint | record |
|---|---|
| **collection list** (`GET /things`) | 2–3 plain samples; every filter parameter with realistic values (and a value that matches nothing); sort options; **walk all pages** (purpose `pagination`) when feasible, else the first several and note the real total; limit edge values (min, max, max+1, 0, non-numeric). |
| **item** (`GET /things/{id}`) | several existing ids (cover different shapes/states — e.g. pets with and without tags); a **non-existent id** (the real 404); a **malformed id** (`abc`, `-1`, `0`, huge); case variants for string ids. |
| **search / free text** | 5–15 realistic, varied queries (popular, rare, misspelled, empty-result); note how results are ranked. Plan Jev matching for unseen queries ([model](model.md#runtime-judgments-jev)). |
| **computed** (conversions, current time, aggregates) | enough samples to reverse-engineer the function: vary every input across its range; include boundaries (DST changes, month ends, zero, negative). |
| **time-bearing** (any response with timestamps) | a `time_profile`: the same request `repeat: 4–6` with `interval_ms` 10–30 s; for market/publication data, profile both inside and outside the active period if the clock allows (or note what was observed). |
| **create** | the minimal valid body, a full body, every required field missing (one at a time), wrong types, unknown fields — then **read back** what was created (purpose `write_readback`) and clean up. |
| **update / replace / delete** | on resources you created; read back after each; non-existent id; delete twice. |
| **async job** | submit (approved), then `poll` until terminal with a short interval (≈ 0.5–2 s); repeat 3+ times to measure duration spread; include inputs that fail (the docs usually say which). |
| **download / binary** | a few files; note content types and `Content-Disposition`. |
| **SSE / chunked / WebSocket** | 2–3 sessions with different parameters/messages; long enough to see the cadence. |

## Captures: chaining calls

`capture` copies values from a call's (real, un-pseudonymized) response into variables usable as
`{{name}}` in later calls' paths, params, headers and bodies:

```json
{"id": "create", "endpoint": "POST /v1/watchlists", "purpose": "variation",
 "request": {"method": "POST", "path": "/v1/watchlists", "json_body": {"name": "fauxcall-probe", "symbols": ["AAPL"]}},
 "capture": {"wl": "$.id"}, "cleanup": ["delete"]},
{"id": "readback", "endpoint": "GET /v1/watchlists/{id}", "purpose": "write_readback",
 "request": {"method": "GET", "path": "/v1/watchlists/{{wl}}"}},
{"id": "delete", "endpoint": "DELETE /v1/watchlists/{id}", "purpose": "cleanup",
 "request": {"method": "DELETE", "path": "/v1/watchlists/{{wl}}"}}
```

## Writes, read-backs and cleanup

- Every write is followed by a read-back so `fauxcall analyze writes <name> --write <id> --readback <id>`
  can tell whether it **persisted**, what the server **added** (ids, timestamps, defaults), and how
  ids are assigned. Some APIs echo writes without storing them — the fake must do the same.
- Name created resources so they are recognizable (`fauxcall-probe-…`) and **always clean up**
  (list the cleanup call ids in `cleanup`; plan them right after the read-back).
- Shared public sandboxes (e.g. Petstore) change under your feet: other people's data appears and
  disappears. Record the state you need in one short run and model parity checks accordingly.

## Async jobs: polling

```json
{"id": "upload-ok", "endpoint": "POST /v1/uploads", "purpose": "variation",
 "request": {"method": "POST", "path": "/v1/uploads",
             "files": [{"field": "file", "filename": "ok.csv", "content_type": "text/csv", "text": "a,b\n1,2\n"}]},
 "capture": {"job": "$.id"},
 "poll": {"endpoint": "GET /v1/uploads/{id}", "state_path": "$.status", "terminal": ["done", "failed"],
          "interval_ms": 500, "timeout_s": 120,
          "request": {"method": "GET", "path": "/v1/uploads/{{job}}"}}}
```

Then `fauxcall analyze jobs <name> --endpoint "GET /v1/uploads/{id}" --state-path '$.status' --terminal done,failed`
prints each job's timeline and suggested duration models / outcome probabilities.

## Streams and WebSockets

```json
{"id": "trades", "endpoint": "GET /v1/stream/trades", "purpose": "sample",
 "request": {"method": "GET", "path": "/v1/stream/trades", "query": [["symbol", "AAPL"], ["count", "10"]]},
 "stream": {"kind": "sse", "duration_s": 5, "max_events": 50}}
```

WebSockets: `"stream": {"kind": "websocket", "duration_s": 3, "send": [{"action": "subscribe", "symbols": ["AAPL"]}]}`
(credentials go in the URL query or headers exactly as the API expects; use `"auth": "default"`).
`send` messages go out right after connecting. For timed or binary probes use `frames`, sent while
the server's messages are received, each `at_ms` after connecting (in order, within `duration_s`):

```json
"stream": {"kind": "websocket", "duration_s": 4, "frames": [
  {"json": {"action": "subscribe", "symbols": ["AAPL"]}},
  {"at_ms": 400, "json": {"action": "ping"}},
  {"at_ms": 1500, "base64": "AAE="},
  {"at_ms": 3000, "text": "after the stream ended"}]}
```

Each frame is `text` (as is), `json` or `base64` (a binary frame); frames the server's close cut off
are noted on the exchange. Probe a feed's errors like any endpoint's: malformed JSON, binary frames,
unknown actions, messages mid-stream and after it ends. A conversation whose messages change state
(orders, writes) is mutating: set `"mutating": true` on the call so the approval rules apply.

## Probes: errors, auth, routing

Errors are part of the contract — agents handle them. For each endpoint record (purpose `invalid`):
missing required parameters, empty values (`?limit=`), wrong types, out-of-range values, unknown ids,
invalid bodies, unsupported methods. Once per API (purpose `auth`): no credentials and invalid
credentials, set on the **request**: `{"id": "no-key", "endpoint": "GET /v1/items", "purpose": "auth",
"request": {"method": "GET", "path": "/v1/items", "auth": "none"}}` (or `"auth": "invalid"`). A request
can also send its own credentials in `headers`/`query` (another key with other powers) instead of the
fake's `.env` ones. Once per API (purpose `edge`): the same path with different letter case and with
a trailing slash (routing quirks), and a path that doesn't exist (the real 404). File a probe of a path
the model doesn't have under that path's own id (`"endpoint": "GET /v1/does-not-exist"`); `plan
check` accepts unknown endpoints for purpose `edge` only.

Do **not** probe rate limits by hammering the API unless the user explicitly approved it; read the
limits from the documentation and from rate-limit headers instead. With approval, raise the
recorder's pace for that one run (it sends at most 2 requests a second by default):
`FAUXCALL_RECORD_RPS=50 fauxcall record <name> data/plans/rate-limit.json` (a plan with enough
`repeat` to cross the limit).

## Privacy

Credentials are always redacted; personal data is pseudonymized consistently (same real value → same
fake value everywhere) before anything is written. Tune detection in `data/privacy.json`:

```json
{"include": {"$.owner.login": "username"}, "exclude": ["$.name"],
 "path_params": {"username": "username"}, "query_params": {"email": "email"},
 "extra_secret_names": ["X-Session-Token"], "allow_names": ["token_count"],
 "public_values": ["user1", "john@email.com"]}
```

`exclude` is for fields that only *look* personal (a product's `name`). When some values of a truly
personal field are public (a sandbox's documented demo users, examples from the docs), list exactly
those values in `public_values`: they stay as they are everywhere (bodies, paths, parity
comparisons), while everyone else's data in the same fields is still pseudonymized. Never `exclude` a
personal field because some of its values are public: on a shared sandbox, other people's real data
arrives in the same fields. The same goes for secret-looking fields of a public demo (passwords it
generates for demo users): list those values rather than allowing the name (`allow_names`), so a real
password in the same field is still redacted. `allow_names` is for names that only look secret
(`token_count`); credentials from `.env` are always redacted, listed or not.

Categories: email, phone, person_name, first_name, last_name, username, address, street, city,
postal_code, ip, credit_card, ssn, date_of_birth, free_text. If a recording leaked something, fix
`privacy.json`, delete the affected recordings and re-record — never hand-edit secrets out.
