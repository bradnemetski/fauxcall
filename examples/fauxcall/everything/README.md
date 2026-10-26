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

Tools: 19 · resources: 7 · prompts: 4.

## Known limitations and notes

<!-- fauxcall:notes (kept by `fauxcall scaffold`; the rest of this file is regenerated) -->
Built from @modelcontextprotocol/server-everything 2026.8.31 (TypeScript SDK 1.32.2) running
locally, ~540 recorded exchanges, and the server's published source read as documentation. Every
tool, resource, template, prompt, completion and task method is computed by handlers that
reproduce the recordings value for value (`tests/unit/test_exact_replay.py`,
`tests/unit/test_sessions.py`); the last parity run against the real server scored 100%
(independent 100%, real writes included).

**Where it follows the source rather than a recording** (the recorder can't express these):
protocol versions (2024-10-07 accepted; an unsupported version negotiates 2025-11-25),
`notifications/tools/list_changed` sent once per capability tool when the client is
initialized, gzip resources shared across the sessions of one evaluation, the session-less
POST rules (only one schema-valid initialize; its version header unchecked), and the 60 s
client-request timeout.

**Known differences**

- *Client cancellation* (`notifications/cancelled` mid-call): FauxCall stops the handler. The
  real server lets it run to the end: progress keeps coming, gzip still registers its resource,
  and only the answer is suppressed.
- *Requests to the client time out on the wall clock* (FauxCall's `ask_client`), while the
  research task's clarification timeout follows the fake's clock.
- *Fast mode* (`FAUXCALL_FAST=1`, the tests) skips the long-running operation's step waits and
  the async tools' 1 s polls. Task stages, 5 s log and update ticks, and `tasks/result` polling
  follow the fake's clock in every mode.
- *gzip-file-as-resource never fetches.* `data:` URLs are compressed locally; the result is
  byte-identical to the real server, which forces the gzip OS byte to 0x13. A zlib other than
  the one this was checked with (zlib-ng) could deflate differently. An http(s) URL returns the
  bytes the real server fetched when recorded: the GitHub README (default), `https://example.com/`
  and one 404 page. Any other URL fails with `fetch failed`, as the real server does offline.
  The real server's fetch size and time limits are not reproduced.
- *Transport details FauxCall answers its own way*:
  - A DELETE with an unsupported `MCP-Protocol-Version` ends the session (real: 400; the session
    is kept).
  - HEAD with a session id gets 404 (real: 405).
  - `Content-Type` is matched by substring for in-session POSTs.
  - SSE answers are `text/event-stream; charset=utf-8`, and their headers wait for the first
    event (real: sent at once, plus `x-powered-by`, `cache-control`, `x-accel-buffering`).
  - On `Last-Event-ID` replay, priming events are written without `event:`, and the event store
    keeps at most 1000 events.
- *Elicitation request ids* inside a research task are numbered when the request is delivered
  (real: when it is queued).
- *Subscriptions*: update order follows each session's first subscription to a URI. The real
  server orders by its process-wide map.
- *Requests tied to a live task* (`_meta` related-task naming an existing task): the real
  server queues the answer in the task's message queue and never sends it; the fake answers.
  An unknown task id is refused as on the real server.
- *`notifications/tools/list_changed`* is sent but not in the model's `server_messages`: no
  recording can show it (the recorder listens after initialization), so parity can't hear it.
- *No credentials*: the server takes none, so the fake has no auth to refuse.
- *Not compared value by value in parity* (the test client compares them by shape and count):
  timestamps inside task notifications, random log levels, and per-session ids. The scenarios
  assert the deterministic parts on both sides.
<!-- /fauxcall:notes -->
