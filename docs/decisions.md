# Design decisions

These were settled with the project owner before implementation (2026-10-10). The original brief is in
[original-brief.md](original-brief.md). When a decision changes, update this file in the same commit.

## Identity and distribution

| Topic | Decision |
|---|---|
| Name | **FauxCall** everywhere: PyPI package / CLI `fauxcall`, Claude Code plugin + marketplace `fauxcall`, GitHub `bradnemetski/fauxcall`. |
| Packaging | A Claude Code **plugin** whose marketplace is this repository itself (`.claude-plugin/marketplace.json`). Install: `claude plugin marketplace add bradnemetski/fauxcall`, then `claude plugin install fauxcall@fauxcall`. |
| Engine | The reusable engine is a **PyPI package** (`fauxcall`); the skills drive its CLI through `uvx`, so users only need `uv`. Generated fakes depend on the versioned package. |
| Skills | `/fauxcall <url>` (build a fake), plus `/fauxcall:refresh`, `/fauxcall:verify`, `/fauxcall:extend`, `/fauxcall:serve`. |
| Output location | Fakes are generated into `./fauxcall/<api>/` in the user's project — one uv workspace holding all fakes. Overridable. |
| Portability | Claude Code first, but every `SKILL.md` follows the open Agent Skills standard and all logic lives in the CLI, so other agents (Codex, Cursor, Gemini CLI, …) can run it. |
| License | Apache-2.0. |
| Python | `>=3.12` (CI on 3.12, 3.13, 3.14; macOS + Linux). 3.14 offered nothing this project needs. |
| Docs | Human-first README (complete but not overwhelming) + `docs/*.md`. |

## Inputs

- OpenAPI 3.x / Swagger 2.0 specs (JSON/YAML, URL or file), including Swagger UI / Redoc pages (the CLI finds the underlying spec).
- Free-form HTML API documentation (the agent authors an OpenAPI document from the docs + real responses).
- **Remote** MCP servers by URL (Streamable HTTP and legacy SSE). Local stdio MCP servers are out of scope.

## Building a fake

| Topic | Decision |
|---|---|
| Process | Sequential, one endpoint at a time, **checkpointed** so re-invoking resumes exactly where it stopped. Subagents only for independent review passes. |
| Check-ins | Key gates only: up-front plan (endpoints, credentials, Jev key, **one consolidated approval of all mutating calls**), genuinely ambiguous real-world behavior, final report. |
| Mutating calls | Approved **once, up front** as a single plan (with cleanup steps); autonomous afterwards. The CLI refuses unapproved mutating calls. |
| Secrets | Credentials live in a gitignored `.env` (pydantic-settings). Auth headers, tokens, cookies, keys are redacted from recordings. |
| PII | **Pseudonymized** deterministically (keyed HMAC, key in `.env`), format-preserving, consistent across endpoints so references survive. Names/addresses come from Faker, so Faker stays pinned in the lock file wherever recordings are made and parity tests run. |
| Storage | **JSON/JSONL only** — no SQLite. Recordings, model and world are diffable text committed to git; fakes load the world into memory. Ad-hoc SQL over recordings is available through a throwaway in-memory SQLite (`fauxcall sql`). |
| Jev | The build asks whether the user has a Jev (TypeSafe) key. It is used **only if they have one and a fake needs it**. Build-time judgment calls are made by the agent itself. |

## Fake behavior

| Topic | Decision |
|---|---|
| Server unit | **One FastAPI/uvicorn server per faked API/MCP**, identical paths/base path/signatures. |
| Unrecorded requests | Real semantics over **one consistent world**: unknown IDs → the real API's 404; function-like endpoints are computed; free-text inputs matched to the closest recording. Optional **build-time world expansion** (generated, invariant-checked, frozen) to match real list sizes/totals. No on-demand synthesis (it breaks order-independence and parity). |
| Fidelity | Error responses (probed, real bodies; FastAPI's own 422 replaced), latency (recorded distribution), rate limiting (real limits, 429 + headers). Contract headers (Content-Type, Location, pagination, Retry-After/rate-limit) are reproduced; incidental headers are not. |
| Payloads | Multipart uploads, binary downloads, SSE/chunked streaming, WebSockets. |
| Time | Real clock by default; per-field time policies (static, now, relative offset, calendar/market-session aware, …). |
| Volatile values | Frozen at the last recording (as specified), with an **optional recorded-series replay** mode. |
| Async jobs | Real state durations **measured while recording**, replayed deterministically from elapsed clock time (seeded per job). |
| State | Writes are **in memory** over an immutable seed; reset/snapshot/restore via admin API; persistence is opt-in. Per-API write semantics are learned (some APIs don't persist writes at all). |
| Auth | Mirrors the real scheme and error bodies. **Configurable allowed / denied users** (REST and MCP OAuth) so agents can be tested against auth failures. MCP OAuth uses a built-in, browserless authorization server. |
| Eval controls | `/__fauxcall` admin API: reset/health (always), clock control, call journal, fault injection, per-session isolation. |
| Realism defaults | Latency + rate limits **on** when serving; off in unit tests; `--fast` turns both off. |
| Runtime Jev uses | Matching unseen requests, semantic outputs (enum/bool/score fields for new input), outcome decisions. Cached + model-pinned; deterministic fallbacks without a key. |
| Hardcoded hosts | Not supported — agents point at the fake via base-URL override. |
| Extras | Dockerfile + compose, `fauxcall up` launcher, generated CI workflow for the fakes. |

## Quality bar

| Topic | Decision |
|---|---|
| Tooling | uv, `mypy --strict`, ruff with `select = ["ALL"]` minus a small curated ignore list. |
| Logging | structlog + stdlib bridge: JSON in prod, console in dev, request/session IDs via contextvars, secret redaction, uvicorn/httpx2 logs through the same pipeline. |
| Unit tests | ≥ 95% coverage (the `fauxcall` package and every generated fake). |
| E2E parity | Each e2e scenario runs against real and fake; **weighted parity checks** (status, schema/types, contract headers, stable values, cross-endpoint invariants, timing semantics); gate **≥ 98%**. Runs against the real API **on demand** (`--target=real|parity`); default runs are fake-only. |

## Validation

The tools/skills are validated by generating fakes with **headless Claude Code sessions** (`claude -p --plugin-dir`) that only know
what the skill tells them, against: a local reference API (exercises every feature), Frankfurter, timeapi.io, Swagger Petstore v3,
FakeRESTApi, and the DeepWiki MCP server. Gaps are fixed **in the skill/tooling, never by hand-editing a generated fake**.

## Made during implementation

| Topic | Decision |
|---|---|
| MCP wire protocol | FauxCall speaks JSON-RPC over Streamable HTTP and SSE itself instead of wrapping the MCP SDK's server, so session ids, content types, error objects and version negotiation can match the real server byte for byte. The official SDK client is used in tests to prove compatibility. |
| WebSocket replay | Defaults to `match`: send the recorded greeting on connect, then answer each client message with what the server sent after the same message (JSON compared by value). Binary frames and close codes/reasons are replayed as recorded. |
| Matching inputs | Repeated query parameters and array elements become lists, so nothing is lost when comparing a request with recordings. Path parameters keep their spec names (`pet-id`). |
| Saved models | `write_model` leaves out defaults for lean diffs, but always keeps the `kind` of field policies (the discriminator). |
| Workspace discovery | CLI commands work from anywhere in a project: the workspace is searched upward, never past the project root, and only folders that hold fakes are accepted. |
| Test suites | Network tests (`-m network`) and whole-generated-project tests (`-m slow`) are deselected by default and run as separate CI jobs; in CI a skipped generated-project gate is a failure. |
| Validation runs | Headless sessions run from a frozen git worktree of the tools in a clean environment (no inherited virtualenv), so fixes made meanwhile never leak into a run. |

