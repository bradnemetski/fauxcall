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
fauxcall init https://mcp.example.com/mcp --mcp --auth-env MY_TOKEN   # forced, with a bearer token
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
| `protocol_versions`, `default_protocol_version` | negotiation (requested → answered) |

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
  in 250 ms and answers in 15 s keeps both.

Errors for unknown tools/methods/missing sessions come from `model.mcp.errors` (recorded by the probes).

## Auth (OAuth)

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
```

Also run the official SDK against the fake once (the generated unit tests do) — real MCP clients must
connect without noticing anything.
