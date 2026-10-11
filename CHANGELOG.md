# Changelog

All notable changes to FauxCall are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and FauxCall uses
[semantic versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/bradnemetski/fauxcall/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/bradnemetski/fauxcall/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bradnemetski/fauxcall/releases/tag/v0.1.0
