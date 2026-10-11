---
name: serve
description: Start one or more FauxCall fakes and wire an agent to them — base URLs, MCP client configuration, sessions, clock control, fault injection and the call journal for evaluations. Use when the user runs /fauxcall:serve [names…], or asks to run fakes, point an agent at a fake, or set up an evaluation against fakes.
license: Apache-2.0
compatibility: Needs uv and a shell (Docker optional).
metadata:
  author: Brad Nemetski
  version: "0.1.0"
allowed-tools: Bash(fauxcall *) Bash(uvx fauxcall *)
---

# Serve fakes for an agent

Arguments: fake names (default: all in `./fauxcall`), optionally `--fast` (no latency/rate-limit
emulation).

1. `fauxcall up [names…] [--fast]` — starts each fake in the background and prints its base URL and,
   for MCP fakes, a ready-to-paste MCP configuration (`{"mcpServers": {…}}`). `fauxcall ps` shows
   status, `fauxcall down` stops them. Containers: `docker compose up` in `./fauxcall`.
2. Wire the agent: replace the real base URL with the fake's (SDK `base_url`, environment variable,
   or the MCP config). Credentials: any well-formed value works unless the fake runs in allowlist mode.
3. Make runs repeatable and observable with the admin API (`/__fauxcall` on each fake):
   - `POST /__fauxcall/reset` before each run (or per session: `{"session": "<id>"}`), and send
     `X-Fauxcall-Session: <id>` from the agent to isolate parallel runs;
   - `POST /__fauxcall/clock` `{"op": "freeze" | "travel" | "advance" | "scale" | "real", …}` to test
     time-dependent behavior (market closed, a job finishing);
   - `PUT /__fauxcall/faults` to inject errors/timeouts/429s/disconnects;
   - `PUT /__fauxcall/auth/users` to test denied/expired users;
   - `GET /__fauxcall/journal?session=<id>` afterwards to assert on what the agent actually did.
4. Tell the user the URLs, the configuration snippet, and the admin calls their eval harness needs.
