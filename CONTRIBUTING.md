# Contributing to FauxCall

Thanks for helping. Bug reports, fixes, new input types and better skills are all welcome.

## Set up

You need [uv](https://docs.astral.sh/uv/). It installs the right Python.

```bash
git clone https://github.com/bradnemetski/fauxcall
cd fauxcall
uv sync
```

## Checks

Every change must pass the same gates CI runs:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest --cov            # fails below 95% coverage
```

Two kinds of tests are deselected by default:

```bash
uv run pytest -m slow          # builds whole fakes and runs their own gates (needs uv)
uv run pytest -m network       # talks to real public services (Petstore, Frankfurter, DeepWiki …)
```

The project uses `ruff` with every rule set enabled and `mypy --strict`. Silence a rule only with a
specific `noqa` and a short reason.

## Working on the skills

The plugin lives in `plugins/fauxcall/`. To try local changes in Claude Code, without installing
the plugin, point the plugin's `fauxcall` wrapper at your checkout:

```bash
FAUXCALL_SOURCE=$PWD claude --plugin-dir ./plugins/fauxcall
```

Then run `/fauxcall <url>` in a scratch directory. Validate the manifests and skills before sending
a change:

```bash
claude plugin validate --strict .
claude plugin validate --strict ./plugins/fauxcall
```

Skills must stay within the [Agent Skills](https://agentskills.io) specification (`name`,
`description`, `license`, `compatibility`, `metadata` and `allowed-tools` frontmatter only), so
agents other than Claude Code can use them.

## A local "real" API

`reference_api/` is a small but realistic market-data API with known ground truth
(`reference_api/GROUND_TRUTH.md`). It has trading hours, async uploads, cursor pagination, rate
limits, SSE, WebSockets and an MCP endpoint. Use it to try the whole pipeline without touching a
public service:

```bash
uv run fauxcall-reference-api --port 9100
```

## Where things live

| Path | What |
|---|---|
| `src/fauxcall/spec/` | Finding and reading OpenAPI specs, docs pages and MCP servers |
| `src/fauxcall/recording/`, `mcpwire/` | Recording real exchanges, the MCP wire protocol |
| `src/fauxcall/privacy/` | Redaction and pseudonymization |
| `src/fauxcall/analysis/` | Turning recordings into evidence |
| `src/fauxcall/model/` | The model of a fake (`data/model.json`) |
| `src/fauxcall/world/`, `clock.py`, `calendar.py` | The dataset, the clock and trading calendars |
| `src/fauxcall/runtime/` | The engine every fake runs on |
| `src/fauxcall/build/` | Generating fake projects (templates included) |
| `src/fauxcall/testing/` | The parity-testing pytest plugin |
| `src/fauxcall/cli/` | The `fauxcall` command |
| `plugins/fauxcall/` | The Claude Code plugin and its skills |

[docs/architecture.md](docs/architecture.md) explains how the pieces fit, and
[docs/decisions.md](docs/decisions.md) records why things are the way they are.

## Pull requests

- Keep changes focused, and add tests with the change. Bug fixes need a test that failed before.
- Update the docs and the skills when behavior changes. The skills are documentation for agents,
  and they drift just like docs for people.
- Add a line to `CHANGELOG.md` under "Unreleased".

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
