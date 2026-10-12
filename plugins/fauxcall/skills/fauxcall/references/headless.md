# Headless runs (no one to ask)

In CI, the Agent SDK or `claude -p`, nobody can answer the up-front gate. Put the answers in
`fauxcall/<name>/answers.json` — or pass a path after the input (`/fauxcall <url> --answers <file>`) —
and the skill reads them instead of asking. If a needed answer is missing, take the **safe** option
(no writes, no credentials) and record why in `fauxcall progress note`.

```json
{
  "name": "petstore",
  "credentials": "none_needed",
  "jev_key": false,
  "mutations": {
    "approve": true,
    "endpoints": ["POST /api/v3/pet", "PUT /api/v3/pet", "DELETE /api/v3/pet/{petId}"],
    "max_calls_per_endpoint": 40,
    "reason": "public demo sandbox; create and delete only our own fauxcall-probe pets"
  },
  "scope": "all",
  "notes": "Prefer fewer, richer recordings; the sandbox is shared."
}
```

Credentials (`"credentials": "provided"`) must be in `fauxcall/<name>/.env` **before** the run, written
by the user, e.g. `FAUXCALL_REAL__HEADERS='{"X-API-Key": "..."}'` (and `TYPESAFE_API_KEY=...` for
Jev). `fauxcall init` keeps that file and only adds the base URL and pseudonym key it needs.

| field | values |
|---|---|
| `credentials` | `none_needed`, `provided` (already in `.env`), `missing` (stop after the gate with a clear message) |
| `jev_key` | whether `TYPESAFE_API_KEY` is in `.env` |
| `mutations.approve` | `false` → model writes from the documentation only |
| `mutations.endpoints` | endpoint ids/globs the approval covers (`["*"]` only for sandboxes) |
| `mutations.max_calls_per_endpoint` | real calls each approved endpoint may make (recording and parity together); omitted: 100, the `approval_budget` setting |
| `scope` | `all` or a list of endpoint ids/globs |

Apply it with the normal commands, then `fauxcall progress gate …`:

```bash
fauxcall plan approve <name> --endpoint "<id or glob>" --per-endpoint --max-calls N --reason "<reason>"
```

`--per-endpoint` gives every matching mutating endpoint its own budget of N, as
`max_calls_per_endpoint` means; without it a glob shares one budget. (Interactively, `--planned`
approves the exact writes a person reviewed instead; in a headless run the answers file is the
approval, by endpoint.) Without `--max-calls` an
approval allows 100 calls (the `approval_budget` setting); `--unlimited` only if the answers say so. Endpoint budgets are spent by recordings and by parity
runs with `--allow-real-writes` alike, so plan for both. Approving an endpoint again adds N calls on
top of what it already allowed (the command says how many in all).
