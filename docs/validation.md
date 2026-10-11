# How FauxCall was validated

FauxCall's tools and skills were validated by using them as a user would, with nobody helping.

## Method

For each target, a headless Claude Code session (`claude -p`) was started with the plugin
(`--plugin-dir plugins/fauxcall`) and one prompt:

```text
/fauxcall <spec, docs page or MCP URL> --name <name> --answers answers.json
```

The session knew only what the skill told it. The answers file held the decisions a user makes once,
up front: credentials, whether a Jev key exists, and which writes to the real service are allowed
(see [the headless reference](../plugins/fauxcall/skills/fauxcall/references/headless.md)).

- **Isolation:** each run used a frozen snapshot of the tools (a git worktree at a commit that
  passed every gate), in its own project folder, with a clean environment (no inherited
  virtualenv). Fixes made during a run reached it only as later, gate-passing commits.
- **Permissions:** runs used Claude Code's `auto` permission mode, with no human to approve
  prompts.
- **Fix the tools, never the fake:** whenever a run struggled, the fix went into the skills or the
  tooling. Generated fakes were never edited by hand.
- **Jev:** no Jev key was configured, so every run used the deterministic fallbacks.

## Targets

| Target | Input | Why it was chosen |
|---|---|---|
| Reference API (`reference_api/`) | Swagger UI page | Every feature, against known ground truth: trading hours, async uploads, cursors, rate limits, SSE, WebSockets |
| [Frankfurter](https://frankfurter.dev) | OpenAPI document | Daily published data (ECB rates), date ranges, conversions |
| [timeapi.io](https://timeapi.io) | Swagger UI page | "Now" in any time zone, conversions, date arithmetic |
| [Swagger Petstore v3](https://petstore3.swagger.io) | Swagger UI page | A shared sandbox with real writes, uploads, and auth |
| [FakeRESTApi](https://fakerestapi.azurewebsites.net) | Swagger UI page | Writes that are echoed but never persisted |
| [DeepWiki](https://mcp.deepwiki.com/mcp) | MCP server | A public MCP server: tools answered from the closest recording |

## Results

Results are added here as runs complete.

## What the runs changed

Every problem a run hit became a fix in the tools or skills. They are listed in the git history
with the message "found by the … validation run", and summarized here.
