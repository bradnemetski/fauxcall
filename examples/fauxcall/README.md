# Fakes

High-fidelity fakes of the APIs and MCP servers this project's agents use, generated with
[FauxCall](https://github.com/bradnemetski/fauxcall). Each folder is one fake; they share this uv
workspace.

| fake | imitates | port |
|---|---|---|
| [`frankfurter`](frankfurter/README.md) | Frankfurter API | 8701 |
| [`refapi`](refapi/README.md) | FauxCall Reference Market API | 8702 |
| [`timeapi`](timeapi/README.md) | Time API Docs | 8703 |

## Run them

```bash
uvx fauxcall up            # start every fake, print base URLs (and MCP config)
uvx fauxcall up frankfurter   # just one
docker compose up          # or in containers
```

Point your agent at the printed base URL instead of the real API. Each fake has an admin API under
`/__fauxcall` (reset, clock control, call journal, fault injection): see its README.

## Keep them honest

```bash
cd <fake> && uv run pytest                             # unit + offline e2e (fast, no network)
cd <fake> && uv run pytest tests/e2e --target=parity   # compare against the real API
```
