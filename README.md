# FauxCall

**Turn a real API or MCP server into a fake your agents can't tell apart from the original.**

FauxCall is a [Claude Code](https://claude.com/claude-code) plugin and a Python tool. Point it at a
Swagger/OpenAPI page, an API documentation page or a remote MCP server. It samples the real thing
one endpoint at a time and writes a fake with exactly the same paths, parameters, payloads, errors
and MCP tools. It then tests the fake against the real service until the two agree on at least 98%
of weighted checks.

```text
/fauxcall https://petstore3.swagger.io
```

## Why

Agents are hard to test against real services. Responses change, data disappears, calls cost money
and get rate-limited, and nobody wants an evaluation run to really place an order. Hand-written
mocks have the opposite problem: they drift from the real API, return data that doesn't add up, and
ignore time altogether.

FauxCall fakes are built from real responses, so they behave like the original:

- **Same signature.** Every path, parameter, status code, header, error body and float spelling
  (`1.50` stays `1.50`), and every MCP tool, resource and prompt, verbatim.
- **Consistent data.** Every ticker has a valid exchange; an uploaded document appears in
  `upload_status` and moves through the same states, with the same timing, as on the real API.
- **Real time.** `GET current_time` returns the actual time. A stock's time field follows the clock
  during market hours and stays at the last tick overnight, while its price stays put. Timestamps
  keep their exact format, including the time zone.
- **Real-world behavior.** Recorded latency, rate limits with real `429`s and headers, pagination,
  async jobs, file uploads and downloads, Server-Sent Events, chunked streams, WebSockets, and MCP
  over Streamable HTTP or SSE, including OAuth.
- **Built for evaluations.** Control the clock, inspect a journal of every call, inject faults,
  isolate parallel runs in sessions, and reset to the recorded state between runs.

## Quick start

You need [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/). uv
installs the right Python for you.

1. Install the plugin:

   ```bash
   claude plugin marketplace add bradnemetski/fauxcall
   claude plugin install fauxcall@fauxcall
   ```

   Inside Claude Code you can use `/plugin marketplace add bradnemetski/fauxcall` and
   `/plugin install fauxcall@fauxcall` instead.

2. Build a fake. In a Claude Code session, run the skill on a spec, docs page or MCP server:

   ```text
   /fauxcall https://petstore3.swagger.io
   /fauxcall https://api.frankfurter.dev/v1/openapi.json
   /fauxcall https://mcp.deepwiki.com/mcp
   ```

   Claude asks you a few questions once, up front: credentials, whether you have a Jev key, and
   approval for any calls that change data on the real service. It then records, models,
   scaffolds and tests the fake on its own. Builds are checkpointed: run `/fauxcall` again to
   resume an interrupted one.

3. Run the fake and point your agent at it:

   ```bash
   uvx fauxcall up            # starts every fake in ./fauxcall and prints their URLs
   ```

   Use the printed base URL in place of the real one. For MCP servers, FauxCall also prints the
   configuration to add to your agent.

## What you get

Each fake is a small, ordinary FastAPI project under `./fauxcall/<api>/`:

```text
your-project/
├── fauxcall/
│   ├── petstore/
│   │   ├── data/model.json          # everything the fake knows (validated, human-editable)
│   │   ├── data/recordings/         # the real exchanges it was built from
│   │   ├── data/world/              # the consistent dataset it serves
│   │   ├── src/fauxcall_petstore/   # routes + your custom handlers
│   │   ├── tests/                   # unit, replay and end-to-end tests
│   │   └── Dockerfile
│   ├── compose.yaml                 # every fake in containers
│   └── pyproject.toml               # a uv workspace of all your fakes
└── .github/workflows/fauxcall.yml   # CI that keeps the fakes tested
```

Run a fake's tests offline, or against the real service to check it hasn't drifted:

```bash
cd fauxcall/petstore
uv run pytest                                 # unit + offline end-to-end tests
uv run pytest tests/e2e --target=parity       # real and fake side by side; fails below 98%
```

## Using fakes in evaluations

Every fake has an admin API under `/__fauxcall`:

| Call | What it does |
|---|---|
| `POST /__fauxcall/reset` | Back to the recorded state, for all sessions or one |
| `POST /__fauxcall/clock` | Freeze, travel to an instant, run faster, or return to real time; `advance` also lifts rate limits |
| `GET /__fauxcall/journal` | Every call the agent made, with requests and responses |
| `PUT /__fauxcall/faults` | Inject errors, timeouts, delays, disconnects or 429s |
| `PUT /__fauxcall/auth/users` | Allowed, denied and expired users (also for MCP OAuth) |

Send `X-Fauxcall-Session: <run id>` with requests to give each parallel run its own state and
clock. Set `FAUXCALL_FAST=1` to switch off latency and rate-limit emulation.

## Companion skills

| Skill | Use it to |
|---|---|
| `/fauxcall <url>` | Build a fake, or resume a build |
| `/fauxcall:refresh <name>` | Re-record and update a fake after the real API changes |
| `/fauxcall:verify <name>` | Run the parity tests against the real service and fix gaps |
| `/fauxcall:extend <name>` | Add endpoints, scenarios or behavior to an existing fake |
| `/fauxcall:serve` | Start fakes and wire them into an agent's configuration |

The skills follow the open [Agent Skills](https://agentskills.io) format, so other agents that read
`SKILL.md` files can use them too. Claude Code is the primary target.

## Privacy and safety

- **Credentials stay out of everything.** They live only in each fake's git-ignored `.env`, which
  you edit, and are redacted from recordings, logs and the call journal.
- **Personal data is pseudonymized** at recording time, consistently across endpoints, so
  relations between records survive.
- **Nothing that changes data runs without your approval.** Mutating calls are listed for you to
  approve once, before recording starts.
- **Rate limits are respected.** Recording is paced and capped.

## Jev (optional)

If you have a [TypeSafe](https://typesafe.ai) Jev key, fakes can use it at runtime for judgments
recordings can't cover, such as which recorded answer best fits a new question. Answers are cached
and committed with the fake, so it replays them without a key. Without a key, everything falls back
to deterministic rules.

## The `fauxcall` command

The skills drive the `fauxcall` command-line tool, and you can use it directly:

```bash
uvx fauxcall status                 # where each fake's build stands
uvx fauxcall serve petstore         # run one fake in the foreground
uvx fauxcall verify petstore --target parity
uvx fauxcall --help                 # everything else
```

## Learn more

- [How it works](docs/architecture.md): the pipeline, the model and the runtime
- [Configuration](docs/configuration.md): every setting of fakes and of the CLI
- [Design decisions](docs/decisions.md): what was decided and why
- [Contributing](CONTRIBUTING.md) and [security](SECURITY.md)

## License

[Apache-2.0](LICENSE)
