# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 233 scenarios.

Independent compliance **100%** over 234 of 289 calls. The others repeat recorded requests (55), which the fake answers from those recordings, or compare shape only (0).

| endpoint | compliance | weight |
|---|---|---|
| `GET /` | 100.00% | 7.0/7.0 |
| `GET /.well-known/oauth-protected-resource` | 100.00% | 7.0/7.0 |
| `GET /MCP` | 100.00% | 12.0/12.0 |
| `GET /Mcp/` | 100.00% | 12.0/12.0 |
| `GET /health` | 100.00% | 7.0/7.0 |
| `GET /mcp` | 100.00% | 36.0/36.0 |
| `GET /mcp/` | 100.00% | 12.0/12.0 |
| `GET /mcp//` | 100.00% | 7.0/7.0 |
| `GET /mcp/messages` | 100.00% | 7.0/7.0 |
| `GET /sse` | 100.00% | 7.0/7.0 |
| `HEAD /mcp` | 100.00% | 6.0/6.0 |
| `HEAD /nothing-here` | 100.00% | 6.0/6.0 |
| `MCP completion/complete` | 100.00% | 276.0/276.0 |
| `MCP initialize` | 100.00% | 22.0/22.0 |
| `MCP logging/setLevel` | 100.00% | 49.0/49.0 |
| `MCP ping` | 100.00% | 102.0/102.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 70.0/70.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 37.0/37.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 74.5/74.5 |
| `MCP prompts/get:simple-prompt` | 100.00% | 13.0/13.0 |
| `MCP prompts/get:weather-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/list` | 100.00% | 50.0/50.0 |
| `MCP resources/delete` | 100.00% | 11.0/11.0 |
| `MCP resources/list` | 100.00% | 82.0/82.0 |
| `MCP resources/read` | 100.00% | 22.0/22.0 |
| `MCP resources/read://resource/x` | 100.00% | 11.0/11.0 |
| `MCP resources/read:DEMO://resource/dynamic/text/8` | 100.00% | 12.5/12.5 |
| `MCP resources/read:DEMO://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:Demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:HTTPS://Example.org` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 122.0/122.0 |
| `MCP resources/read:demo://resource/dynamic/text/../blob/4` | 100.00% | 12.5/12.5 |
| `MCP resources/read:demo://resource/dynamic/text/3/4` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 155.0/155.0 |
| `MCP resources/read:demo://resource/session/README.md.gz` | 100.00% | 12.5/12.5 |
| `MCP resources/read:demo://resource/session/drafts.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/notes.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/session/weather.csv.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/./extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/README.md` | 100.00% | 22.0/22.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:mailto:someone@example.com` | 100.00% | 11.0/11.0 |
| `MCP resources/read:not a uri` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 36.0/36.0 |
| `MCP resources/templates/list` | 100.00% | 29.0/29.0 |
| `MCP resources/unsubscribe` | 100.00% | 27.0/27.0 |
| `MCP resources/watch` | 100.00% | 11.0/11.0 |
| `MCP tasks/cancel` | 100.00% | 48.5/48.5 |
| `MCP tasks/get` | 100.00% | 84.0/84.0 |
| `MCP tasks/list` | 100.00% | 59.5/59.5 |
| `MCP tasks/result` | 100.00% | 95.5/95.5 |
| `MCP tools/call:echo` | 100.00% | 178.0/178.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 127.0/127.0 |
| `MCP tools/call:get-env` | 100.00% | 12.0/12.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 280.0/280.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 122.0/122.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 25.0/25.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 58.0/58.0 |
| `MCP tools/call:get-sum` | 100.00% | 181.0/181.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 17.0/17.0 |
| `MCP tools/call:get-weather` | 100.00% | 24.0/24.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 148.0/148.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 128.0/128.0 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 46.0/46.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 23.0/23.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 112.0/112.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 41.0/41.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 61.0/61.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 77.0/77.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 62.0/62.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 99.0/99.0 |
| `MCP tools/list` | 100.00% | 1691.0/1691.0 |
| `OPTIONS /anything/else` | 100.00% | 6.0/6.0 |
| `OPTIONS /mcp` | 100.00% | 12.0/12.0 |
