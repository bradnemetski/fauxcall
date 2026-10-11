# FauxCall

**Build a faithful fake of any API or MCP server, so you can test your agents without touching the
real thing.**

Give FauxCall an API's Swagger/OpenAPI page, its documentation, or the address of an MCP server. It
calls the real service, one endpoint at a time, and builds a local copy that answers the same way:
the same URLs, parameters, responses and errors. Then it runs the same tests against both and
keeps fixing the fake until they agree at least 98% of the time.

```text
/fauxcall https://petstore3.swagger.io
```

## Why

Testing agents against real services is painful. The data changes between runs, calls cost money
and hit rate limits, and you really don't want a test run to place an actual order. Hand-written
mocks avoid that but bring their own problems: they slowly drift away from the real API, their data
doesn't hang together, and they ignore time completely.

A FauxCall fake is built from real responses, so it behaves like the original:

- **It looks the same.** Every URL, parameter, status code, header and error message matches, down
  to how numbers are written (`1.50` stays `1.50`). MCP fakes have the same tools, resources and
  prompts.
- **Its data adds up.** If a stock references an exchange, that exchange exists. If you upload a
  file, it shows up in the status endpoint and goes through the same steps, at the same pace, as on
  the real API.
- **It knows what time it is.** Asking for the current time gets you the actual time. A stock's
  timestamp moves during trading hours and stops at the last trade overnight, while the price stays
  where it closed. Dates come back in exactly the real format.
- **It behaves like the real thing under load.** Real response times, the real rate limits (with
  proper `429` errors), pagination, background jobs, file uploads and downloads, live streams
  (Server-Sent Events and WebSockets), and MCP over HTTP, including sign-in with OAuth.
- **It's made for testing.** You can freeze or move the clock, see every call your agent made,
  inject failures, give parallel test runs their own separate state, and reset everything between
  runs.

## Quick start (Claude Code)

You need [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/). uv
takes care of installing Python.

1. **Install the plugin:**

   ```bash
   claude plugin marketplace add bradnemetski/fauxcall
   claude plugin install fauxcall@fauxcall
   ```

   (Inside Claude Code, `/plugin marketplace add bradnemetski/fauxcall` and
   `/plugin install fauxcall@fauxcall` do the same.)

2. **Build a fake.** In Claude Code, point the skill at a spec, a docs page or an MCP server:

   ```text
   /fauxcall https://petstore3.swagger.io
   /fauxcall https://api.frankfurter.dev/v1/openapi.json
   /fauxcall https://mcp.deepwiki.com/mcp
   ```

   Before it starts, Claude asks you everything it needs in one go: any credentials, whether you
   have a Jev key, and permission for calls that would change data on the real service, along with
   how many such calls it may make. After that it works on its own. If it gets interrupted, run the
   same command again and it picks up where it stopped.

3. **Run the fake and point your agent at it:**

   ```bash
   uvx fauxcall up       # starts every fake in ./fauxcall and prints their addresses
   ```

   Use the printed address instead of the real API's. For an MCP server, FauxCall also prints the
   snippet to paste into your agent's configuration.

## Using it with other agents

FauxCall isn't tied to Claude Code. Its skills are plain [Agent Skills](https://agentskills.io)
(`SKILL.md` files), and all the real work happens in the `fauxcall` command, so any agent that
reads skills and can run shell commands can use it: Codex, Gemini CLI, Cursor, GitHub Copilot and
others.

1. **Install the `fauxcall` command:**

   ```bash
   uv tool install fauxcall                                       # once it's on PyPI
   uv tool install git+https://github.com/bradnemetski/fauxcall   # or straight from GitHub
   ```

2. **Copy the skills to where your agent looks for them.** Most agents read the shared
   `~/.agents/skills/` folder (or `.agents/skills/` inside a project); some also have their own,
   such as `~/.cursor/skills/` or `.github/skills/`. Check your agent's documentation.

   ```bash
   git clone https://github.com/bradnemetski/fauxcall
   mkdir -p ~/.agents/skills
   cp -R fauxcall/plugins/fauxcall/skills/* ~/.agents/skills/
   ```

   That installs five skills: `fauxcall`, `refresh`, `verify`, `extend` and `serve`. If those last
   four names clash with skills you already have, copy just `fauxcall`. It covers a whole build on
   its own.

3. **Ask your agent to use it**, for example "use the fauxcall skill to fake
   https://petstore3.swagger.io".

A few things are smoother in Claude Code: it asks the up-front questions as a form, and runs the
final independent review as a separate subagent. Other agents do the same steps in the
conversation.

## What you get

Each fake is a small, ordinary FastAPI project in `./fauxcall/<name>/`:

```text
your-project/
├── fauxcall/
│   ├── petstore/
│   │   ├── data/model.json          # everything the fake knows, in one readable file
│   │   ├── data/recordings/         # the real requests and responses it was built from
│   │   ├── data/world/              # the data it serves
│   │   ├── src/fauxcall_petstore/   # its routes, plus any custom code
│   │   ├── tests/                   # its tests
│   │   └── Dockerfile
│   ├── compose.yaml                 # runs all your fakes in containers
│   └── pyproject.toml               # one uv workspace for all of them
└── .github/workflows/fauxcall.yml   # CI that keeps the fakes tested
```

Run a fake's tests offline, or against the real service to see whether the real API has changed:

```bash
cd fauxcall/petstore
uv run pytest                                 # offline tests
uv run pytest tests/e2e --target=parity       # same tests against both; fails below 98%
```

The [`examples/`](examples/) folder has fakes FauxCall built by itself, from a single command each,
while we were testing it: Frankfurter, timeapi.io, Swagger Petstore, FakeRESTApi and this
repository's own reference API.

## Using fakes in tests

Every fake has a small control API under `/__fauxcall`:

| Call | What it does |
|---|---|
| `POST /__fauxcall/reset` | Put the data back the way it was recorded (for everyone, or for one test run) |
| `POST /__fauxcall/clock` | Freeze time, jump to a date, speed it up, or go back to real time. Moving the clock forward also lifts rate limits |
| `GET /__fauxcall/journal` | List every call your agent made, with requests and responses |
| `PUT /__fauxcall/faults` | Make calls fail: errors, timeouts, delays, dropped connections or `429`s |
| `PUT /__fauxcall/auth/users` | Choose which users are allowed, refused or expired |

To run tests in parallel, give each run its own name in an `X-Fauxcall-Session` header. It then gets
its own data and its own clock. Set `FAUXCALL_FAST=1` to skip the realistic delays and rate limits.

## Skills

| Skill | What it's for |
|---|---|
| `/fauxcall <url>` | Build a fake, or carry on with an interrupted build |
| `/fauxcall:refresh <name>` | Re-record a fake after the real API has changed |
| `/fauxcall:verify <name>` | Check a fake against the real service and fix what's off |
| `/fauxcall:extend <name>` | Add endpoints, test scenarios or behavior to a fake |
| `/fauxcall:serve` | Start fakes and hook them up to your agent |

## Privacy and safety

- **Your credentials stay private.** They live only in each fake's `.env` file, which you fill in
  and which git ignores. They're scrubbed from recordings, logs and the call journal, and never
  copied into Docker images.
- **Personal data is replaced with realistic stand-ins** as it's recorded, consistently, so a
  customer's name is replaced the same way in every response.
- **Nothing that changes data happens without your OK.** Before recording, you see the calls that
  would create, change or delete things, and approve them once. You also set how many such calls
  each endpoint may make (100 unless you choose otherwise; set `FAUXCALL_APPROVAL_BUDGET` to change
  the default).
- **It's polite to the real service.** Recording is slowed down to stay within rate limits, and
  capped.

## Jev (optional)

If you have a [TypeSafe](https://typesafe.ai) Jev key, a fake can use it to make judgment calls its
recordings don't cover, for example picking which recorded answer best fits a question it hasn't
seen before. Its answers are saved with the fake, so they replay later without the key. Without a
key, the fake uses simple, predictable rules instead.

## The `fauxcall` command

The skills drive the `fauxcall` command-line tool, and you can use it directly too:

```bash
uvx fauxcall status                 # how far each fake's build has got
uvx fauxcall serve petstore         # run one fake in the foreground
uvx fauxcall verify petstore --target parity
uvx fauxcall --help                 # everything else
```

## Questions

**Why does it install `httpx2` and `httpcore2` instead of `httpx` and `httpcore`?** They aren't
lookalike copies. They're maintained by Pydantic ([github.com/pydantic/httpx2](https://github.com/pydantic/httpx2)),
by the author of `httpx`. FauxCall needs `httpx2` for WebSockets and for testing fakes in-process;
`httpcore2` is the lower-level library `httpx2` is built on.

## Learn more

- [Examples](examples/): fakes FauxCall built by itself, ready to run
- [How it was tested](docs/validation.md): the method, the services we tried and the results
- [How it works](docs/architecture.md): the pipeline, the model and the runtime
- [Configuration](docs/configuration.md): every setting
- [Design decisions](docs/decisions.md): what we decided and why
- [Contributing](CONTRIBUTING.md) and [security](SECURITY.md)

## License

[Apache-2.0](LICENSE)
