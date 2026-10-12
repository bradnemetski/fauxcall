# Everything Reference Server: a FauxCall fake

A high-fidelity fake of **Everything Reference Server** (http://127.0.0.1:3101), generated with
[FauxCall](https://github.com/bradnemetski/fauxcall) from http://127.0.0.1:3101/mcp. Agents talk to it exactly
like the real MCP server: same tools, resources and transport,
consistent data across tools, real-world timing, and repeatable state.

## Run it

```bash
uv run fauxcall-everything            # http://127.0.0.1:8706
uvx fauxcall up everything            # same, from the workspace root
```

Point your agent's MCP client at `http://127.0.0.1:8706/mcp` instead of the real server,
for example in `.mcp.json`:

```json
{
  "mcpServers": {
    "everything": {
      "type": "http",
      "url": "http://127.0.0.1:8706/mcp"
    }
  }
}
```

or `claude mcp add --transport http everything http://127.0.0.1:8706/mcp`.
## Control it during evaluations

Every fake has an admin API under `/__fauxcall` (send `X-Fauxcall-Session: <id>` to isolate parallel runs):

| call | what it does |
|---|---|
| `GET /__fauxcall/health` | status, clock, sessions |
| `POST /__fauxcall/reset` | back to the recorded state (`{"session": "..."}` for one session) |
| `POST /__fauxcall/clock` | `{"op": "freeze"}`, `{"op": "travel", "at": "2026-01-05T15:30:00Z"}`, `{"op": "scale", "rate": 60}`, `{"op": "real"}` |
| `GET /__fauxcall/journal` | every call the agent made (filter by `session`, `endpoint`, `since`) |
| `PUT /__fauxcall/faults` | inject errors, timeouts, delays, disconnects, 429s |
| `PUT /__fauxcall/auth/users` | allowed / denied / expired users |

Settings come from `FAUXCALL_*` variables, `.env` and `fauxcall.toml` (e.g. `FAUXCALL_FAST=1` turns off
latency and rate-limit emulation).

## What's inside

| path | what |
|---|---|
| `data/model.json` | everything the fake knows: tools, transport, errors and timing |
| `data/recordings/` | the real exchanges it was built from (secrets redacted, personal data pseudonymized) |
| `src/fauxcall_everything/handlers.py` | custom behavior (yours to edit) |
| `tests/` | unit tests, recorded-request replays and end-to-end scenarios (yours to extend) |

## Test it

```bash
uv run pytest                                   # unit + offline end-to-end (no network)
uv run pytest tests/e2e --target=parity         # compare with the real API (needs .env)
```

Tools: 16 · resources: 7 · prompts: 4.

## Known limitations and notes

<!-- fauxcall:notes (kept by `fauxcall scaffold`; the rest of this file is regenerated) -->
Built from ~400 recorded exchanges with `@modelcontextprotocol/server-everything` 2026.8.31
(serverInfo `mcp-servers/everything` 2.0.0) over Streamable HTTP. Every tool is computed from
rules found in the recordings (exact Zod error wording, JavaScript number formatting, `Number()`
id parsing, WHATWG URL normalization, per-session state), not replayed.

**Matches the real server**
- All 16 tools, 7 static documents, both resource templates, 4 prompts, completions,
  `logging/setLevel`, subscriptions, `ping`, version negotiation (2024-11-05 to 2025-11-25).
- MCP Tasks for `simulate-research-query`: task required, stages about 1 s apart, completion after
  about 4 s, `ttl` always 300000, expiry 300 s after the last update (a task was still there 302 s
  after creation; the exact end of the window wasn't measured), cancel/result semantics,
  tasks scoped to their MCP session.
- `gzip-file-as-resource` output matched Node's zlib byte for byte for every recorded input (on
  the recording host; Node's bundled zlib can differ by CPU, clients decompress anyway). Session
  resources are readable only in the session that created them, under their normalized URI (so,
  as on the real server, names with spaces or `..` can't be read back); a re-used name replaces
  the content.
- URI handling follows `new URL()`: dot segments resolved, bad hosts and ports refused as
  `Invalid URL`; `tasks/list` pages by 10 with the last task id as `nextCursor`.
- Dynamic resources print the local time like `toLocaleTimeString()`. The recorded server ran
  at UTC-4; the fake uses `FAUXCALL_LOCAL_TZ` (default `America/New_York`). Set it to the zone of
  the server you compare against.
- `trigger-long-running-operation` takes `duration` seconds and sends `notifications/progress`
  only when the call carries a `progressToken`.

**Differs, and why**
- `resources/list` never shows the session resources `gzip-file-as-resource` creates (the real
  server appends them): FauxCall serves listings from the model, so handlers can't extend them.
  `resources/read` of those URIs works.
- The fake is offline: `gzip-file-as-resource` serves only the URLs fetched while recording (the
  default README, `example.com`, a GitHub 404 page); any other `http(s)` URL answers
  `fetch failed`, as the real server does for an unreachable host. `data:` URLs work fully.
- No server-initiated traffic outside a request: after `toggle-simulated-logging` or
  `toggle-subscriber-updates` the real server pushes `notifications/message` /
  `notifications/resources/updated` every 5 s on the standalone GET stream; the fake doesn't.
  Nor does it send `notifications/resources/list_changed` after gzip.
- `trigger-sampling-request` and `trigger-elicitation-request` (offered only to clients declaring
  sampling / elicitation) make the server call the client back, which the fake can't do. It answers
  as the real server does when the client never responds: after 60 s,
  `MCP error -32001: Request timed out` (observed in parity runs). Agents that do answer sampling
  or elicitation get a different result from the real server. `get-roots-list` always reports
  that no roots are configured, since the fake never asks the client for them.
- Not modeled: `trigger-url-elicitation`, `trigger-sampling-request-async` and
  `trigger-elicitation-request-async`, which the real server lists only to clients declaring
  `elicitation.url` or `tasks.requests.*` capabilities (FauxCall's introspection doesn't declare
  those).
- `tools/call` without a string `name`, or with `arguments` that aren't an object (other than the
  recorded cases), get FauxCall's generic errors instead of the server's Zod `-32603` list; a second
  `initialize` in a session is answered instead of refused with `-32600`.
- `simulate-research-query` with `ambiguous: true` from a client that declares elicitation: the
  real server sends `elicitation/create` and parks the task in `input_required`
  (`Found multiple interpretations for "<topic>". Requesting clarification...`, observed in
  parity); the fake can't see the client's capabilities from a handler and completes the task in
  about 4 s, as it does for every other client.
- `gzip-file-as-resource` doesn't enforce the server's `GZIP_MAX_FETCH_SIZE` (10 MB) and
  `GZIP_MAX_FETCH_TIME_MILLIS` limits on `data:` URLs.
- Wire details: the fake's `Mcp-Session-Id` is 32 hex digits (the real one a dashed UUID), SSE
  event ids are 16 hex digits (real: UUIDs), every JSON-RPC message puts `jsonrpc` first (the real
  server last), and 2025-11-25 sessions get no empty priming SSE event before the answer.
- `get-env` returns the recorded environment of the real process (near-empty by design).
<!-- /fauxcall:notes -->
