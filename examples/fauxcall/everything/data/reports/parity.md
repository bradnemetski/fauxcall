# Parity report — PASS

Overall compliance **100%** (threshold 98%) across 228 scenarios.

Independent compliance **100%** over 243 of 352 calls. The others repeat recorded requests (83), which the fake answers from those recordings, or compare shape only (26).

| endpoint | compliance | weight |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` | 100.00% | 7.0/7.0 |
| `GET /MCP` | 100.00% | 12.0/12.0 |
| `GET /Mcp/` | 100.00% | 12.0/12.0 |
| `GET /mcp` | 100.00% | 72.0/72.0 |
| `GET /mcp/` | 100.00% | 12.0/12.0 |
| `GET /mcp/extra` | 100.00% | 7.0/7.0 |
| `GET /not-mcp` | 100.00% | 7.0/7.0 |
| `GET /sse` | 100.00% | 7.0/7.0 |
| `MCP completion/complete` | 100.00% | 297.0/297.0 |
| `MCP does/not-exist` | 100.00% | 11.0/11.0 |
| `MCP elicitation/create` | 100.00% | 11.0/11.0 |
| `MCP logging/setLevel` | 100.00% | 87.0/87.0 |
| `MCP ping` | 100.00% | 18.0/18.0 |
| `MCP prompts/get` | 100.00% | 22.0/22.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 85.0/85.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 48.0/48.0 |
| `MCP prompts/get:missing-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 79.0/79.0 |
| `MCP prompts/get:simple-prompt` | 100.00% | 26.0/26.0 |
| `MCP prompts/list` | 100.00% | 78.0/78.0 |
| `MCP resources/list` | 100.00% | 196.0/196.0 |
| `MCP resources/read` | 100.00% | 33.0/33.0 |
| `MCP resources/read:demo://elsewhere/thing` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 134.5/134.5 |
| `MCP resources/read:demo://resource/dynamic/text/` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/1/2` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 145.5/145.5 |
| `MCP resources/read:demo://resource/session/never-made.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/notes.txt.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/session/report.csv.gz` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/session/x.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/Features.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/a b.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/missing.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:https://example.com/not-a-resource` | 100.00% | 11.0/11.0 |
| `MCP resources/read:no scheme here` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 65.0/65.0 |
| `MCP resources/templates/list` | 100.00% | 36.0/36.0 |
| `MCP resources/unsubscribe` | 100.00% | 18.0/18.0 |
| `MCP sampling/createMessage` | 100.00% | 11.0/11.0 |
| `MCP stream:notifications/message` | 100.00% | 70.0/70.0 |
| `MCP stream:notifications/resources/list_changed` | 100.00% | 20.0/20.0 |
| `MCP stream:notifications/resources/updated` | 100.00% | 29.0/29.0 |
| `MCP stream:notifications/tasks/status` | 100.00% | 20.0/20.0 |
| `MCP stream:roots/list` | 100.00% | 20.0/20.0 |
| `MCP tasks/cancel` | 100.00% | 53.0/53.0 |
| `MCP tasks/get` | 100.00% | 161.5/161.5 |
| `MCP tasks/list` | 100.00% | 37.5/37.5 |
| `MCP tasks/result` | 100.00% | 83.0/83.0 |
| `MCP tools/call` | 100.00% | 33.0/33.0 |
| `MCP tools/call:echo` | 100.00% | 194.0/194.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 209.0/209.0 |
| `MCP tools/call:get-env` | 100.00% | 24.0/24.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 495.0/495.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 183.0/183.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 49.0/49.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 97.0/97.0 |
| `MCP tools/call:get-sum` | 100.00% | 243.0/243.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 34.0/34.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 95.0/95.0 |
| `MCP tools/call:no-such-tool` | 100.00% | 13.0/13.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 117.0/117.0 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 23.0/23.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 23.0/23.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 86.0/86.0 |
| `MCP tools/call:trigger-elicitation-request-async` | 100.00% | 40.0/40.0 |
| `MCP tools/call:trigger-elicitation-request-async:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-elicitation-request:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 147.0/147.0 |
| `MCP tools/call:trigger-long-running-operation:notifications` | 100.00% | 80.0/80.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 76.0/76.0 |
| `MCP tools/call:trigger-sampling-request-async` | 100.00% | 36.0/36.0 |
| `MCP tools/call:trigger-sampling-request-async:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-sampling-request:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/call:trigger-url-elicitation` | 100.00% | 99.0/99.0 |
| `MCP tools/call:trigger-url-elicitation:notifications` | 100.00% | 10.0/10.0 |
| `MCP tools/list` | 100.00% | 1680.0/1680.0 |
| `OPTIONS /mcp` | 100.00% | 6.0/6.0 |
