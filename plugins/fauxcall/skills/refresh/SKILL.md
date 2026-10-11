---
name: refresh
description: Re-record an existing FauxCall fake against its real API, detect upstream drift (new or changed fields, statuses, errors, timing), update the model, world and tests, and re-run parity. Use when the user runs /fauxcall:refresh <name>, or says a fake is stale, the real API changed, or parity dropped.
license: Apache-2.0
compatibility: Needs uv, a shell and network access to the real service; the fake's .env must hold any credentials.
metadata:
  author: Brad Nemetski
  version: "0.1.0"
allowed-tools: Bash(fauxcall *) Bash(uvx fauxcall *)
---

# Refresh a fake

The argument is the fake's name (see `fauxcall status`). Follow the main `/fauxcall` skill's ground
rules (approvals, secrets, evidence) and its reference guides.

1. `fauxcall status <name>` and `fauxcall report <name>` — know what exists and the last parity score.
2. **Drift check first (read-only):** `fauxcall verify <name> --target parity` (no writes). The report
   lists every place the real API now differs.
3. Re-record what drifted: re-run the affected plans in `data/plans/` (read-only ones freely; writes
   only within the existing approval — `fauxcall plan review <name>` shows coverage; ask the user once
   for anything new). Also re-run the spec discovery if the API publishes one:
   `fauxcall endpoints <name>` vs the current spec (new endpoints → `/fauxcall:extend`).
4. `fauxcall analyze endpoint <name> "<E>" --apply` for each drifted endpoint; update policies, error
   templates, relations; `fauxcall world build <name>`; `fauxcall model check <name>`.
5. `fauxcall scaffold <name>` (regenerates routes/README only), update handlers and tests.
6. `fauxcall verify <name>` then `fauxcall verify <name> --target parity` until ≥ 98 %.
7. `fauxcall report <name>` and summarize for the user: what drifted, what changed in the fake, score.
