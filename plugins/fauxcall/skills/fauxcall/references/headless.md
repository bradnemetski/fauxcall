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

The write approval needs no command: the answers file *is* the user's approval, and `fauxcall
record`, `plan review`, `plan check` and parity runs apply it themselves (each matching tool or
write endpoint gets its own budget of `max_calls_per_endpoint`, for recording and parity
together; omitted: 100, the `approval_budget` setting). Never run `fauxcall plan approve` in a
headless run, and **never create or edit `answers.json`**: only the user writes it. Then record the
gate: `fauxcall progress gate …`.
