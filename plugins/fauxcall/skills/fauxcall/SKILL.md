---
name: fauxcall
description: Build a high-fidelity fake of a real HTTP API or remote MCP server for repeatable agent testing. Records the real service endpoint by endpoint, models time behavior and cross-endpoint consistency, generates a FastAPI fake with unit and end-to-end tests, and proves at least 98% parity with the real thing. Use when the user runs /fauxcall with an OpenAPI or Swagger URL or file, a Swagger UI or Redoc page, an API documentation page, or an MCP server URL, or asks to fake, mock, stub or emulate an API or MCP server for agent evaluations.
license: Apache-2.0
compatibility: Needs uv (https://docs.astral.sh/uv), Python 3.12+, a shell and network access to the real service. The Claude Code plugin puts a `fauxcall` command on PATH; elsewhere install it with `uv tool install fauxcall` (or `uv tool install git+https://github.com/bradnemetski/fauxcall`). Its dependencies httpx2 and httpcore2 are genuine (Pydantic's, github.com/pydantic/httpx2), needed for WebSockets, not typosquats of httpx.
metadata:
  author: Brad Nemetski
  homepage: https://github.com/bradnemetski/fauxcall
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
   annotated `readOnlyHint: true`) run only when `data/approvals.json` covers them, in recordings and
   in parity runs alike, each spending the same budgets. The CLI and the test harness refuse
   otherwise — never work around it. Need more? Ask the user once, with the full list. The method
   decides, not the outcome you expect: a POST you expect to be refused is still a write.
2. **The real API is called only through FauxCall:** `fauxcall record` (plans) and the e2e suite's
   `--target real|parity` runs. Never curl, scripts or a subagent: they skip the pacing, the call
   budget, redaction and the approval check, and what they show is not evidence. (Fetching public
   documentation pages is fine.)
3. **Secrets never appear in chat, plans, code or commits.** Credentials live only in
   `fauxcall/<name>/.env`, which the *user* edits. Recordings are redacted and pseudonymized by the CLI.
4. **Evidence, not assumptions.** Every behavior in the model comes from recordings. When something
   cannot be recorded (e.g. a rare failure), use the documentation and say so in the model's `notes`.
5. **Checkpoint after every step** with `fauxcall progress …` — the run can be interrupted at any time
   and is resumed by invoking this skill again.
6. **One endpoint at a time** through the endpoint loop, in dependency order.
7. **Fix the model or the handlers, never a test's expectations** — unless the test is wrong.
8. **Consistency is non-negotiable:** every reference between endpoints must resolve in the fake
   (a ticker's exchange exists; an uploaded file appears in the upload status endpoint).
9. **Ask only at the up-front gate** and for genuinely ambiguous real-world behavior. Batch questions.
   In a non-interactive environment, read answers from `fauxcall/<name>/answers.json`
   ([headless](references/headless.md)) — never wait for input that cannot come.
10. **Never park the build on a question after the gate.** The user is usually away by then; one
   question can stall hours of work. What the gate approved stands: don't ask again for writes it
   already covers. If more writes or probes would help, note them (`fauxcall progress note`), carry on
   with what you have, and ask once, in the final report. Stop only when nothing else can proceed.

## Phase 0 — start or resume

```bash
fauxcall doctor --skill-dir "<the folder this SKILL.md is in>"
fauxcall status                      # existing fakes and their next action
```

If doctor says the skill doesn't match the CLI, **stop** and give the user the fix it prints: these
instructions were written for a different release of the tool, and following them with it goes wrong
in ways that are hard to see.

If a fake for this input exists, run `fauxcall status <name>` and continue at its next action
(skip to that phase). Otherwise create it, always with `--name`:

```bash
fauxcall init "<input>" --name <kebab-name> [--base-url https://api.example.com] [--mcp]
```

If the service needs credentials just to connect (an MCP server behind a key or token, like
`mcp.stripe.com`), ask for them **before** init: the user puts them in `fauxcall/<name>/.env` as
`FAUXCALL_REAL__HEADERS='{"Authorization": "Bearer …"}'`, and `init --name <name>` reads them from
there. The same file serves recording and tests later, so it is the only place a key goes. Never
read, print, source or move that file yourself: if init fails on auth, it creates the file (with
commented examples) when missing and prints its path; give the user that path and its advice.
`fauxcall env check <name>` shows which headers are set, and their form, without their values.

Read the summary. Free-form docs: follow [workflow § docs](references/workflow.md#free-form-documentation).
MCP servers: also read [mcp](references/mcp.md).

Then read the documentation's **prose** too (the spec's `info.description`, tag and operation
descriptions, linked guides): endpoints a spec can't express (WebSocket feeds, streams described in
text) are only mentioned there, and they are in scope like any other. Describe each in a small
OpenAPI file (`data/source/<topic>.openapi.json`; a WebSocket is a `get` on its path, its protocol
in the `description`) and add it with `fauxcall spec import <name> data/source/<topic>.openapi.json`
(existing endpoints are kept) — never by editing `data/model.json` by hand at this stage.

## Phase 1 — the up-front gate (ask once, everything together)

Prepare first, then ask in a single message (AskUserQuestion if available):

1. **Credentials.** If the summary lists auth schemes, tell the user exactly which variables to put
   in `fauxcall/<name>/.env` (e.g. `FAUXCALL_REAL__HEADERS='{"X-API-Key": "…"}'`). Prefer a
   sandbox/test account. Verify with `fauxcall env check <name>` (prints no values).
   **What the credentials may do:** once they are in place, record one cheap read of each endpoint
   or tool (a read-only plan: no approval needed), then `fauxcall analyze access <name>`. If it
   lists refusals, put them in the question: "your key can't use X, Y: the fake will refuse those
   too. Keep it, or use a test key with the permissions your agents need?" A restricted key makes
   a restricted fake. (MCP tools not annotated `readOnlyHint` need the approval even to read: check
   those right after it, and if most are refused, ask before going on. A key that can't do the job
   is worth stopping for.)
2. **Jev key.** Ask whether they have a TypeSafe/Jev key. It is used only if they have one *and* the
   fake needs runtime judgments (free-text inputs, semantic outputs, outcome decisions); they add
   `TYPESAFE_API_KEY=…` to the same `.env`. Without one, fakes use deterministic fallbacks.
3. **Mutating calls.** Write the recording plans for **every** endpoint now
   ([recording](references/recording.md)), every write you will need in the whole build: the
   approval covers exactly these calls. Then `fauxcall plan review <name>` and show the table: each
   write, how often, what cleans it up, and what **stays on the real service** (no cleanup, or a
   cleanup that failed). Also show the calls the plans declare read-only (`mutating: false`) on
   tools or methods that could write. Ask for the approval **and the parity budget**: how many
   writes parity runs may make per endpoint, offering the default the review prints (100 unless
   the user set `FAUXCALL_APPROVAL_BUDGET`). On approval:
   `fauxcall plan approve <name> --planned --max-calls <their number> --reason "<the user's words>"`.
   Recording may then run exactly the reviewed writes: one you add or change later shows as
   **new**, is refused, and goes on the questions for the end of the build (rule 10). To run an
   approved write again, the user approves it again (`--call <id>`).
4. **Scope.** For very large APIs, confirm whether to fake everything or a subset.

Then: `fauxcall progress gate <name> --credentials <none_needed|provided|missing> --jev|--no-jev --mutations <approved|declined|none_planned>`.
If writes were declined, model write endpoints from the documentation (lower fidelity — note it).

## Phase 2 — the endpoint loop

Order: independent collections' reads → dependent reads → writes → async jobs → streams/websockets
(`fauxcall endpoints <name>` lists them). For **each** endpoint E:

1. **Understand** — `fauxcall endpoints <name> --show "<E>"`, plus the docs for E. Start a plan
   entry with `fauxcall plan template <name> "<E>"` (REST endpoints and MCP tools alike).
2. **Plan** — refine `data/plans/<slug>.json`: samples, variations, edge cases, invalid probes,
   time profile, pagination walk, write + read-back, job polling, streams
   ([recording](references/recording.md) says exactly what each kind of endpoint needs).
   `fauxcall plan check <name> data/plans/<slug>.json`.
3. **Record** — `fauxcall record <name> data/plans/<slug>.json`. Read the report; re-plan on surprises.
   Browse what was recorded with `fauxcall recordings <name> --endpoint "<E>"` (one line per
   exchange, MCP calls with their arguments and answer; `--status`, `--purpose`, `--grep` filter,
   `--show <id>` prints one in full, `--jsonl` gives them all for a script). Never write your own
   script over `data/recordings/`.
4. **Analyze** — `fauxcall analyze endpoint <name> "<E>" --apply` (statuses, field profiles,
   timestamp behavior, documented-vs-observed schema, latency; for an MCP tool also how it refuses
   arguments that break its schema and whether it answers the same arguments differently, which
   marks it `varies`). Add `analyze errors`, `analyze writes`, `analyze jobs`, `fauxcall sql` as
   needed.
5. **Model** — edit `data/model.json` for E ([model](references/model.md), [time](references/time.md);
   MCP tools: `fauxcall model tool`, see [mcp](references/mcp.md#what-the-runtime-serves)):
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

(If uv warns that `VIRTUAL_ENV` doesn't match the project's environment, the user's own project
environment is active: it is harmless, since uv uses the fake's `.venv`. `unset VIRTUAL_ENV` in
that shell silences it.)

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
remaining failure is understood and explained. Then look at what the score rests on
([testing § reading the report](references/testing.md#reading-the-report)): the **independent**
score (without calls that only repeat recordings or compare shape), what no scenario covers, and
whether a refused credential is checked. Close those gaps with new scenarios. Checkpoint
`e2e_tests`, `parity`.

## Phase 6 — review and report

- **Independent review:** have a fresh reviewer (Claude Code: the `fauxcall:reviewer` subagent; elsewhere
  a new session) check the model, recordings and parity report against the docs. It works offline
  (never ask it to call the real API): when it needs evidence it proposes probes as plan entries,
  and you record them with `fauxcall record` (approval rules apply). Fix what it finds.
  Checkpoint `fauxcall progress phase <name> review in_progress` first, then run the reviewer in the
  **foreground** and wait for its report: never end your turn while it runs, since a review left in
  the background is lost when the session ends. `fauxcall status` flags a review that never finished.
- Write the known limitations (what differs from the real API, and why) into the fake's `README.md`
  between its `fauxcall:notes` markers: the only part of the README `fauxcall scaffold` keeps.
- `fauxcall report <name>` → `data/reports/summary.md`.
- Tell the user: what was faked, the parity score and the independent score, known gaps and why,
  and how to use it
  (`fauxcall up`, base URL / MCP config, the `/__fauxcall` admin API). Checkpoint `review`, `report`.
