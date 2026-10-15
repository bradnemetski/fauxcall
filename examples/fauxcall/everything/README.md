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
### How faithful it is, and where it differs

Every one of the 19 tools, the 7 documents, both resource templates, the 4 prompts, completions,
`logging/setLevel`, subscriptions and the task methods are reproduced from recordings of the real
server (`@modelcontextprotocol/server-everything` 2026.8.31). Its answers are computed, not just
replayed: argument refusals are worded like zod's, numbers are spelled like JavaScript's
(`0.30000000000000004`, `1e+21`, `received Infinity`), URIs are parsed the WHATWG way (as Node
does), gzip output is byte-identical, and tasks (including a report that fails), toggles, session
resources, subscriptions (one map for every client, as on the real server) and URL-elicitation
retries keep their state. Tools that ask the client (sampling, elicitation, roots, client tasks)
do so mid-call, check the client's replies with the server's schemas, and are listed only to
clients that declare the capability; registering them after `notifications/initialized` sends
`notifications/tools/list_changed`, as on the real server.

The HTTP front is the real one too (`src/fauxcall_everything/front.py`, `express.py`, `sse.py`,
from the recordings and the published source of Express 5.3, cors 2.8.6 and the SDK's Streamable
HTTP transport): CORS headers and `OPTIONS` preflights (browser clients such as MCP Inspector
connect directly), Express's HTML 404 page, `/mcp` matched in any case and with a trailing slash,
and the transport's refusals in its order (no session, `Accept`, `Content-Type`, a body over
4 MiB, invalid JSON, batches over 100, messages its JSON-RPC schema rejects such as a fractional
progress token, handshake rules, unsupported `MCP-Protocol-Version` headers named in the answer).
SSE answers are written as the SDK writes them (field and member order, the session id, the
priming event by the version the client asked for), a request naming a task the client doesn't
have is refused before it runs, an `initialize` inside a session is refused by its schema, and a
DELETE ends the session's GET stream. Protocol versions negotiate as recorded: 2024-10-07 to
2025-11-25, anything else gets 2025-11-25. Everything the front answers itself is in the journal
(`/__fauxcall/journal`) like the rest.

Known differences:

- **No network.** `gzip-file-as-resource` processes `data:` URLs locally and serves the default
  README (as fetched while recording); any other `http(s)` URL answers `fetch failed`, as the real
  server does when a fetch fails. The real server would fetch it, and gzip whatever came back,
  an HTTP 404 page included.
- **Local time.** Dynamic resources are stamped `h:mm:ss AM/PM` in America/New_York, the recorded
  server's zone, on the fake's clock.
- **`tools/call` parameters FauxCall's runtime checks before the fake's handlers**: a `name` that
  isn't a string gets the recorded refusal of a missing one (`received undefined`, real: the
  actual type); non-object `arguments` get the answer recorded for exactly that value (`echo`:
  `null`, `[]`, `"hello"`, `5`; `get-sum`: `"x"`), any other value the recorded refusal of an
  array (`received array`); an unknown tool is refused as unknown before its `arguments` or
  `task` are checked (real: zod's refusal, or "Invalid task creation result", first).
- **A request naming an existing related task** (`_meta["io.modelcontextprotocol/related-task"]`)
  is answered as usual; the real server queues that answer for the task and then drops it
  (`tasks/result` delivers queued answers only to the server's own requests), so the request is
  never answered. Inside a batch, the methods FauxCall's runtime answers itself (`ping`, the
  listings) don't look the task up at all.
- **Resuming a GET stream** (`Last-Event-ID`): the real server replays every later event of every
  stream of the session (priming events included), never refuses it as a second stream, and
  sends nothing more on it; the fake replays the matching stream's events, answers 409 while
  another GET stream is open, and keeps pushing on it.
- **Cancellation**: a client's `notifications/cancelled` is ignored (the real server stops that
  call's progress notifications, drops its answer and leaves the POST stream open), and the fake
  sends none when its own requests to the client time out.
- **Wire details**: a queued research clarification gets its JSON-RPC id when delivered (real: when
  queued); SSE answers carry a `Content-Length` when complete (real: Node chooses). JSON nested
  deeper than about 1,000 levels is refused as invalid (real: parsed).
- **Timers**: simulated logging, resource updates and task status pushes run on wall-clock time,
  while task states follow the fake's clock (`/__fauxcall/clock`). `POST /__fauxcall/reset`
  starts every client afresh and ends what they left running.
- **Not recorded, from the published source**: the transport's refusals of raw POST bodies (over
  4 MiB, invalid JSON, batches, two handshakes in one batch), DELETE closing streams and stream
  resumption could not be recorded without approving raw `POST /mcp` probes or holding streams
  open; GET, HEAD and OPTIONS behavior is checked against the real server by the parity suite.
- Roots are requested on the client's GET stream, as by the real server: a client without one waits
  60 s (the SDK's timeout) and is told no roots are configured, exactly like the real server.
<!-- /fauxcall:notes -->
