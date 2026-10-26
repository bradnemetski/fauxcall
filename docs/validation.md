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

- **Isolation:** each run used a frozen copy of the tools: a plain export of a commit that passed
  every gate, without git history and without the reference API's source and answer key, so there
  was nothing to read the "real" answers from. Each run had its own project folder and a clean
  environment (no inherited virtualenv). Fixes made during a run reached it only as later,
  gate-passing exports, swapped in file by file.
- **Permissions:** runs used Claude Code's `auto` permission mode, with no human to approve
  prompts.
- **Fix the tools, never the fake:** whenever a run struggled, the fix went into the skills or the
  tooling. Generated fakes were never edited by hand.
- **Jev:** the user added a Jev key for DeepWiki, the one target whose fake must judge free text;
  every other run used the deterministic fallbacks.

## Targets

| Target | Input | Why it was chosen |
|---|---|---|
| Reference API (`reference_api/`) | Swagger UI page | Every feature, against known ground truth: trading hours, async uploads, cursors, rate limits, SSE, WebSockets |
| [Frankfurter](https://frankfurter.dev) | OpenAPI document | Daily published data (ECB rates), date ranges, conversions |
| [timeapi.io](https://timeapi.io) | Swagger UI page | "Now" in any time zone, conversions, date arithmetic |
| [Swagger Petstore v3](https://petstore3.swagger.io) | Swagger UI page | A shared sandbox with real writes, uploads, and auth |
| [FakeRESTApi](https://fakerestapi.azurewebsites.net) | Swagger UI page | Writes that are echoed but never persisted |
| [DeepWiki](https://mcp.deepwiki.com/mcp) | MCP server | A public MCP server: tools answered from the closest recording |
| [Everything](https://github.com/modelcontextprotocol/servers/tree/main/src/everything) (2026.8.31, run locally) | MCP server | The MCP project's reference server, which exercises the whole protocol: resources and templates, prompts, completions, subscriptions, logging, progress, tasks, tools offered only to some clients |

## Results

| Target | Parity | Scenarios | Endpoints | Recordings | Offline tests | Sessions | Reported cost |
|---|---|---|---|---|---|---|---|
| Reference API | **100%** | 65, real writes included (independent: 100% over 168 of 230 calls) | 21 | 859 | 716, 98% coverage | 2 | $41.73 |
| Frankfurter | **100%** | 142 | 9 | 219 | 427, 99% coverage | 2 | $14.06 |
| timeapi.io | **100%** | 192 | 28 | 576 | 1,366, 98% coverage | 1 | $19.72 |
| Swagger Petstore | **99.71%** | 96 (98.48% over 80 with real writes) | 19 | 370 | 361, 99% coverage | 1 | $15.21 |
| FakeRESTApi | **100%** | 115 | 27 | 323 | 671, 99% coverage | 1 | $13.02 |
| DeepWiki (MCP) | **100%** | 20 | 3 tools | 70 | 175, 98% coverage | 3 | over $8 |
| Everything (MCP) | **100%** | 228 (independent: 100% over 243 of 352 calls) | 19 tools, 7 resources, 2 templates, 4 prompts | 538 | 962, 97% coverage | 1 | $64.53 |

*Parity* is the weighted share of checks (status, shape, content type, values, lengths, headers) on which the fake answered
like the real service, over end-to-end scenarios run against both. *Reported cost* is what Claude
Code reported for the sessions. The finished fakes are in [`examples/`](../examples/), except
DeepWiki's (see below).

**Reference API.** The hardest target: trading sessions per exchange, async uploads, cursor
pagination, a fixed-window rate limit, an SSE stream and a WebSocket feed. The first session was cut
off by a usage limit and resumed. The build missed the WebSocket feed, which only the API's
description mentions; the independent review caught it, the session recorded 36 conversations and
added it, and the skill now reads the docs' prose for such endpoints. That build recorded on a
Sunday, so open-market quotes followed the documentation. The fake in `examples/` is a later
rebuild that recorded on a weekday night, so it has the opposite gap: weekend and holiday closures
follow the documentation, unverified live.

**Frankfurter.** Daily ECB rates published at 16:00 Central European time, with TARGET holidays.
The fake computes "latest" and date ranges on its own clock. Its main limit: rates stop moving after
the last recorded fixing (dates keep advancing; the rates repeat).

**timeapi.io.** The Swagger page documents two API versions; the write endpoints exist only in the
older one, so the fake covers both. It pins the time-zone database to the release the real server
uses, reproduces negative daylight-saving time (Ireland, Morocco) and calendar-aware translations,
and answers IP lookups only for the 55 recorded addresses (others fall back to a nearby recorded one
or UTC). One run with every write endpoint scored 99.96%; its one failure (Korean AM/PM wording) was
fixed, and the final read-only run scored 100%.

**Swagger Petstore.** A shared public sandbox that other people write to while you record. The fake
reproduces the sandbox as it really behaves, faults included: only the seeded demo users answer
(any other user, and every user write, gets a 500), tag search always fails, and the inventory counts
orders rather than pets. An unrecorded "healthy" mode, documented from the server's open-source
code, is available as a switch. The run created, changed and deleted only its own probe records
(using ids it first checked were free) and confirmed they were all gone at the end. Its remaining
differences: the sandbox's listing changed during the run, login returns a random session id, and
OPTIONS names the host it was called on. The run with real writes came before the review's fixes and
wasn't repeated, because the write budget was nearly spent.

**FakeRESTApi.** A .NET demo API whose writes are echoed but never stored, and whose authors are
random on every request. The fake generates its data from the same rules (book N has N×100 pages),
binds request bodies the way ASP.NET Core does (case-insensitive names, numbers in strings, the
exact error messages with byte positions, a maximum depth of 32) and parses ids like .NET
(`+5`, `0x10`). Its run with writes scored 99.98%; the one difference (how a malformed `true` is
quoted in an error message) was fixed and re-checked.

**DeepWiki.** A public MCP server answering questions about GitHub repositories' documentation
through three tools. The fake serves the recorded wikis of five repositories (others answer
"Repository not found") and answers questions it never saw from them: a recorded answer when Jev
judges that one fits, otherwise excerpts from the best-matching wiki page with the real links. It
answers refusals in about 250 ms and questions in 10 to 17 s, as the real server does. Its parity
scenarios compare wiki structure and contents by value and generated answers by shape, checked by
behavior (about the repository asked about, with links that resolve). The parity run came before
the review's fixes and couldn't be repeated: the approved budget was spent. **The build made 33 real
`ask_wiki_question` calls against an approval of 30**: a session stopped mid-recording (to restart it
with the user's Jev key) hadn't saved the count of 6 calls, so the next one spent the full budget on
top of them. They were read-only calls to a public service; the bug is fixed (`af370c3`).

The DeepWiki fake is not in `examples/`. Its recordings are the wiki pages and answers DeepWiki
generated, and nothing grants a right to republish them: Cognition, which runs DeepWiki, publishes
no license for that content, and its [website terms](https://cognition.com/legal/website-terms-of-use)
(section 3.1) and [acceptable use policy](https://cognition.com/acceptable-use-policy) forbid
copying or republishing it. The results above describe how the fake performed without reproducing
any of it.

## Integrity

After the runs, every session transcript was searched for reads of the reference API's source, its
answer key (`GROUND_TRUTH.md`) or the FauxCall repository. None read them. One reviewer went looking
for the answer key, which the reference API's docs mentioned by name; there was nothing to find,
and the docs no longer mention it. Before exports were introduced, the first reference API session
read FauxCall's own code (not the reference API) while investigating a CLI bug.

## What the runs changed

Every problem a run hit became a fix in the tools or skills, never in the fake. The main ones:

- **Parity comparison.** Ids created during a scenario are compared through their counterparts on
  the other side; polling is compared by its final answer; streams by their structure; real calls
  are paced to the recorded rate limit (`0747190`, `0290142`, `c70072d`).
- **Real calls go only through FauxCall.** Sessions had called real APIs with curl and scripts,
  which skips pacing, budgets, redaction and approvals. The skill now forbids it (the reviewer works
  offline and proposes probes), and the recorder gained what those calls were used for: timed and
  binary WebSocket frames (`b4495df`, `b7c3e28`).
- **Missed endpoints.** Endpoints only the docs' prose mentions are imported with `fauxcall spec
  import` (`b4495df`); routing probes of paths the API lacks pass `plan check` (`ea5e5ba`).
- **Model and data.** Map-shaped responses (a date per key) are inferred as one value schema, which
  shrank Frankfurter's model from 60 MB to 95 KB (`52436e4`); policy kinds, endpoint roles and
  parameter descriptions survive saving (`1740991`, `80cb62d`).
- **Runtime.** Authorization by scope after rate limiting (`4853215`); advancing the fake's clock
  lifts rate limits (`b338c44`); a served fake admits exactly the real API's users while its tests
  let themselves in (`5e559fd`).
- **Command line.** The workspace is found from anywhere in a project (`be550b4`); `verify` works
  from the project root and never reprints an old report (`29cc733`); scaffold writes CI where the
  project is (`97747ee`), keeps the known limitations in a fake's README (`8982487`) and gives fakes
  brought together their own ports (`450871b`).
- **Write budgets.** Parity runs with real writes didn't spend the user's approved budget at all;
  FakeRESTApi counted its writes by hand. They now spend it exactly as recording does, and stop when
  it runs out (`3f11da1`). Recorders save each spend before the call goes out, so a stopped run
  can't lose its count again (`af370c3`), and `plan check` applies the recorder's rule to MCP calls
  (`c9be916`).
- **Timing.** Errors keep their own recorded times (DeepWiki refuses in 250 ms and answers in 15 s),
  and `analyze endpoint --apply` now times MCP tools, including answers sent as SSE (`7468b1b`,
  `6c049e0`). A modeled default protocol version wins over the recorded handshake's (`944bc73`).
- **Privacy and images.** `public_values` keeps a sandbox's public demo users as they are while
  everyone else's data in the same fields is still pseudonymized; Petstore had to switch
  pseudonymization off for every user field instead (`63bc5a2`). The same option keeps a demo's
  generated passwords without switching off password redaction (`c586f90`). Every Docker image used to receive
  every fake's `.env`; a generated `.dockerignore` now keeps credentials out (`db6570c`).
- **MCP beyond tools.** The Everything server's first build reached 95.59%: every failure was a
  gap in FauxCall's MCP runtime. Prompts and completions answered with the first recording
  whatever was asked; resources, prompts and completions couldn't be computed; notifications,
  subscriptions and tools offered only to some clients weren't supported; a crashing handler gave
  a bare HTTP 500. The runtime gained handlers for any method, streamed progress and log
  notifications, capability-gated tools and `-32603` for crashes, and the second build reached
  99.91%. Its notes named what was still missing (extensible listings, the client's capabilities
  in handlers, pushes on the GET stream, the tools for `elicitation.url` and `tasks` clients, the
  real refusals of malformed calls); with those added, the third build reached 100%. Headless approvals were refused by the agent's own permission checks, so the CLI now
  applies `answers.json` itself; parity reports now pseudonymize the real side's answers.
- **A fake never calls out.** The fourth build also scored 100%, but its `gzip-file-as-resource`
  fetched URLs live (GitHub, and the real server itself): its tests passed only while the real
  server ran. Offline test runs now refuse any connection off the machine or to the real service
  and fail the test that tried. That build's review also found the last protocol gaps (a GET or
  DELETE for an unknown session, a second GET stream, the real keep-alive, reacting to the
  client's notifications), now learned and repeated.
- **Clients that answer back.** The sixth build passed at 100% but had to skip 23 scenarios
  in-process and reach into FauxCall's internals: the in-process test transport buffered answers,
  so a scenario couldn't answer the server mid-call. Its notes also showed that `fauxcall up`
  served only the model (no `handlers.py`), that recordings couldn't see the client's GET stream
  (the server asks for roots there), and that the fake dropped what a reconnecting client had
  missed. Scenarios now open sessions with capabilities and answers (`mcp.open`), the in-process
  fake streams, `up` runs the fake's own code, plans can `listen` on the GET stream, and the fake
  replays missed events. The seventh build used all of it in one session: 100%, independent 100%
  over 160 of 205 calls, nothing skipped. The eighth, the one in `examples/`, also learned that the
  server adds its capability tools only after `notifications/initialized`; its session ended at
  the usage limit during the review and a second one finished it from the saved progress.
- **The HTTP layer, learned.** That eighth build also wrote 856 lines of its own HTTP layer in
  front of FauxCall: CORS, Express's 404 page, the transport's refusals in their order, SSE
  events written as the SDK writes them, cancellation and stream resumption. FauxCall now learns
  or models all of it (introspection records the 404 page, path matching, CORS, body refusals
  and the order of the checks, the SSE format and priming), and the ninth build needed none: no middleware, just the learned settings. Its first session ended when
  the network dropped, after the world phase; the second resumed from the saved progress.
- **What the server sends on its own.** Parity used to compare only answers: the Everything
  server's log messages and resource updates every 5 s, its `list_changed` notices, task status
  updates and roots request went unchecked, and the fakes' timers ran on the wall clock. Scenarios
  now `listen` and compare what arrives (`heard`), calls compare their progress and log messages,
  and background work waits on the fake's clock. The tenth build, the one in `examples/`, compared
  all five kinds of message on the stream and every call's progress, at 100%, in one session. It
  also found that a GET or DELETE naming no session got a stream or a 200 where the server refuses
  it, that waits on the clock broke when a test suite used more than one event loop, that probes
  were recorded without their path and body, and that privacy redacted `maxTokens` as a secret;
  all four are fixed. It still added a small middleware for POSTs sent before a session exists
  (the order the SDK checks them in).
- **Consistency both ways.** Relations used to be checked one way only (every ticker's exchange
  exists), on the built data only: nothing said whether every exchange must list tickers, writes
  could leave references dangling, and the model's `on_delete` was never carried out. Relations
  now have a reverse minimum and write rules, each decided from evidence and the domain, enforced
  by the runtime (custom handlers included) and checked after every scenario. The rebuilt
  Reference API decided all three of its relations with evidence: every exchange lists tickers
  and every ticker has a quote (`min_per_target: 1`), tickers need no watchlist (`0`), and a
  watchlist naming an unknown symbol is refused, as three recorded writes showed. Its first
  session ended at the usage limit just before the report; the second resumed from the saved
  progress for $2.98.
- **Docs for agents.** Where auth probes go, how to probe an approved rate limit, how to record when
  the user prepared `.env` first, and a readable `fauxcall model schema --brief` (`ebcaa3d`,
  `a450dd1`, `818a252`, `05dcd98`).
