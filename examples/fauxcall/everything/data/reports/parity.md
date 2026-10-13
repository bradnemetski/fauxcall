# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 129 scenarios.

Independent compliance **100%** over 144 of 192 calls. The others repeat recorded requests (48), which the fake answers from those recordings, or compare shape only (0).

| endpoint | compliance | weight |
|---|---|---|
| `MCP completion/complete` | 100.00% | 188.0/188.0 |
| `MCP logging/setLevel` | 100.00% | 47.0/47.0 |
| `MCP ping` | 100.00% | 9.0/9.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 50.0/50.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 61.0/61.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 74.5/74.5 |
| `MCP prompts/get:weather-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/list` | 100.00% | 39.0/39.0 |
| `MCP resources/list` | 100.00% | 161.0/161.0 |
| `MCP resources/read` | 100.00% | 11.0/11.0 |
| `MCP resources/read:DEMO://resource/dynamic/blob/6` | 100.00% | 12.5/12.5 |
| `MCP resources/read:demo://resource/dynamic/../dynamic/text/0x0A` | 100.00% | 12.5/12.5 |
| `MCP resources/read:demo://resource/dynamic/blob/` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 58.0/58.0 |
| `MCP resources/read:demo://resource/dynamic/text/3/4` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 92.5/92.5 |
| `MCP resources/read:demo://resource/session/never-made.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/parity-notes.txt.gz` | 100.00% | 26.0/26.0 |
| `MCP resources/read:demo://resource/static/document/README.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:https://example.com/resource` | 100.00% | 11.0/11.0 |
| `MCP resources/read:just words` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 47.0/47.0 |
| `MCP resources/templates/list` | 100.00% | 18.0/18.0 |
| `MCP resources/unsubscribe` | 100.00% | 18.0/18.0 |
| `MCP tasks/cancel` | 100.00% | 37.5/37.5 |
| `MCP tasks/get` | 100.00% | 115.0/115.0 |
| `MCP tasks/list` | 100.00% | 37.5/37.5 |
| `MCP tasks/result` | 100.00% | 34.5/34.5 |
| `MCP tools/call` | 100.00% | 11.0/11.0 |
| `MCP tools/call:echo` | 100.00% | 134.0/134.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 92.0/92.0 |
| `MCP tools/call:get-env` | 100.00% | 12.0/12.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 325.0/325.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 165.5/165.5 |
| `MCP tools/call:get-roots-list` | 100.00% | 13.0/13.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 95.0/95.0 |
| `MCP tools/call:get-sum` | 100.00% | 172.0/172.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 17.0/17.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 81.0/81.0 |
| `MCP tools/call:no-such-tool` | 100.00% | 13.0/13.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 55.0/55.0 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 36.0/36.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 36.0/36.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 13.0/13.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 13.0/13.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 49.0/49.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 13.0/13.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 13.0/13.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 13.0/13.0 |
| `MCP tools/explode` | 100.00% | 11.0/11.0 |
| `MCP tools/list` | 100.00% | 210.0/210.0 |
