# Changelog

All notable changes to FauxCall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and FauxCall uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.3.1]

MCP fakes enforce their server's size limits and refuse GET streams as it does; refusals of an
unknown protocol version name the version the client sent.

### Added

- MCP fakes enforce the server's limits on a body's size and a batch's length, with its real
  refusals (`max_body_bytes`, `max_batch`); introspection probes the SDKs' defaults (4 MiB, 100
  messages).
- They also refuse a GET stream without `Accept: text/event-stream` or with an unknown
  `MCP-Protocol-Version`, as the real server does.

### Fixed

- A refusal of an unknown protocol version names the version the client sent; it named the one
  introspection had probed with (`1999-01-01`).
- GitHub releases carry the tag's release notes (the workflow didn't fetch the annotated tag, so
  they showed the commit message).

## [0.3.0]

Data that stays consistent in both directions, and MCP fakes that learn their server's HTTP
layer. Relations now say whether every target must be referenced (every author has a book; a city
needs a team only in a sports API) and what the real API does when a write would break a
reference; the fake enforces both, custom handlers included, and every scenario checks it. MCP
introspection learns the server's 404 pages, path matching, CORS, refusal order, SSE format and
priming, and the fake handles cancellation and stream resumption, so a fake no longer needs a
hand-written HTTP layer. Rebuild existing fakes to pick this up: their tests can now fail on a
write that leaves the data inconsistent, and `fauxcall model check` lists the relation rules to
decide.

### Added

- **The MCP fake's HTTP layer is learned, not hand-written.** Introspection records the real
  404 page and method-not-allowed page, whether the MCP path matches in any case or with a
  trailing slash, CORS (preflight and headers), the refusals of bodies that aren't JSON or
  JSON-RPC, and the order in which the server checks a POST; the fake repeats each. A build of the
  Everything fake had written 856 lines of its own HTTP layer for these; the rebuilt example in
  `examples/everything` needs none (100%, independent 100% over 181 of 238 calls).
- Introspection also learns how SSE events are written (field order, event name, streamed or
  with a length), priming by the protocol version the client asked for, and the member order of
  messages the server sends on its own.
- **Cancellation, both ways.** A client's `notifications/cancelled` stops the call and drops its
  answer, as SDK servers do; with `timeout_cancel_reason`, the fake tells the client when its own
  request timed out.
- `resumption: "session"` replays a whole session to a client that reconnects, as the TypeScript
  SDK's event store does.
- `METHOD_HANDLERS["tools/call"]` runs before FauxCall's own checks of a tool call, so a fake can
  answer odd names and arguments the way the real server does.

- **Relations hold in both directions.** `min_per_target` says every target must be referenced
  (every author has a book; a city needs a team only in a sports API), and `world check` enforces
  it. `world check` and `world build` print each relation both ways (references, dangling ones,
  how many targets are referenced), and `analyze relations` shows the reverse coverage of
  candidates. Expansion gives targets short of referrers the first new records.
- **Writes keep references as the real API does.** `on_missing` refuses (or accepts) a write that
  points at a target that doesn't exist, with the endpoint's `missing_reference` error case;
  `on_delete` (restrict, cascade, set null, ignore) is now carried out, refusing with the
  `still_referenced` error case. Custom handlers follow the same rules with
  `ctx.refuse_missing(collection, record)` and `ctx.delete(collection, key)`.
- Generated fakes test that their world is consistent, and every scenario on the in-process fake
  fails if its writes leave a dangling reference (where writes keep them) or break an invariant.
- The `refapi` and `everything` examples are rebuilt with all of this: the Reference API's three
  relations are decided both ways with recorded evidence (100%, independent 100% over 168 of 230
  calls), and the Everything fake learned that its server adds capability tools only after
  `notifications/initialized` (100%, independent 100% over 181 of 238 calls).
- `fauxcall model check` lists each relation rule not decided yet; the skill decides them from
  reverse coverage, the domain and recorded write probes, and says why in `rules_evidence`.

### Changed

- A relation's `on_delete` starts undecided (`null`) instead of `ignore`; undecided rules behave
  as before (writes are accepted, deletes leave references dangling).
- MCP introspection makes about a dozen more calls (the HTTP layer's probes, the listing before
  `notifications/initialized`); the optional ones come last and stop when the budget runs out.

### Fixed

- Lone UTF-16 surrogates in an answer no longer crash the fake; they are escaped, as
  `JSON.stringify` writes them.
- A DELETE ends the session's open GET stream; a client that hangs up leaves no task behind; a
  second GET stream no longer cuts the first one off from pushes when it closes.
- `Content-Type: Application/JSON` (any case) is JSON.
- In scenarios, `mcp.request("tools/call", …)` spends the write budget like `call_tool`.
- Requests an MCP fake sends to the client (sampling, elicitation, roots) are numbered from 0 in
  each session, as the MCP SDKs number them; they were strings (`fauxcall-1`).
- A recorded refusal that the real server sent with a `null` id (a second `initialize`) keeps
  it; the fake put the caller's id in.
- Tools a server offers only to capable clients wait for the client's
  `notifications/initialized` when the real server adds them only then. Introspection learns it
  by listing tools before and after sending it (`capability_tools_after_initialized`).

## [0.2.0]

Full MCP support. A fake of an MCP server now behaves like the real one at every level the protocol
defines: every method answered by code; progress and other notifications as they happen; requests
to the client (sampling, elicitation, roots); background work and pushes on the client's open
stream; tools listed only to capable clients; the real refusals; and the wire itself (response
member order, id shapes, priming events). The new `examples/fauxcall/everything` fake of the MCP
project's Everything reference server, built by a headless run, shows all of it.

### Added

**MCP fakes**

- **Every method can be answered by code.** `METHOD_HANDLERS` (`register_method`) handles dynamic
  resources, prompts, completions, subscriptions, `logging/setLevel` or any other method, falling
  back to the recordings when a handler returns `None`. Without one, the fake answers
  subscriptions, `logging/setLevel` and completions the way the protocol says, and an unknown
  method with the real "method not found". Listings can be answered too (`resources/list` with
  what a session created).
- **Notifications.** `ctx.progress(…)` (when the call carried a `progressToken`) and
  `ctx.notify(…)`; over SSE they reach the client as they happen, before the answer.
  `ctx.push(…)` sends on the client's open GET stream, and `ctx.background(…)` leaves work running
  after a call, ended with the client's session.
- **Requests to the client.** `await ctx.ask_client("sampling/createMessage", …)` (or
  elicitation, roots) sends the request mid-call and returns the client's answer; a refusal or a
  timeout answers the call with the same error, as SDK servers do.
- **Time and sessions.** `ctx.pause(seconds)` takes real time (skipped in fast mode) and every
  pause is noted in `runtime.pauses` for tests; `ctx.mcp_session` is the client's
  `Mcp-Session-Id` and `ctx.client_capabilities` what it declared.
- **Tools for capable clients.** Introspection finds tools a server lists only to clients that
  support sampling, elicitation (form or URL), roots or tasks, alone or together (`requires`), and
  the fake lists them only to such clients.
- **The client's notifications.** A `METHOD_HANDLERS` handler for a notification
  (`notifications/roots/list_changed` …) runs in the background after the client's `202`, and
  can react as the real server does, e.g. by asking for the roots again.
- **The real refusals.** Introspection records how the server refuses a `tools/call` without a
  name, arguments that aren't an object, a second `initialize`, a wrong `Accept` or
  `Content-Type`, an unknown protocol-version header, an unknown session (on POST, GET and
  DELETE) and a second GET stream in a session, and the fake repeats each one.
- **The wire.** The fake writes response members in the real server's order, shapes session and
  SSE event ids like the real ones (UUIDs or hex), sends the empty priming event newer protocol
  versions open SSE answers with, and keeps streams alive with the real comment at the real
  interval, on idle GET streams and during long calls.
- **Recording.** Plans declare the client's capabilities (`client_capabilities`) and how the
  recorder answers the server's requests (`client_replies`), so tools that sample, elicit or ask
  for roots can be recorded. With `"listen": true` the recorder also holds the client's GET
  stream open, records what the server sends there (log messages, resource updates, requests such
  as `roots/list`) and answers its requests; `listen_after_seconds` waits for periodic pushes.
- **Reconnecting.** A fake of a server that numbers its SSE events keeps what it sends, including
  pushes while no stream is open, and replays the rest of a stream to a client that reconnects
  with `Last-Event-ID`, as the real server does.
- An MCP example: `examples/fauxcall/everything`, a fake of the MCP project's Everything reference
  server built by a headless run (100% parity, independent 100% over 160 of 205 calls).

**Testing**

- **Offline test runs refuse the network.** Against the fake alone, any connection off the
  machine, or to the real service, fails the test that tried (even when the fake swallowed the
  error): a fake answers from its recordings and data, never by calling out.
- **Scenarios can be any MCP client.** `await mcp.open(capabilities=…, replies=…)` opens the
  session as a client that supports sampling, roots … and answers the server's requests mid-call;
  `mcp.answer_with(…)` changes the answers. The in-process fake now streams its answers, so this
  works without serving the fake.
- `fauxcall verify --fake-url` runs the scenarios against a fake that is already running.

**Building fakes**

- **Headless runs apply `answers.json` themselves.** `record`, `plan review`/`check` and parity
  runs turn the answers file's write approval into approvals, so the agent never approves anything.
- `fauxcall model tool <name> <tools…>` sets a tool's `--behavior`, `--invalid-args` and
  `--varies`/`--steady`; `fauxcall recordings-delete` removes recordings by id or endpoint.
- Home folders hide their account name (`/Users/<name>`, `C:\Users\<name>`) wherever they appear.

### Changed

- In scenarios, the server's requests to the client get "Method not found" (as from a client
  without those capabilities) unless the scenario answers them, instead of no answer at all.
- **Existing fakes' tests can now fail** if the fake reaches the network: that was a fidelity bug
  the tests couldn't see. Serve what was recorded instead, and answer as the real service does
  when the network fails.
- MCP introspection makes a few more calls (the capability and refusal probes) and, when the
  server offers a GET stream, waits up to 20 seconds on it to learn its keep-alive; the optional
  probes come last and stop when the recording budget runs out.
- MCP's `progressToken` is no longer redacted as a secret.

### Fixed

- `fauxcall serve` and `fauxcall up` run a scaffolded fake's own code. They served only the routes
  the model describes, so tools and methods answered by `handlers.py` answered "no handler".
- MCP prompts, resource reads and completions answer with the recording that matches their
  params; before, the first recording answered every call.
- Parity compares the real side's answers as they would have been recorded (same pseudonyms as
  the fake, credentials redacted), so no real personal data reaches the parity report.
- A crashing MCP handler answers `-32603` instead of a bare HTTP 500, and tool arguments that
  aren't an object are no longer turned into `{}`.
- `model check` counts an MCP fake's tools, resources and prompts; `analyze endpoint` profiles an
  MCP tool's result, not the JSON-RPC envelope; the parity report groups resource reads by
  template; `status` shows a phase's latest note; `init` prints its notes once.
- A checkout's installed `fauxcall` reports the new version after a bump (`uv` rebuilds it when
  `plugin.json` changes), so `doctor --skill-dir` no longer sees a mismatch that isn't there.

## [0.1.3]

### Added

- **The parity report says what its score rests on.** Next to the overall compliance it gives the
  *independent* score, without the calls that only repeat a recorded request (which the fake
  answers from that recording) or compare shape only, with how many calls each covers. It lists
  endpoints, tools and MCP listings no scenario compared, and says when no scenario checks a
  refused credential. `fauxcall report` and the terminal summary show the same; the skills report
  both scores and close the gaps. The gate is unchanged (overall score, 98%).
- MCP calls refused at the HTTP level (a `401`, a `403`) keep that status in parity comparisons.

- `fauxcall analyze access` shows which endpoints and tools the credentials were refused for (the
  fake will refuse them too); the skill checks this before planning the build.
- `fauxcall plan template` works for MCP tools (arguments from the input schema), resources and
  prompts.
- `fauxcall recordings` shows MCP calls with their tool, arguments, how they answered and the
  answer's text, and `--jsonl` prints matching recordings in full for scripts.
- MCP plans can send a call without credentials or with a bad one (`"auth": "none" | "invalid"`).
- `fauxcall init` with credentials records how the server refuses calls without them, and its OAuth
  discovery documents when it has some, so the fake refuses and signs in clients the same way.
- `analyze endpoint --apply` learns how an MCP tool refuses arguments that break its schema
  (`invalid_args`) and whether it answers the same arguments differently (`varies`: replays and
  parity then compare its answers by shape only).
- `fauxcall doctor --skill-dir` checks that the running skill and the CLI come from the same
  release, and says how to update whichever is behind. The skills run it first and stop on a
  mismatch.
- MCP tool handlers can answer with a JSON-RPC error (`raise RpcError(code, message)`) or a whole
  HTTP reply (`raise EarlyResponse(...)`, e.g. a bare `403`), and keep per-session state in
  `ctx.state`, which `reset` clears.
- Account ids are pseudonymized wherever they appear: Stripe's `acct_…` always, plus the patterns
  in `privacy.json` `identifiers` (a named group `id` covers an account id embedded in other ids).
- `fauxcall env check` works before `init`, on a folder holding only the prepared `.env`, and
  shows each header's shape (`Authorization (Bearer …)`, or a missing scheme) without its value.

### Changed

- **Approvals cover exactly the reviewed calls.** `plan approve --planned` approves each write in
  the plans as reviewed, its `repeat` times, for recording; editing it, or adding a write, needs
  approving again (`--call <id>` approves one again). Parity runs get their own budget per
  endpoint (`--max-calls`, 100 by default). `plan review` shows new writes, what stays on the real
  service (no cleanup, or a cleanup that failed) and calls the plans declare read-only. A write
  whose connection failed gives its approval back.
- The fake's MCP endpoint is `/` when the server's URL has no path (it was `/mcp`).
- A UUID under a secret-looking key is redacted to a UUID-shaped placeholder, not `[REDACTED]`.
- Connecting to an MCP server says when the credentials were refused (with the server's
  `WWW-Authenticate` and what to fix), names what each transport answered, and retries a passing
  Streamable HTTP failure once.
- A fake repeats the real "unknown tool/resource/prompt" errors with the caller's name instead of
  the probe's, and listing results keep the real server's other members (`ttlMs`, `_meta` …).
- `fauxcall record` lists calls that failed and exits with an error when any did; MCP calls show
  how they answered (`200 isError`, `200 error -32602`).
- MCP fakes' READMEs give a ready MCP client configuration and say which credentials to send;
  `fauxcall up` includes those headers too. The generated CI installs from the lockfile
  (`--locked`) with Python 3.12.
- The skill never waits on a question after the up-front gate, runs the independent review in the
  foreground, and `fauxcall status` flags a review that never finished.
- `FAUXCALL_LOG_LEVEL` accepts any case (`info`).
- `init` on a service that refuses the credentials says so (whether or not `--mcp` was given),
  creates the fake's `.env` with commented examples when it's missing, and prints its path.
- Calls to tool names the server doesn't list are probes, not writes: they need no approval in
  recordings or parity runs.
- Generated starter scenarios for MCP tools come only from recordings that returned a result.
- Generated fakes' mypy settings let `tests/unit` and `tests/e2e` have their own `conftest.py`.
- `fauxcall verify` runs the fake's tests without the caller's `VIRTUAL_ENV` (no uv warning).

### Security

- The generated `compose.yaml` keeps the real API's credentials and the pseudonym key out of the
  fakes' containers: a running fake never needs them.

### Fixed

- Recorded requests are replayed on the clock of the moment they were recorded, so a replay can't
  start failing when the date moves on (Frankfurter's replays of then-future dates broke on
  2026-10-12). Existing fakes get this without regenerating.

## [0.1.2]

### Fixed

- `fauxcall init` reads credentials from the fake's `.env` (`FAUXCALL_REAL__HEADERS`), so a
  service that needs a key just to connect, like an MCP server behind a token, works without
  exporting it to the shell. The skill asks for such credentials before init and never touches
  `.env` itself.

## [0.1.1]

### Changed

- Approvals allow 100 real calls per endpoint by default (`FAUXCALL_APPROVAL_BUDGET`), and the
  skill asks for the number at the start of every build; approving an endpoint again adds calls.
- The README is easier to read, explains how to use FauxCall with other agents (Codex, Gemini CLI,
  Cursor, Copilot) and why it depends on `httpx2` and `httpcore2`.

### Fixed

- The call journal keeps deeply nested JSON as text on every Python version.

## [0.1.0]

The first release.

### Added

- The `fauxcall` Claude Code plugin, with the `/fauxcall` skill to build fakes and the
  `/fauxcall:refresh`, `/fauxcall:verify`, `/fauxcall:extend` and `/fauxcall:serve` companion
  skills.
- Inputs: OpenAPI 3.x and Swagger 2.0 documents, Swagger UI and Redoc pages, free-form API
  documentation pages, and remote MCP servers over Streamable HTTP or the SSE transport.
- Recording: paced and capped real calls, one up-front approval for mutating calls (100 real calls
  per endpoint unless the user picks another number, `FAUXCALL_APPROVAL_BUDGET`), credential
  redaction and consistent pseudonymization of personal data (a sandbox's public demo values can be
  kept exactly).
- Analysis: schema and field-profile inference, time-format detection, clock-following fields,
  relations, async-job state machines, write semantics, error shapes, and ad-hoc SQL over
  recordings.
- The engine:
  - Consistent worlds with relations and invariants, and per-session state and clocks.
  - Time-aware field policies, including trading calendars.
  - The real API's errors, auth, latency, rate limits, pagination and routing quirks.
  - Async jobs, file uploads and downloads, Server-Sent Events, chunked streams and WebSockets.
  - MCP servers with OAuth.
  - An admin API: reset, clock control, the call journal, fault injection and users.
- Generated fakes: uv/FastAPI projects with tests, a Dockerfile, compose and CI, which pass ruff,
  mypy --strict and a 95% coverage gate out of the box.
- Parity testing: a pytest plugin that runs scenarios against the real service and the fake, and
  scores them with weighted checks (98% required). Its real writes spend the same approved budget
  as recording.
- Optional Jev (TypeSafe) judgments for matching unseen requests, semantic outputs and job
  outcomes, with cached answers and deterministic fallbacks.
- Examples: fakes FauxCall built on its own while it was validated, from a single prompt each.

[Unreleased]: https://github.com/bradnemetski/fauxcall/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/bradnemetski/fauxcall/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/bradnemetski/fauxcall/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bradnemetski/fauxcall/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/bradnemetski/fauxcall/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/bradnemetski/fauxcall/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/bradnemetski/fauxcall/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bradnemetski/fauxcall/releases/tag/v0.1.0
