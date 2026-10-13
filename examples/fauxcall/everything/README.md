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
### Known limitations (what differs from the real Everything server, and why)

Evidence: ~430 recorded exchanges against `@modelcontextprotocol/server-everything` 2026.8.31 (SDK ^1.30); where recording was impossible, the server's and SDK's published source was read as documentation. Parity: 100% overall and independent over 129 scenarios.

**Tools that send requests to the client.** FauxCall can't send requests back to a client (sampling, elicitation, roots). The real server lists `trigger-sampling-request`, `trigger-elicitation-request`, `trigger-url-elicitation`, `get-roots-list` and the two `-async` tools only to clients declaring the matching capabilities, and the fake gates them the same way (order and predicates from the recordings and source). Called by such a client, the fake behaves as the real server does with a client that **never answers**:
- `trigger-sampling-request(-async)`, `trigger-elicitation-request-async`: `MCP error -32001: Request timed out` after 60 s.
- `trigger-elicitation-request`, `trigger-url-elicitation`: the same after 10 minutes.
- `get-roots-list`: "no roots are currently configured" after 60 s. The real server would list the client's roots at once.

`trigger-url-elicitation` with `errorPath: true` answers the real `-32042` error. Clients like Claude Code declare roots and elicitation, so expect these stalls; the fast mode used in tests skips them. Without the capability, the tools answer the recorded "Tool … not found".

**Ambiguous research queries.** With an elicitation-capable client, `simulate-research-query` `ambiguous: true` follows the source: `input_required`, then the SDK's 60 s timeout, then the "elicitation unavailable" default interpretation. This path is not recorded.

**GET-stream notifications** (simulated logs, `resources/updated`, subscribe/unsubscribe logs, `resources/list_changed`):
- Modeled from source; the subscribe log, the first update and the first simulated log message were verified against the real server (`test_pushed_log_and_update_messages`).
- Messages are random and on a 5 s pace, so values differ run to run.
- `notifications/resources/list_changed` on session-resource registration comes from the SDK source and is unverified.

**`gzip-file-as-resource` never goes to the network.**
- `data:` URLs are decoded per WHATWG.
- The default README URL replays the README recorded on 2026-10-12. The live one on GitHub may have changed since.
- Any other http(s) URL answers `fetch failed`; the real server would fetch it (10 MB / 30 s limits not modeled).
- Fetch latency (~200-300 ms for real) is not added.

**Session resources are per session.** In the real server a module-global map means session B gzipping the same name removes session A's resource. The fake keeps them per session.

**Tasks.** The store keeps a task for its TTL (300000 ms, always) after its last update. The real cleanup timer starts at creation and resets at terminal states.

**Transport and protocol details the FauxCall runtime handles generically** (not adjustable from the fake's code):
- Responses put `jsonrpc`/`id` before `result`; the real server puts `result` first.
- Session ids are 32-hex and SSE event ids 16-hex; the real server uses UUIDs.
- No priming SSE event for 2025-11-25 clients.
- No `400 Unsupported protocol version` for a bad `mcp-protocol-version` header.
- A bogus or stale `Mcp-Session-Id` gets "Server not initialized" instead of "No valid session ID provided".
- No 406/415 for wrong `Accept`/`Content-Type`.
- No `Last-Event-ID` replay, no 409 for a second GET stream, no handling of `notifications/cancelled`.
- `tools/call` of an **unknown** tool with a `task` param answers "Tool … not found" (the real SDK: `-32602 Invalid task creation result`). For known tools the fake matches.
- Unsupported protocol versions negotiate to `2025-11-25` (the SDK's latest; per source, not probed).

**Time.** Dynamic resources embed `toLocaleTimeString()` of the server's zone: US Eastern observed; set `FAUXCALL_EVERYTHING_TZ` to change it.
<!-- /fauxcall:notes -->
