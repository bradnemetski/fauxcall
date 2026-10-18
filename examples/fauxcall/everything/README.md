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
Built from @modelcontextprotocol/server-everything 2026.8.31 (TypeScript SDK 1.32.1), recorded on
2026-10-18 (34 recording plans, 497 exchanges). Every tool except `get-env` is computed
(`src/fauxcall_everything/`); `tests/unit/test_plans.py` replays every plan in one session and
matches the recorded answers value for value. Parity with the real server: 100% (independent
100%).

What differs from the real server, and why:

- **No network.** `gzip-file-as-resource` reads `data:` URLs itself; for its default URL (the
  servers repository README on GitHub) it serves the README recorded on 2026-10-18 (after the
  recorded 132 ms); any other `http(s)` URL answers `fetch failed`, as the real server does when
  the network fails. The real server would fetch it (and the README changes upstream).
- **`get-env`** replays the recorded environment of the real process, including its
  `PORT=3101`, whatever port the fake listens on.
- **Roots.** The real server asks for roots on the client's GET stream, 350 ms after
  `notifications/initialized` (on the wall clock, also in the fake's fast mode). With no GET
  stream open its request is lost and `get-roots-list` answers after the SDK's 60 s timeout; the
  fake fails the request at once and waits out the minute only when not in fast mode. A client
  that refuses the request gets the "no roots configured" answer at once, on both.
- **Wall-clock behavior.** Research task stages (1 s each), `tasks/result` polling (1 s), task
  TTLs (5 min), the 60 s wait for a clarification and the simulated log messages and resource
  updates (every 5 s) run on the wall clock, as on the real server, even in fast mode; moving the
  fake's clock changes timestamps, not these timers. `trigger-long-running-operation` and the
  async tools' 1 s polls take real time unless fast. Times of day are the real host's:
  America/New_York, en-US (`10:29:02 PM`).
- **Random values** (simulated log levels, generated elicitation ids, task ids, session ids) are
  random on both sides, so they differ run to run.
- **From the source, not recorded** (implemented as the server and SDK code do it): a gzip resource
  registered under a name another session used removes it from that session; `tasks/list` pages of
  ten; task expiry after its TTL; a number or boolean clarification fails the research task
  (`state.clarification.includes is not a function`); one `notifications/tools/list_changed` per
  capability-dependent tool at the first `notifications/initialized` (kept for resumption; a
  client's first GET stream doesn't see them), and nothing at a second one; a protocol version the
  server doesn't support is answered with the latest, `2025-11-25` (`2024-10-07` is supported).
- **Transport gaps of FauxCall's runtime** (not changeable from this fake): the
  `MCP-Protocol-Version` refusal repeats the recorded text, which names `1999-01-01` whatever
  was sent; GET requests aren't checked for `Accept` or `MCP-Protocol-Version` (the real server
  answers 406 and 400); the SDK's limits (4 MiB bodies → 413, batches over 100 messages, one
  `initialize` per batch) aren't enforced; resumed streams replay priming events with empty data
  (the SDK writes `data: {}`); after a client cancels `trigger-long-running-operation` the real
  server keeps sending progress on the open stream, the fake only keep-alives; a POST's SSE
  headers go out with the first event (or after 15 s), not at once.
- **Approximations.** URLs are parsed like WHATWG `new URL()` for http(s), data and the server's
  `demo:` URIs, including IPv6 and IPv4 number forms (`127.1`) and percent-encoded or IDNA hosts
  (IDNA 2003 rather than UTS 46); zod's error JSON is reproduced for the shapes recorded
  (unions list every alternative; the content discriminator names its options) and follows zod
  v4's source beyond them.
- **Not compared by parity:** what the server sends outside a call's answer (GET-stream log
  messages, resource updates, list changes, task status, roots requests) and progress
  notifications. `tests/unit/test_streams.py` checks them against what the recorded GET streams
  show (000294, 000310, 000323, 000332, 000367, 000394, 000471, 000500); the cross-session
  removal, roots changes, clarification timeout and TTL expiry there follow the source.
<!-- /fauxcall:notes -->
