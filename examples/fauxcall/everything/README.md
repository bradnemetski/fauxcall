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
What this fake reproduces, and where it knowingly differs from the real
`@modelcontextprotocol/server-everything` 2026.8.31 (TypeScript SDK 1.32.2) it was recorded from.

**How it was built.** Every tool, resource, prompt, completion and `tasks/*` method was recorded
(489 exchanges); behavior that can't be replayed is computed in `src/fauxcall_everything/`
from that evidence and the server's published source: zod-worded argument errors, JavaScript
number spelling (`1e+21`, `5e-7`), `Number()` parsing of resource ids, WHATWG URL normalization
of resource URIs, Node-identical gzip bytes, the server's local time (America/New_York,
`7:54:20 PM`) in dynamic resources, per-session state (session resources, subscriptions, logging
level, roots, toggles, tasks) and the requests the server sends the client.

**Known differences**

- **No network.** `gzip-file-as-resource` compresses `data:` URLs itself, and the default README
  URL from its recording (fetched 2026-10-26). Any other http(s) URL answers `fetch failed`, as
  the real server does for an unreachable host; the real server would fetch it.
- **Sessions stay apart.** The real server keeps some state in module-wide maps shared by every
  session:
  - Gzip session resources: a second session registering the same name removes the first
    session's resource.
  - The subscription list: the order of `resources/updated` follows the first subscription by
    any session.

  The fake keeps each session's state to itself.
- **`notifications/tools/list_changed` at initialization.** The real server registers its
  conditional tools on `notifications/initialized`, and sends one `list_changed` per tool. A client
  that opens its GET stream only after initializing (every common client) never sees them; only
  a client whose stream was already open, or that resumes with `Last-Event-ID`, would. The fake
  doesn't send them.
- **Requests the fake can't keep.** Without a GET stream, the real server's `roots/list` request is
  dropped but kept in its event store, and times out after 60 s with a `notifications/cancelled`.
  The fake reproduces the dropped request and the 60 s wait of `get-roots-list`, but not the late
  cancellation notice or the request's replay on `Last-Event-ID`.
- **Protocol versions.** An unsupported version is answered with 2025-11-25, and 2024-10-07 is
  accepted, as the SDK source says. This wasn't recorded, because the recorder can't open a fresh
  session per `initialize`.
- **Time zone.** Dynamic resources print the time of day in America/New_York, the recorded host's
  zone. A real server on another host would use that host's zone.
- **URL parsing.** URLs are parsed as WHATWG `new URL` does, for what was recorded and checked.
  Rare hosts differ:
  - Malformed numeric IPv4 hosts (`http://1.2.3.4.5/`) are accepted by the fake, while Node
    refuses them.
  - Hex IPv4 hosts (`http://0x7f.1/`) aren't normalized.
  - IDNA2008 special cases differ (`faß.de`: Node writes `xn--fa-hia.de`, the fake `fass.de`).
- **Rare validation shapes.** Zod's wording is reproduced for every case recorded or exercised
  (tool and prompt arguments, client answers to sampling, elicitation and async tasks, request
  shapes). Unusual nested shapes are approximations, e.g. invalid content inside a sampling
  answer's `content` object.
- **Source-only facts.** The timeout cancellation reason (`McpError: MCP error -32001: Request timed
  out`) and the session-wide event replay on `Last-Event-ID` (`resumption: session`) come from the
  SDK and server source. They weren't recorded, because the recorder always answers the server's
  requests.

**Parity:** see `data/reports/parity.md` (the latest full run, against the real server with
real writes) and the build summary in `data/reports/summary.md`. The server takes no credentials,
so there's no refused-credential scenario.
<!-- /fauxcall:notes -->
