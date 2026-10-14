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
Built from recordings of `@modelcontextprotocol/server-everything` 2026.8.31 (serverInfo
`mcp-servers/everything` 2.0.0), run locally. Every tool and method is computed by
`src/fauxcall_everything/` from what was recorded; the package's published source was read as
documentation (never run or called) to explain the rules behind the recordings.

What differs from the real server, and why:

- **No network.** `gzip-file-as-resource` gzips `data:` URIs exactly as the real server does
  (byte-identical output). For `http(s)` URLs the fake never fetches: the default README URL
  returns the bytes recorded on 2026-10-14; any other URL answers `fetch failed`, which is what
  the real server says when its fetch fails. Against the real server, other public URLs would
  succeed.
- **Server clock and zone.** Dynamic resources print `toLocaleTimeString()` in the real server's
  local zone, modeled as `America/New_York` (recordings showed UTC-4 in October; a host in a
  UTC-4 zone without daylight saving time would differ in winter). Move the fake's
  clock with `POST /__fauxcall/clock` and these times follow it. Task timestamps
  (`createdAt`, `lastUpdatedAt`) land on whole seconds after creation; the real server adds a few
  milliseconds of scheduling jitter.
- **Random log levels.** `toggle-simulated-logging` sends a log message at a random level every
  5 seconds, as the real one does. The fake's random sequence isn't the real one's, and its order
  is repeatable per session.
- **Roots handshake.** The real server asks for roots on the client's GET stream 350 ms after
  `initialized`. If no stream is open then, that request is lost (recording 000497), and the
  roots are asked for again when `get-roots-list` runs. The fake does the same. Without a GET
  stream, `get-roots-list` waits out the SDK's 60 s timeout on both sides; in fast mode (tests)
  the fake skips that wait.
- **Shared server state across clients.** The real server keeps some maps per process: a gzip
  file registered under a name another client already used removes it from that client's
  session, and resource subscriptions are ordered by first subscription. The fake keeps these per
  FauxCall evaluation session (`X-Fauxcall-Session`), so parallel evaluations stay isolated.
  When a gzip file moves to another client, the original client gets no
  `notifications/resources/list_changed`; the real server would send one.
- **Not recorded, taken from the server's source:** the 10-minute elicitation timeout, the 60- and
  600-poll limits of the async client-task tools, zod's message for a sampling reply missing
  `content`, resource-template completion for arguments other than `resourceId`, task expiry
  (300 s after creation, restarted when the task ends), non-finite numbers, and Node's timer
  overflow (steps longer than 2^31-1 ms fire after 1 ms). `tasks/list` paging was recorded:
  10 per page, and the cursor is the last task id of the page.
- **Before `notifications/initialized`.** The real server registers its capability-dependent
  tools (sampling, elicitation, roots, research, async) when the client sends `initialized`, and
  announces them with `notifications/tools/list_changed`. A client that lists or calls tools
  between `initialize` and `initialized` sees only the 12 base tools there. The fake offers them
  from `initialize` on and sends no `list_changed`; every MCP client sends `initialized` first.
- **Protocol versions.** The fake negotiates like the SDK's source: a supported version
  (2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05, 2024-10-07) is echoed, and anything else gets
  2025-11-25. The recorder can't send a second `initialize` to probe this, so it comes from the
  source.
- **Wire details of the FauxCall runtime.** Requests the fake sends to the client use string ids
  (`"fauxcall-1"`); the real server numbers them per session. A second `initialize` in a session
  is refused with the caller's request id; the real server answers with `"id": null`. A body
  that isn't JSON gets `Parse error`; the real server says `Parse error: Invalid JSON`.
- **Reply checks not reproduced.** The real server checks client replies with zod. The fake
  reproduces the checks on sampling results and on the elicitation `action`, but not these:
  - elicitation `content` values that aren't primitives;
  - a `trigger-sampling-request-async` reply that is neither a task nor a full message. The fake
    echoes it as `[SYNC]`; the real server answers with a zod error.
- **`get-env`** returns the recorded environment of the real server's process (an almost empty
  one: `PATH`, `HOME=/nonexistent/home`, `PORT`, and macOS's `__CF_USER_TEXT_ENCODING`, which
  includes the local uid 501).
<!-- /fauxcall:notes -->
