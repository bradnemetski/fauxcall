# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 171 scenarios.

Independent compliance **100%** over 181 of 238 calls. The others repeat recorded requests (57), which the fake answers from those recordings, or compare shape only (0).

| endpoint | compliance | weight |
|---|---|---|
| `MCP completion/complete` | 100.00% | 204.0/204.0 |
| `MCP logging/setLevel` | 100.00% | 49.0/49.0 |
| `MCP ping` | 100.00% | 9.0/9.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 74.0/74.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 37.0/37.0 |
| `MCP prompts/get:no-such-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 96.5/96.5 |
| `MCP prompts/get:simple-prompt` | 100.00% | 26.0/26.0 |
| `MCP prompts/list` | 100.00% | 78.0/78.0 |
| `MCP resources/delete` | 100.00% | 11.0/11.0 |
| `MCP resources/list` | 100.00% | 243.0/243.0 |
| `MCP resources/read` | 100.00% | 22.0/22.0 |
| `MCP resources/read:demo://elsewhere/x` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 59.5/59.5 |
| `MCP resources/read:demo://resource/dynamic/text/1/2` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 84.5/84.5 |
| `MCP resources/read:demo://resource/session/anything.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/cycle.txt.gz` | 100.00% | 26.0/26.0 |
| `MCP resources/read:demo://resource/session/two words.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/two%20words.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/README.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:https://example.com/doc.md` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 18.0/18.0 |
| `MCP resources/templates/list` | 100.00% | 36.0/36.0 |
| `MCP resources/unsubscribe` | 100.00% | 29.0/29.0 |
| `MCP sampling/createMessage` | 100.00% | 11.0/11.0 |
| `MCP tasks/cancel` | 100.00% | 48.5/48.5 |
| `MCP tasks/get` | 100.00% | 73.0/73.0 |
| `MCP tasks/list` | 100.00% | 26.5/26.5 |
| `MCP tasks/result` | 100.00% | 83.0/83.0 |
| `MCP tools/call` | 100.00% | 11.0/11.0 |
| `MCP tools/call:echo` | 100.00% | 150.0/150.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 183.0/183.0 |
| `MCP tools/call:get-env` | 100.00% | 12.0/12.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 402.0/402.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 183.0/183.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 13.0/13.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 84.0/84.0 |
| `MCP tools/call:get-sum` | 100.00% | 171.0/171.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 17.0/17.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 109.0/109.0 |
| `MCP tools/call:no-such-tool` | 100.00% | 13.0/13.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 86.0/86.0 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 23.0/23.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 23.0/23.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 56.0/56.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 28.0/28.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 99.0/99.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 51.0/51.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 24.0/24.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 99.0/99.0 |
| `MCP tools/list` | 100.00% | 2100.0/2100.0 |
