# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 140 scenarios.

Independent compliance **100%** over 160 of 205 calls. The others repeat recorded requests (38), which the fake answers from those recordings, or compare shape only (7).

| endpoint | compliance | weight |
|---|---|---|
| `MCP completion/complete` | 100.00% | 212.0/212.0 |
| `MCP logging/setLevel` | 100.00% | 58.0/58.0 |
| `MCP ping` | 100.00% | 9.0/9.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 61.0/61.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 37.0/37.0 |
| `MCP prompts/get:missing-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 96.5/96.5 |
| `MCP prompts/get:simple-prompt` | 100.00% | 13.0/13.0 |
| `MCP prompts/list` | 100.00% | 39.0/39.0 |
| `MCP resources/list` | 100.00% | 155.0/155.0 |
| `MCP resources/read` | 100.00% | 11.0/11.0 |
| `MCP resources/read:Demo://resource/dynamic/blob/9` | 100.00% | 12.5/12.5 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 61.0/61.0 |
| `MCP resources/read:demo://resource/dynamic/text/1/extra` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 83.0/83.0 |
| `MCP resources/read:demo://resource/session/anything.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/report.csv.gz` | 100.00% | 37.0/37.0 |
| `MCP resources/read:demo://resource/static/document/README.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:file:///etc/passwd` | 100.00% | 11.0/11.0 |
| `MCP resources/read:not a uri` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 18.0/18.0 |
| `MCP resources/templates/list` | 100.00% | 18.0/18.0 |
| `MCP resources/unsubscribe` | 100.00% | 20.0/20.0 |
| `MCP tasks/cancel` | 100.00% | 25.5/25.5 |
| `MCP tasks/get` | 100.00% | 83.5/83.5 |
| `MCP tasks/list` | 100.00% | 25.5/25.5 |
| `MCP tasks/result` | 100.00% | 59.5/59.5 |
| `MCP tools/call` | 100.00% | 11.0/11.0 |
| `MCP tools/call:Echo` | 100.00% | 13.0/13.0 |
| `MCP tools/call:does-not-exist` | 100.00% | 13.0/13.0 |
| `MCP tools/call:echo` | 100.00% | 135.0/135.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 100.0/100.0 |
| `MCP tools/call:get-env` | 100.00% | 12.0/12.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 336.0/336.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 135.0/135.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 25.0/25.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 99.0/99.0 |
| `MCP tools/call:get-sum` | 100.00% | 172.0/172.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 17.0/17.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 80.0/80.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 76.0/76.0 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 18.0/18.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 18.0/18.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 46.0/46.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 24.0/24.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 74.0/74.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 76.0/76.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 24.0/24.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 64.0/64.0 |
| `MCP tools/list` | 100.00% | 420.0/420.0 |
| `MCP tools/unknown` | 100.00% | 11.0/11.0 |
