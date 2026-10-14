# Faking a remote MCP server

An MCP fake must be indistinguishable to MCP clients: same server info, capabilities, instructions,
tool definitions (verbatim — descriptions are prompts for the agent), transport behavior, results and
errors. FauxCall speaks MCP at the wire level and reproduces all of it.

## Contents

- [Init and introspection](#init-and-introspection)
- [Transport behavior](#transport-behavior)
- [Recording tool calls](#recording-tool-calls)
- [Tool behaviors](#tool-behaviors)
- [Auth (OAuth)](#auth-oauth)
- [Testing](#testing)

## Init and introspection

```bash
fauxcall init https://mcp.example.com/mcp            # detected as MCP automatically
fauxcall init https://mcp.example.com/mcp --name demo   # uses the headers in fauxcall/demo/.env
fauxcall init https://mcp.example.com/mcp --mcp --auth-env MY_TOKEN   # or a token from the shell
fauxcall mcp introspect <name>                       # re-run later (e.g. after adding credentials)
```

Introspection records `initialize`, every listing (`tools/list`, `resources/list`,
`resources/templates/list`, `prompts/list` — following `nextCursor`), and probes: GET on the endpoint,
a request without a session id, an unknown method, an unknown tool, and protocol-version negotiation.
The results land in the model's `mcp` section.

## Transport behavior

`model.mcp.transport` (filled by introspection — check it):

| field | meaning |
|---|---|
| `kind` | `streamable_http` or legacy `sse` (with `messages_path`) |
| `path` | the endpoint path (`/mcp`, `/sse`) |
| `response_mode` | answers as `json` or `sse` |
| `issues_session`, `session_header`, `requires_session`, `missing_session_status` | session handling |
| `get_stream`, `get_status` | `sse` (GET opens a stream) or `status` (GET answered with `get_status`, usually 405) |
| `delete_status` | DELETE response |
| `protocol_versions`, `default_protocol_version` | negotiation (requested → answered); the default is what the real server answers an unsupported version (probe one), and wins over the recorded handshake's version |

## Recording tool calls

Plans use endpoint ids `MCP tools/call:<tool>` (and `MCP resources/read:<uri>`, `MCP prompts/get:<name>`);
`request.json_body` holds the JSON-RPC **params**:

```json
{"id": "ask-1", "endpoint": "MCP tools/call:ask_question", "purpose": "sample",
 "request": {"method": "POST", "path": "/mcp",
             "json_body": {"name": "ask_question",
                           "arguments": {"repoName": "facebook/react", "question": "What is Fiber?"}}}}
```

Per the MCP spec a tool may modify state unless annotated `readOnlyHint: true`; such tools need the
up-front approval like HTTP writes (approve e.g. `--endpoint "MCP tools/call:*"` for a read-only
research server after checking its tools). Record, per tool: 5–15 realistic and varied argument sets,
argument edge cases, invalid arguments (wrong types, missing required) and inputs that produce tool
errors (`isError: true`).

## Tool behaviors

`model.mcp.tools[]` keeps each `definition` verbatim and adds:

- `behavior`: `match` (exact recorded arguments, else the closest recording — Jev when available,
  lexical otherwise), `replay` (same, recorded answers only), `computed` (a handler in
  `handlers.TOOL_HANDLERS`), `error` (a tool that always fails: the recorded failure, else
  `invalid_template`, else an `isError` result "Error executing tool <name>").
- `semantic.match` — `inputs` (`args.<path>`), `exact` inputs that must match (e.g. the repository),
  `min_confidence`, `fallback` (`lexical` | `first` | `none`).
- `invalid_args` — how the real server reports schema violations (`tool_error` result,
  `jsonrpc_error`, or `accept`), and `invalid_template` (a recorded error message, re-addressed).
- `latency` — from `fauxcall analyze endpoint <name> "MCP tools/call:<tool>" --apply`, which times
  answers and errors (JSON-RPC errors, `isError` results) apart: a tool that refuses bad arguments
  in 250 ms and answers in 15 s keeps both. The same command sets `invalid_args` from your
  recorded calls whose arguments break the schema, and `varies` (below).
- `varies` — the real tool answers the same arguments differently (search ranking, generated
  text): record a few calls with the same arguments, and `analyze endpoint --apply` sets it when
  most differ. Replays and parity then compare its answers by shape only, not values or lengths.
  Never skip or xfail such recordings in tests instead.

Errors for unknown tools, resources, prompts and methods and for missing sessions come from
`model.mcp.errors` (recorded by the probes); the fake puts the caller's name where the real error
repeated the probe's. Members of listing results besides the list (`resultType`, `ttlMs`, `_meta`)
are replayed from `model.mcp.listing_extras`.

## Computed tools

A `computed` tool's handler (`handlers.TOOL_HANDLERS`) returns the tool result. It can also answer
the way real servers do, without patching FauxCall:

```python
from fauxcall.runtime import EarlyResponse, RequestContext, RpcError
from fauxcall.types import JsonObject, JsonValue
from starlette.responses import Response


async def create_price(ctx: RequestContext, arguments: JsonObject) -> JsonValue:
    if "unit_amount" not in arguments:  # the real server's JSON-RPC error
        raise RpcError(-32602, "Invalid params: unit_amount is required")
    if arguments.get("product") in ARCHIVED:  # a bare HTTP refusal, as recorded
        raise EarlyResponse(
            Response(
                status_code=403, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'}
            )
        )
    prices = ctx.state.setdefault("prices", [])  # per session, cleared by reset
    ...
```

`EarlyResponse` answers the whole HTTP request (and takes the tool's recorded error time);
`ctx.state` is state for what the world can't hold, per FauxCall evaluation session
(`X-Fauxcall-Session`); `ctx.mcp_session` is the connected client's `Mcp-Session-Id`, to key
state per client. A handler that crashes answers `-32603`, as SDK servers do. Set a tool's
behavior with `fauxcall model tool <name> <tools…> --behavior computed` (also `--invalid-args`,
`--varies`/`--steady`), not by editing `data/model.json`. Never monkeypatch FauxCall classes:
if a behavior can't be expressed, say so in the model's `notes`.

## What the runtime serves

| method | the fake answers with |
|---|---|
| `initialize`, `ping` | the recorded server info, capabilities and negotiated version; `{}` |
| `tools/list`, `resources/list`, `resources/templates/list`, `prompts/list` | a `METHOD_HANDLERS` handler (to add what a session created), else the recorded listing, paginated as recorded; tools with `requires` only to clients whose capabilities satisfy it, and only after `notifications/initialized` when the real server waits for it (`capability_tools_after_initialized`, learned by introspection) |
| `tools/call` | its handler (`computed`), else the closest recording; schema checks per `invalid_args`; a call without a name, or with arguments that aren't an object, gets the real server's recorded refusal |
| a second `initialize` in a session | the real refusal, when the server refused it during introspection |
| `resources/read`, `prompts/get`, `completion/complete` | a `METHOD_HANDLERS` handler, else the recording that matches its params, else the real "not found" error (`completion/complete`: no values) |
| `resources/subscribe` / `unsubscribe`, `logging/setLevel` | a handler or recording, else `{}` when the server advertised it |
| anything else (`tasks/*` …) | a handler or recording, else the real "method not found" |
| a notification from the client (`notifications/roots/list_changed` …) | `202` at once; its `METHOD_HANDLERS` handler, if any, runs in the background |
| a POST with the wrong `Accept` or `Content-Type`, an unknown `MCP-Protocol-Version`, or an unknown session id | the real HTTP refusal, recorded by introspection |
| a GET or DELETE for an unknown session, or a second GET stream in a session | the real HTTP refusal, recorded by introspection |

The wire itself follows the recordings too: response members in the real server's order, session
and SSE event ids shaped like the real ones (UUIDs or hex), the empty priming event that newer
protocol versions open SSE answers with (for the versions that sent one), and the real
keep-alive comment (`: keepalive` …) at the real interval, on an idle GET stream and on a long
call's stream. Introspection waits up to 20 seconds on the GET stream to learn the keep-alive.

`METHOD_HANDLERS` (in `handlers.py`) maps any method to `async (ctx, params) -> result`; return
`None` to let the recordings answer (compute dynamic resources, replay the static ones). Both kinds
of handler can send notifications: `ctx.progress(step, total, message)` (only when the call
carried a `progressToken`, as on a real server) and `ctx.notify("notifications/message", {...})`.
Over SSE they reach the client as they happen, before the answer. Outside any call, a server
can also send on the client's open GET stream (resource updates, periodic log messages):
`ctx.push(method, params)` sends there, and `ctx.background(work)` keeps running
`async work(channel)` (with `channel.push(...)` and `await channel.pause(seconds)`) until it ends,
is cancelled (keep the returned task in `ctx.state` to stop it) or the client ends its session.
`ctx.client_capabilities` holds what the client declared (sampling, elicitation …), to answer
differently for clients that can do more. A handler registered for a client notification
(`"notifications/roots/list_changed"`) reacts to it as the real server does, e.g. by asking the
client again with `await ctx.ask_client("roots/list")`; it runs in the background, its result is
ignored, and a crash is only logged. Take the real time between them
with `await ctx.pause(seconds)`: skipped in fast mode (tests), and noted in `runtime.pauses` either
way, so a test checks the timing by reading it (never by replacing FauxCall's private sleep).

**Requests to the client.** A handler can ask the client, mid-call, as real servers do:
`await ctx.ask_client("sampling/createMessage", {...})` (or `elicitation/create`, `roots/list`)
sends the request on the call's SSE stream (else the client's GET stream) and returns the
client's answer. A refusal, or no answer within 60 s (`within=` to change it), raises
`ClientError`; let it propagate and the call answers with that error (`-32001 Request timed out`),
as an SDK server does. To record such tools, a plan declares the client's capabilities
(`"client_capabilities": {"sampling": {}}`) and each call says how the recorder answers the
server's requests (`"client_replies": {"sampling/createMessage": {...a result...}}`, or
`{"error": {...}}` to refuse); unlisted requests get "Method not found".

**What the server sends outside a call.** Many servers also send on the client's GET stream: log
messages, resource updates, and requests such as `roots/list`. To record them, set
`"listen": true` in the plan: the recorder holds the GET stream open for the session, records
everything sent there with its timing (endpoint `MCP GET:stream`) and answers the server's
requests with the running call's `client_replies`; `"listen_after_seconds": 12` keeps listening
after the last call, for periodic pushes. Without it, a server that asks for roots on the GET
stream gets no answer, and the recording shows only its timeout.

**Reconnecting.** A server that numbers its SSE events (`sse_event_ids`) keeps what it sends, and
a client that reconnects with `Last-Event-ID` gets the rest of that stream, including pushes sent
while no stream was open; the fake does the same (`ctx.push` returns `False` then, but the message
is kept). A server without event ids drops them, and so does its fake.

Tools some servers list only to clients that support sampling, elicitation (form or URL), roots or
tasks are found by introspection (one session per capability, then one declaring them all) and
carry `requires`: alternatives, each a set of capability paths a client must all declare
(`[["sampling"], ["tasks.requests", "sampling"]]`).

## Auth (OAuth)

`fauxcall init` with credentials also calls the server **without** them. It records the refusal as
the `unauthorized` error template, sets `auth` (the way your credentials are sent, plus `oauth2`
when the server publishes OAuth discovery documents, which it saves), so the fake refuses and signs
in clients the same way. To record other refusals, set `"auth": "none"` or `"auth": "invalid"` in a
plan entry's `request`: the call goes without credentials, or with a bad one, outside the session.

When the real server uses OAuth, the fake serves the same discovery documents (rewritten to its own
URL) and a **browserless** authorization server: dynamic registration, `/authorize` (approves
immediately as the configured user — `login_hint` or `FAUXCALL_AUTH__DEFAULT_USER`), `/token` (PKCE,
refresh), `/revoke`. Denied users get `access_denied`; expired or denied tokens get the real 401/403.
Configure `auth.schemes: [{"kind": "oauth2", "oauth": {"scopes": […], "token_ttl_seconds": 3600}}]`
and the auth error templates.

## Testing

```python
async def test_ask_about_hooks(mcp: McpClient) -> None:
    result = await mcp.call_tool(
        "ask_question", {"repoName": "facebook/react", "question": "What are hooks?"}
    )
    assert result["content"][0]["type"] == "text"


async def test_sampling_tool(mcp: McpClient) -> None:
    # open the session as a client that can sample, answering the server's request mid-call
    await mcp.open(
        capabilities={"sampling": {}},
        replies={
            "sampling/createMessage": {
                "role": "assistant",
                "model": "m",
                "content": {"type": "text", "text": "hi"},
            }
        },
    )
    result = await mcp.call_tool("trigger-sampling-request", {"prompt": "Say hi"})
```

`mcp.open(...)` comes before the scenario's first request (which otherwise opens a session without
capabilities); `mcp.answer_with(replies)` changes the answers mid-scenario. By default the client
answers the server's requests with "Method not found", like a client without those capabilities.
This works in-process and against a served fake alike: never reach into `mcp.wire` or skip such
scenarios in-process.

Also run the official SDK against the fake once (the generated unit tests do) — real MCP clients must
connect without noticing anything.
