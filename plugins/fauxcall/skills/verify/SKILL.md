---
name: verify
description: Check a FauxCall fake's quality — lint, types, unit tests with coverage, and the end-to-end parity suite against the real API — and produce a compliance report with explained failures. Use when the user runs /fauxcall:verify <name>, or asks how faithful a fake is or whether it still matches the real API.
license: Apache-2.0
compatibility: Needs uv and a shell; parity against the real API needs network access and the fake's .env credentials.
metadata:
  author: Brad Nemetski
allowed-tools: Bash(fauxcall *) Bash(uvx fauxcall *)
---

# Verify a fake

The argument is the fake's name, optionally followed by `--offline` (skip the real API).

0. `fauxcall doctor --skill-dir "<the folder this SKILL.md is in>"`: if it says the skill doesn't
   match the CLI, stop and give the user the fix it prints.
1. In `fauxcall/<name>`: `uv sync`, then `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy` — report any failure precisely.
2. `fauxcall verify <name>` — unit + end-to-end on the fake, with coverage (must be ≥ 95 %).
3. Unless offline: `fauxcall verify <name> --target parity` (add `--allow-real-writes` only if the
   user approved writes for this fake — see `fauxcall plan review <name>`).
4. Read `data/reports/parity.md`. For each failed check, determine the cause (see the main skill's
   testing guide, "Reading the report") and classify it: fake bug, stale data (upstream drift), test
   bug, or expected difference (explain why).
5. `fauxcall report <name>`. Tell the user: lint/type/coverage status, parity score vs the 98 % gate
   and the independent score (calls that don't just repeat recordings), what no scenario covers,
   and the classified failures with suggested fixes (fixes themselves: `/fauxcall:refresh` for drift,
   or edit model/handlers).
