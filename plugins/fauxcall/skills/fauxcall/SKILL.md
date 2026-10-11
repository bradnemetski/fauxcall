---
name: fauxcall
description: Build a high-fidelity fake of a real HTTP API or remote MCP server for repeatable agent testing. Records the real service endpoint by endpoint, models time behavior and cross-endpoint consistency, generates a FastAPI fake with unit and end-to-end tests, and proves at least 98% parity with the real thing. Use when the user runs /fauxcall with an OpenAPI or Swagger URL or file, a Swagger UI or Redoc page, an API documentation page, or an MCP server URL, or asks to fake, mock, stub or emulate an API or MCP server for agent evaluations.
license: Apache-2.0
compatibility: Needs uv (https://docs.astral.sh/uv), Python 3.12+, a shell and network access to the real service. The Claude Code plugin puts a `fauxcall` command on PATH; elsewhere use `uvx fauxcall`.
metadata:
  author: Brad Nemetski
  homepage: https://github.com/bradnemetski/fauxcall
  version: "0.1.0"
allowed-tools: Bash(fauxcall *) Bash(uvx fauxcall *)
---

# FauxCall — build a fake

The input (a spec URL/file, docs page or MCP URL, optionally followed by `--name <kebab-name>`) is
the skill's argument. The goal is a fake an agent cannot tell from the real service: same paths or
tools, parameters, responses and errors; realistic timing; data consistent across endpoints; state
that changes the way the real one does. It must pass **≥ 98 % parity** against the real service.
This takes hours. **Be thorough, not fast.**

Use the `fauxcall` command for every step (`uvx fauxcall` if `fauxcall` is not on PATH).
`fauxcall <command> --help` documents every option. Reference guides (read them when the step says so):
[workflow](references/workflow.md) · [recording](references/recording.md) · [model](references/model.md) ·
[time](references/time.md) · [consistency](references/consistency.md) · [testing](references/testing.md) ·
[mcp](references/mcp.md) · [headless runs](references/headless.md).

## Ground rules — always

1. **No mutating call without the up-front approval.** POST/PUT/PATCH/DELETE (and MCP tools not
   annotated `readOnlyHint: true`) run only when `data/approvals.json` covers them. The CLI refuses
   otherwise — never work around it. Need more? Ask the user once, with the full list.
2. **Secrets never appear in chat, plans, code or commits.** Credentials live only in
   `fauxcall/<name>/.env`, which the *user* edits. Recordings are redacted and pseudonymized by the CLI.
3. **Evidence, not assumptions.** Every behavior in the model comes from recordings. When something
   cannot be recorded (e.g. a rare failure), use the documentation and say so in the model's `notes`.
4. **Checkpoint after every step** with `fauxcall progress …` — the run can be interrupted at any time
   and is resumed by invoking this skill again.
5. **One endpoint at a time** through the endpoint loop, in dependency order.
6. **Fix the model or the handlers, never a test's expectations** — unless the test is wrong.
7. **Consistency is non-negotiable:** every reference between endpoints must resolve in the fake
   (a ticker's exchange exists; an uploaded file appears in the upload status endpoint).
8. **Ask only at the up-front gate** and for genuinely ambiguous real-world behavior. Batch questions.
   In a non-interactive environment, read answers from `fauxcall/<name>/answers.json`
   ([headless](references/headless.md)) — never wait for input that cannot come.

## Phase 0 — start or resume

```bash
fauxcall doctor
fauxcall status                      # existing fakes and their next action
```

If a fake for this input exists, run `fauxcall status <name>` and continue at its next action
(skip to that phase). Otherwise create it:

```bash
fauxcall init "<input>" [--name <kebab-name>] [--base-url https://api.example.com] [--mcp]
```

Read the summary. Free-form docs: follow [workflow § docs](references/workflow.md#free-form-documentation).
MCP servers: also read [mcp](references/mcp.md).

## Phase 1 — the up-front gate (ask once, everything together)

Prepare first, then ask in a single message (AskUserQuestion if available):

1. **Credentials.** If the summary lists auth schemes, tell the user exactly which variables to put
   in `fauxcall/<name>/.env` (e.g. `FAUXCALL_REAL__HEADERS='{"X-API-Key": "…"}'`). Prefer a
   sandbox/test account. Verify with `fauxcall env check <name>` (prints no values).
2. **Jev key.** Ask whether they have a TypeSafe/Jev key. It is used only if they have one *and* the
   fake needs runtime judgments (free-text inputs, semantic outputs, outcome decisions); they add
   `TYPESAFE_API_KEY=…` to the same `.env`. Without one, fakes use deterministic fallbacks.
3. **Mutating calls.** Write the recording plans for **every** endpoint now
   ([recording](references/recording.md)) so the approval covers the whole build. Then
   `fauxcall plan review <name>` and show the table: which endpoints, how many calls, what is created
   and how it is cleaned up. On approval:
   `fauxcall plan approve <name> --planned --reason "<the user's words>"`.
4. **Scope.** For very large APIs, confirm whether to fake everything or a subset.

Then: `fauxcall progress gate <name> --credentials <none_needed|provided|missing> --jev|--no-jev --mutations <approved|declined|none_planned>`.
If writes were declined, model write endpoints from the documentation (lower fidelity — note it).

## Phase 2 — the endpoint loop

Order: independent collections' reads → dependent reads → writes → async jobs → streams/websockets
(`fauxcall endpoints <name>` lists them). For **each** endpoint E:

1. **Understand** — `fauxcall endpoints <name> --show "<E>"`, plus the docs for E.
2. **Plan** — refine `data/plans/<slug>.json`: samples, variations, edge cases, invalid probes,
   time profile, pagination walk, write + read-back, job polling, streams
   ([recording](references/recording.md) says exactly what each kind of endpoint needs).
   `fauxcall plan check <name> data/plans/<slug>.json`.
3. **Record** — `fauxcall record <name> data/plans/<slug>.json`. Read the report; re-plan on surprises.
4. **Analyze** — `fauxcall analyze endpoint <name> "<E>" --apply` (statuses, field profiles,
   timestamp behavior, documented-vs-observed schema, latency). Add `analyze errors`, `analyze writes`,
   `analyze jobs`, `fauxcall sql` as needed.
5. **Model** — edit `data/model.json` for E ([model](references/model.md), [time](references/time.md)):
   role, collection + key, parameter behavior (`on_invalid` from your probes), error cases → templates,
   pagination, write semantics, field policies, Jev semantics if needed. Then `fauxcall model check <name>`.
6. **Checkpoint** — `fauxcall progress endpoint <name> "<E>" recorded done` (likewise `analyzed`,
   `modeled`) and `fauxcall progress note <name> "<one-line finding>"`.

## Phase 3 — whole-API modeling

Follow [consistency](references/consistency.md) and [time](references/time.md):

- Collections with `extract` paths → `fauxcall world build <name>`; repeat until it reports the world
  is consistent (record the missing referenced entities; never invent them).
- `fauxcall analyze relations <name>` → confirm and add relations; `fauxcall world check <name>`.
- Calendars (market hours, holidays, publication times), job state machines, auth error templates,
  rate limits, routing quirks (probe case-sensitivity and trailing slashes), default headers.
- World expansion only when the real API's totals exceed what was recorded.

`fauxcall progress phase <name> model done` (and `world`).

## Phase 4 — generate and implement

```bash
fauxcall scaffold <name>
cd fauxcall/<name> && uv sync
```

Implement `computed`/`custom` endpoints (and MCP tools) in `src/<package>/handlers.py`
([model § custom handlers](references/model.md#custom-handlers)), with unit tests. Quality gates — all
must pass:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov
```

(≥ 95 % coverage of the fake's package.) Checkpoint `scaffold`, `implement`, `unit_tests`.

## Phase 5 — end-to-end parity

Write **many** scenarios in `tests/e2e/` ([testing](references/testing.md)): happy paths, parameter
variations, every error, cross-endpoint consistency, time behavior, pagination walks, async jobs,
auth failures, streams. Then:

```bash
fauxcall verify <name> --target parity [--allow-real-writes]
```

Read `data/reports/parity.md`. Iterate (fix model/handlers, re-run) until compliance ≥ 98 % and every
remaining failure is understood and explained. Checkpoint `e2e_tests`, `parity`.

## Phase 6 — review and report

- **Independent review:** have a fresh reviewer (Claude Code: the `fauxcall:reviewer` subagent; elsewhere
  a new session) check the model, recordings and parity report against the docs. Fix what it finds.
- `fauxcall report <name>` → `data/reports/summary.md`.
- Tell the user: what was faked, the parity score, known gaps and why, and how to use it
  (`fauxcall up`, base URL / MCP config, the `/__fauxcall` admin API). Checkpoint `review`, `report`.
