# The model (`data/model.json`)

Everything the fake knows lives in one validated JSON document. Edit it directly; validate with
`fauxcall model check <name>`. `fauxcall model schema --brief` lists every part's fields, types,
defaults and descriptions (`--part EndpointModel` for one part; without `--brief`, the JSON Schema).
Fields left out take their defaults.

## Contents

- [Endpoints and roles](#endpoints-and-roles)
- [Parameters](#parameters)
- [Responses, shapes and pagination](#responses-shapes-and-pagination)
- [Writes](#writes)
- [Streams (SSE, chunked, WebSockets)](#streams-sse-chunked-websockets)
- [Errors](#errors)
- [Auth, rate limits, routing, headers](#auth-rate-limits-routing-headers)
- [Runtime judgments (Jev)](#runtime-judgments-jev)
- [Custom handlers](#custom-handlers)

## Endpoints and roles

Each endpoint has an `id` (`"<METHOD> <full path>"`, never change it), a `role`, and role-specific
settings. Choose the role from evidence:

| role | when | needs |
|---|---|---|
| `list` | returns many records of one collection (bare array or envelope) | `collection`, usually `shape`, `pagination`, filter params |
| `get` | one record by key from the URL | `collection`, `key_param`, `not_found` error |
| `search` | free-text / ranked results | recorded answers for recorded queries; `collection` (filter the world) or `semantic.match` (closest recording) |
| `create` / `replace` / `update` / `delete` | writes | `collection`, `write` |
| `job_submit` / `job_status` | asynchronous work | `job` (a JobModel), see [consistency](consistency.md#async-jobs) |
| `download` / `upload` | binary content | collection + key (downloads serve uploaded bytes or recordings) |
| `sse` / `websocket` | streaming | `stream` |
| `replay` | fixed answers per input (exact recording, else closest) | recordings |
| `echo` | returns the request body | — |
| `computed` / `custom` | anything the engine can't express (conversions, aggregates, current time in a zone) | a handler in `handlers.py` |

Prefer the most generic role that reproduces the evidence; drop to `computed` only when needed.

```json
{
  "id": "GET /api/v3/pet/{petId}", "method": "GET", "path": "/api/v3/pet/{petId}",
  "role": "get", "collection": "pets", "key_param": "petId",
  "params": [{"name": "petId", "location": "path", "required": true, "schema": {"type": "integer"},
              "on_invalid": "error"}],
  "errors": [{"when": "not_found", "template": "pet_not_found"},
             {"when": "invalid_param", "template": "invalid_id", "param": "petId"}]
}
```

## Parameters

FastAPI's validation is bypassed; the engine validates parameters the way the **real** API does:

- `schema` — types, `enum`, `minimum`/`maximum`, `minLength`/`maxLength`, `pattern`, arrays
  (`style`/`explode`). Remove constraints the real API doesn't enforce (your invalid probes tell you).
- `on_invalid` — what the real API did with a malformed value: `error` (renders the endpoint's
  `invalid_param` error case), `ignore` (treated as absent), `coerce` (passed through as text).
- `required` + a `missing_param` error case for required parameters.
- `empty_as_absent` — the real API treats an empty value (`?limit=`) as not given (your empty-value
  probe got the default behavior instead of an error).
- List filtering: `maps_to` (record field), `operator` (`eq`, `in` (+ `separator`), `contains`,
  `icontains`, `prefix`, `gte`, `lte`, `gt`, `lt`, `search`).

## Responses, shapes and pagination

`fauxcall analyze endpoint … --apply` fills `responses[]` (status, exact `content_type`,
`observed_schema`, examples), `latency` and the API's `json_style`. You add:

- `success_status` (e.g. 201 for creates).
- `shape`: `record` (default), `list` (bare array), `envelope` (a recorded body as `template` whose
  `items_path` receives the records), `raw`; `projection` limits fields (summary listings).
- `pagination` — reproduce the real mechanics and cursor **spelling**:

```json
"shape": {"kind": "envelope", "items_path": "$.data",
          "template": {"data": [], "next_cursor": null, "total": 0}},
"pagination": {"style": "cursor", "limit_param": "limit", "cursor_param": "cursor",
               "default_limit": 20, "max_limit": 100, "clamp_limit": false,
               "total_path": "$.total", "next_path": "$.next_cursor",
               "cursor_format": "key", "cursor_template": {"after": "{{key}}"},
               "cursor_encoding": "base64url"}
```

Styles: `offset` (`offset_param`), `page` (`page_param`, `first_page`), `cursor`, `link_header`
(RFC 8288 `Link`), `none`. `page_fields` maps other body paths to `total`, `count`, `page`,
`per_page`, `offset`, `limit`, `total_pages`, `has_more`, `next`, `prev`.
- Contract headers per response: `responses[].headers` (`{"Location": {"value": "/pets/{{id}}"}}`).

## Writes

```json
"write": {"persist": true, "echo": "record", "id_strategy": "increment", "id_start": 1001,
          "status": 201, "defaults": {"created_at": "{{now:iso_ms}}", "symbols": []},
          "merge": false, "upsert": false, "location_header": "/v1/watchlists/{{id}}"}
```

From `fauxcall analyze writes`: `persist` (false when the read-back didn't find it), `echo`
(`record` = the stored record, `request` = the body as sent, `empty`, `message` with
`message_template`), `id_strategy` (`increment`, `uuid4`, `random_int`, `client`), server `defaults`
(placeholders allowed). Response key order follows the observed schema automatically.

## Streams (SSE, chunked, WebSockets)

```json
"stream": {"kind": "websocket", "respond_to": "match", "speed": 1.0, "loop": false,
           "exchanges": [], "auth_failure_close_code": 4401}
```

Streams replay recordings with their recorded spacing (`speed` 2.0 = twice as fast); JSON payloads
go through the endpoint's body-scope `fields` policies, so timestamps inside events are "now".
`kind`: `sse` or `chunked` (pick the recording whose query best matches the request) or `websocket`.
`exchanges` restricts replay to specific recording ids. For WebSockets, `respond_to`:

- `match` (default): send what the server sent on connect (greetings, acks), then answer each client
  message with the server messages recorded after the same message (JSON compared by value);
  unrecorded messages get no answer. A recorded close is replayed with its code.
- `none`: push the server side on its own timeline, ignoring the client (`loop` repeats it).
- `echo`: echo client messages.

`auth_failure_close_code`: when the real server accepts the socket and then closes it on bad
credentials (record it!), its close code; leave `null` when it refuses the handshake.

## Errors

Error templates are real error responses with the variable parts replaced by placeholders. Create one
from a recording, then generalize it:

```bash
fauxcall analyze errors <name>                                   # distinct error bodies
fauxcall analyze errors <name> --template not_found --from 000123
```

```json
{"name": "not_found", "status": 404, "content_type": "application/problem+json; charset=utf-8",
 "body_json": {"type": "https://tools.ietf.org/html/rfc7231#section-6.5.4", "title": "Not Found",
               "status": 404, "traceId": "{{traceparent}}"}}
```

Placeholders: `{{uuid}}`, `{{uuid7}}`, `{{traceparent}}`, `{{hex:16}}`, `{{digits:6}}`,
`{{alnum:12}}`, `{{now:iso|iso_ms|iso_us|iso_7|iso_ns|http_date|epoch|epoch_ms|date}}`, `{{method}}`,
`{{path}}`, `{{url}}`, `{{query}}`, `{{param.<name>}}`, `{{header.<name>}}`, `{{body.<path>}}`,
`{{value}}` (the offending value), `{{param}}` (its name), `{{status}}`, `{{request_id}}`,
`{{identity}}`. A string that is exactly one placeholder keeps the value's type.
Attach templates per endpoint with `errors: [{"when": …, "template": …, "param": …}]` (conditions:
`not_found`, `invalid_param`, `missing_param`, `invalid_body`, `unauthorized`, `forbidden`,
`expired`, `insufficient_scope`, `conflict`, `method_not_allowed`, `unknown_route`,
`rate_limited`, `payload_too_large`, `unsupported_media_type`, `custom`). A template **named** like a
condition (e.g. `not_found`) is the API-wide default for it.

## Auth, rate limits, routing, headers

```json
"auth": {"schemes": [{"kind": "api_key", "location": "header", "name": "X-API-Key"}],
         "exempt": ["GET /healthz"], "missing_template": "unauthorized",
         "invalid_template": "unauthorized", "forbidden_template": "forbidden"},
"rate_limits": [{"name": "per_key", "limit": 30, "window_seconds": 10, "key": "credential",
                 "endpoints": ["*"], "error_template": "rate_limited",
                 "headers": {"X-RateLimit-Limit": "{{limit}}", "X-RateLimit-Remaining": "{{remaining}}",
                             "X-RateLimit-Reset": "{{reset_epoch}}"}, "source": "headers"}],
"routing": {"case_insensitive": true, "trailing_slash": "strict",
            "unknown_route_template": "unknown_route", "spec_paths": ["/swagger/v1/swagger.json"]},
"default_headers": {}
```

**Order of checks**, as most real APIs do it: faults → authentication (who is calling; failures get
`missing`/`invalid`/`forbidden`/`expired` templates and are *not* counted by rate limits) → rate
limits → authorization (`required_scopes`, e.g. `{"POST *": ["write"]}`: a caller listing scopes
without the needed one gets `insufficient_scope_template`, else `forbidden_template`; the call *was*
counted, so rate-limit headers are present) → parameters → the role handler.

Rate limits: `key` `credential` (per API key/token), `session`, `ip` or `global`; `algorithm`
`fixed_window` (the window starts at its first request), `sliding_window` or `token_bucket`;
`endpoints` globs; `headers` templates with `{{limit}}`, `{{remaining}}`, `{{reset_epoch}}`,
`{{reset_seconds}}`, `{{window_seconds}}`; `headers_on_success` (also on 2xx); `retry_after`
(add `Retry-After` to the 429). Read limits from docs and headers; don't hammer the API to find them.
Windows run in real time, so a frozen clock never locks an agent out; `advance` on the fake's clock
moves them forward too (time passed), which is how a test lifts a 429 without waiting.

Which users the fake accepts is runtime configuration, not model: `auth.mode` (`any`, `allowlist`,
`off`) and `auth.users` in the fake's `fauxcall.toml` (or `FAUXCALL_AUTH__*`), so evaluations can
test auth failures. When the real API has credentials with special powers that your scenarios use
(a read-only key, a revoked key), add them as users there, with the same values, so both sides
agree: e.g. `[[auth.users]]` `name = "readonly"`, `api_key = "readonly-key"`, `scopes = ["read"]`,
or `status = "denied"` for a revoked key. Users without `scopes` are unrestricted.

## Runtime judgments (Jev)

Only when the user has a Jev key **and** the fake must judge inputs no recording covers. Answers are
cached in `data/jev-cache.jsonl` (commit it: the fake then replays them without a key); without a key
the fallback is deterministic.

```json
"semantic": {
  "match": {"inputs": ["args.question", "args.repoName"], "exact": ["args.repoName"],
            "min_confidence": 0.5, "fallback": "lexical", "no_match": "closest"},
  "outputs": [{"path": "$.sentiment", "kind": "choice", "inputs": ["body.text"],
               "instructions": "Which sentiment would the API assign to this text?",
               "options": {"positive": "…", "neutral": "…", "negative": "…"}, "fallback": "neutral"}],
  "outcome": {"instructions": "Would the API accept this upload?", "inputs": ["body.filename"],
              "outcomes": {"done": "processed", "failed": "rejected as corrupt"}, "fallback": "done"}
}
```

Input names: `path.<name>`, `query.<name>`, `header.<name>`, `body.<json path>` (REST) and
`args.<json path>` (MCP tool arguments). A repeated query parameter is the list of its values;
values inside arrays use `[*]` and are the list of every element's value
(`args.symbols[*]` = `["AAPL", "MSFT"]`, `body.items[*].sku` = `["a", "b"]`). Without Jev, matching
is exact on all inputs first, then the recording sharing the most input words among those agreeing
on every `exact` input; `fallback: "none"` answers with the real "not found" error instead.

## Custom handlers

`src/<package>/handlers.py` maps endpoint ids to async functions taking a `RequestContext`:

```python
async def latest(ctx: RequestContext) -> Response:
    base = str(ctx.params.get("base") or "EUR")
    record = ctx.world.get("rates", "latest")  # the consistent world (session-aware)
    if record is None:
        return ctx.error("not_found")  # the real API's error, from the model
    body = convert(record.record, base)  # your logic
    return ctx.respond(ctx.render_body(body))  # field policies + exact JSON spelling


HANDLERS["GET /v1/latest"] = latest
```

Context helpers: `ctx.params` (validated), `ctx.json_body`, `ctx.path_param(name)`, `ctx.query(name)`,
`ctx.header(name)`, `ctx.world` (`get`, `list`, `insert`, `update`, `replace`, `delete`),
`ctx.now_ns` (fake clock, ns), `ctx.calendar(name)` (`is_open`, `last_close`, …), `ctx.rng` (seeded),
`ctx.render_record(record)`, `ctx.render_body(value)`, `ctx.respond(body, status=…, headers=…)`,
`ctx.respond_bytes(…)`, `ctx.empty(status)`, `ctx.error(condition, param=…, value=…)`,
`ctx.identity`, `ctx.session`, `ctx.runtime.jev` (cached Jev). MCP tools use
`TOOL_HANDLERS["name"] = async def tool(ctx, arguments) -> result_json`.
