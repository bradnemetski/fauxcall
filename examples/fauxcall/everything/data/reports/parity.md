# Parity report — PASS

Overall compliance **99.91%** (threshold 98%) across 170 scenarios.

Independent compliance **99.91%** over 157 of 213 calls. The others repeat recorded requests (53), which the fake answers from those recordings, or compare shape only (3).

| endpoint | compliance | weight |
|---|---|---|
| `MCP completion/complete` | 100.00% | 229.0/229.0 |
| `MCP logging/setLevel` | 100.00% | 47.0/47.0 |
| `MCP ping` | 100.00% | 36.0/36.0 |
| `MCP prompts/get` | 100.00% | 11.0/11.0 |
| `MCP prompts/get:args-prompt` | 100.00% | 61.0/61.0 |
| `MCP prompts/get:completable-prompt` | 100.00% | 13.0/13.0 |
| `MCP prompts/get:resource-prompt` | 100.00% | 84.0/84.0 |
| `MCP prompts/get:simple-prompt` | 100.00% | 13.0/13.0 |
| `MCP prompts/get:weather-prompt` | 100.00% | 11.0/11.0 |
| `MCP prompts/list` | 100.00% | 78.0/78.0 |
| `MCP resources/delete` | 100.00% | 11.0/11.0 |
| `MCP resources/list` | 98.68% | 75.0/76.0 |
| `MCP resources/read` | 100.00% | 11.0/11.0 |
| `MCP resources/read:Demo://resource/dynamic/text/5` | 100.00% | 12.0/12.0 |
| `MCP resources/read:demo://bad host/x` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/blob/%2E%2E/text/12` | 100.00% | 12.0/12.0 |
| `MCP resources/read:demo://resource/dynamic/blob/{resourceId}` | 100.00% | 114.0/114.0 |
| `MCP resources/read:demo://resource/dynamic/image/5` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/5/` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/dynamic/text/{resourceId}` | 100.00% | 60.0/60.0 |
| `MCP resources/read:demo://resource/session/e2e-notes.txt.gz` | 100.00% | 26.0/26.0 |
| `MCP resources/read:demo://resource/session/never-created.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/session/x.gz` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/./../document/startup.md` | 100.00% | 12.0/12.0 |
| `MCP resources/read:demo://resource/static/document/README.md` | 100.00% | 11.0/11.0 |
| `MCP resources/read:demo://resource/static/document/architecture.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/extension.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/features.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/how-it-works.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/instructions.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/startup.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource/static/document/structure.md` | 100.00% | 13.0/13.0 |
| `MCP resources/read:demo://resource:123456/dynamic/text/1` | 100.00% | 11.0/11.0 |
| `MCP resources/read:file:///etc/hosts` | 100.00% | 11.0/11.0 |
| `MCP resources/read:just words` | 100.00% | 11.0/11.0 |
| `MCP resources/subscribe` | 100.00% | 29.0/29.0 |
| `MCP resources/templates/list` | 100.00% | 18.0/18.0 |
| `MCP resources/unsubscribe` | 100.00% | 27.0/27.0 |
| `MCP sampling/createMessage` | 100.00% | 11.0/11.0 |
| `MCP tasks/cancel` | 100.00% | 37.5/37.5 |
| `MCP tasks/get` | 98.90% | 179.5/181.5 |
| `MCP tasks/list` | 100.00% | 26.5/26.5 |
| `MCP tasks/result` | 100.00% | 34.5/34.5 |
| `MCP tools/call:Echo` | 100.00% | 13.0/13.0 |
| `MCP tools/call:echo` | 100.00% | 174.0/174.0 |
| `MCP tools/call:get-annotated-message` | 100.00% | 154.0/154.0 |
| `MCP tools/call:get-env` | 100.00% | 24.0/24.0 |
| `MCP tools/call:get-resource-links` | 100.00% | 326.0/326.0 |
| `MCP tools/call:get-resource-reference` | 100.00% | 113.0/113.0 |
| `MCP tools/call:get-roots-list` | 100.00% | 25.0/25.0 |
| `MCP tools/call:get-structured-content` | 100.00% | 99.0/99.0 |
| `MCP tools/call:get-sum` | 100.00% | 182.0/182.0 |
| `MCP tools/call:get-tiny-image` | 100.00% | 34.0/34.0 |
| `MCP tools/call:get_sum` | 100.00% | 13.0/13.0 |
| `MCP tools/call:gzip-file-as-resource` | 100.00% | 68.0/68.0 |
| `MCP tools/call:simulate-research-query` | 100.00% | 70.5/70.5 |
| `MCP tools/call:toggle-simulated-logging` | 100.00% | 27.0/27.0 |
| `MCP tools/call:toggle-subscriber-updates` | 100.00% | 18.0/18.0 |
| `MCP tools/call:trigger-elicitation-request` | 100.00% | 26.0/26.0 |
| `MCP tools/call:trigger-long-running-operation` | 100.00% | 37.0/37.0 |
| `MCP tools/call:trigger-sampling-request` | 100.00% | 26.0/26.0 |
| `MCP tools/describe` | 100.00% | 11.0/11.0 |
| `MCP tools/list` | 100.00% | 420.0/420.0 |

## Failed checks

| scenario | call | check | detail |
|---|---|---|---|
| tests/e2e/test_stateful.py::test_gzip_registers_a_session_resource | `MCP resources/list` | length $.resources | 8 vs 7 |
| tests/e2e/test_stateful.py::test_ambiguous_research_for_a_client_that_can_elicit | `MCP tasks/get` | value $.status | 'input_required' vs 'completed' |
| tests/e2e/test_stateful.py::test_ambiguous_research_for_a_client_that_can_elicit | `MCP tasks/get` | value $.statusMessage | 'Found multiple interpretations for "jaguar". Requesting clarification...' vs 'Generating report...' |
