# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 60 scenarios.

Independent compliance **100%** over 173 of 257 calls. The others repeat recorded requests (58), which the fake answers from those recordings, or compare shape only (26).

| endpoint | compliance | weight |
|---|---|---|
| `GET /MCP` | 100.00% | 12.0/12.0 |
| `GET /mcp` | 100.00% | 24.0/24.0 |
| `GET /mcp/` | 100.00% | 12.0/12.0 |
| `GET /mcp/extra` | 100.00% | 7.0/7.0 |
| `GET /sse` | 100.00% | 7.0/7.0 |
| `MCP completion/complete` | 100.00% | 267.0/267.0 |
| `MCP logging/setLevel` | 100.00% | 67.0/67.0 |
| `MCP ping` | 100.00% | 18.0/18.0 |
| `MCP prompts/get` | 100.00% | 11.0/11.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 59.0/59.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 24.0/24.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 67.0/67.0 |
| `MCP prompts/get:simple-prompt` | 100.00% | 13.0/13.0 |
| `MCP prompts/get:weather-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/list` | 100.00% | 39.0/39.0 |
| `MCP resources/list` | 100.00% | 120.0/120.0 |
| `MCP resources/read` | 100.00% | 22.0/22.0 |
| `MCP resources/read:DEMO://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/dynamic/audio/1` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/../text/12` | 100.00% | 12.0/12.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 70.0/70.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 93.0/93.0 |
| `MCP resources/read:demo://resource/session/greeting.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/session/notes.txt.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/session/nothing-here.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/u.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/./features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/missing.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:file:///etc/hosts` | 100.00% | 11.0/11.0 |
| `MCP resources/read:just words` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 47.0/47.0 |
| `MCP resources/templates/list` | 100.00% | 18.0/18.0 |
| `MCP resources/unsubscribe` | 100.00% | 9.0/9.0 |
| `MCP stream:notifications/message` | 100.00% | 70.0/70.0 |
| `MCP stream:notifications/resources/list_changed` | 100.00% | 10.0/10.0 |
| `MCP stream:notifications/resources/updated` | 100.00% | 29.0/29.0 |
| `MCP stream:notifications/tasks/status` | 100.00% | 10.0/10.0 |
| `MCP stream:roots/list` | 100.00% | 20.0/20.0 |
| `MCP tasks/cancel` | 100.00% | 48.5/48.5 |
| `MCP tasks/get` | 100.00% | 68.5/68.5 |
| `MCP tasks/list` | 100.00% | 48.5/48.5 |
| `MCP tasks/result` | 100.00% | 57.5/57.5 |
| `MCP tasks/result:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:echo` | 100.00% | 134.0/134.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 101.0/101.0 |
| `MCP tools/call:get-env` | 100.00% | 12.0/12.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 170.0/170.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 87.0/87.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 36.0/36.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 69.0/69.0 |
| `MCP tools/call:get-sum` | 100.00% | 134.0/134.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 17.0/17.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 107.0/107.0 |
| `MCP tools/call:no-such-tool` | 100.00% | 24.0/24.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 70.5/70.5 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 38.0/38.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 18.0/18.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 70.0/70.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 28.0/28.0 |
| `MCP tools/call:trigger-elicitation-request-async:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-elicitation-request:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 153.0/153.0 |
| `MCP tools/call:trigger-long-running-operation:notifications` | 100.00% | 80.0/80.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 62.0/62.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 37.0/37.0 |
| `MCP tools/call:trigger-sampling-request-async:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-sampling-request:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 84.5/84.5 |
| `MCP tools/call:trigger-url-elicitation:notifications` | 100.00% | 20.0/20.0 |
| `MCP tools/list` | 100.00% | 1470.0/1470.0 |
| `OPTIONS /mcp` | 100.00% | 6.0/6.0 |
