# Stream, Tool Calls and Images Implementation Plan

> Execute inline in this session; preserve unrelated working-tree changes.

**Goal:** Fix reproducible multi-call and SSE disconnect paths and enable native image generation/editing without regressing vision.
**Architecture:** Extend existing Excel adapters and managed Codex configuration. Keep image request validation/translation in a dedicated module; keep transport ownership and cancellation in the existing proxy.
**Tech Stack:** Python 3.12, FastAPI, httpx, unittest.

## Global Constraints
- Use the repository virtualenv and isolated tools/test-proxy-contracts.py runner; never broad pytest discovery.
- Do not restart the running user proxy for isolated QA or expose session credentials.
- Preserve unrelated dirty files and existing vision uploads; never convert genuinely unfinished calls into successful execution.

## 1. Tool calls and streaming
Files: app/excel_upstream.py, app/proxy.py, app/format_translation.py, tests/test_excel_stream_recovery.py.
- [x] Add regression tests: one/two valid calls, mixed invalid calls, parallel=false, ordered indices and replay; expect the current two-call conversion to fail.
- [x] Add stream tests: interrupted frame, completed final item without terminal, unfinished/commentary refusal, heartbeat/cancellation, bounded safe transport errors.
- [x] Implement plural conversion and per-round agent iteration; retain singular compatibility entry points.
- [x] Implement safe trailing-frame parsing, conditional terminal reconstruction, periodic in-progress events and error terminal mapping.
- [x] Run: .venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_stream_recovery test_excel_upstream test_excel_continuity test_excel_contracts test_reasoning_translation

## 2. Native image generation and editing
Files: app/excel_image_generation.py, app/proxy.py, app/constants.py, app/proxy_client_config.py, tests/test_excel_image_generation.py, tests/test_proxy_client_config.py.
- [x] Add failing mock-HTTP tests for generation/edit aliases, upstream paths, JSON/multipart payloads, session auth, invalid fields/images and redacted upstream errors.
- [x] Implement validated gpt-image-2 requests against the existing Excel session; no arbitrary URL downloads or new third-party API keys.
- [x] Enable image tool header in managed provider configuration while preserving configuration restoration and advertise parallel capability.
- [x] Run: .venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_image_generation test_excel_images test_excel_request_compat test_proxy_client_config

## 3. Integration and handoff
Files: tools/test-proxy-contracts.py, readme.md.
- [x] Register regressions and document supported image options, stream semantics and restart requirements.
- [x] Run the selected offline suite, syntax checks and git diff --check.
- [x] Exercise repaired routes with an isolated application; if the authenticated backend is available, run one minimal image round-trip and report any upstream restrictions.
- [x] Review the complete task diff; report tested behavior separately from running-service activation.

## Verification Results — 2026-09-25

- Reproduced the original multi-call and incomplete-stream failures before implementation. Also reproduced and fixed a singular-helper compatibility regression (`StopIteration` when the original response contains two calls).
- Final focused regression run: **199 tests passed**, using the selected runner with only the unrelated dashboard-page module omitted.
- Full selected runner: **205 tests; 203 passed, 2 failed**. Both remaining failures belong to the pre-existing dashboard/API-cost redesign: `test_home_has_navigation_but_no_request_table` and `test_home_replaces_subscription_quota_with_api_costs`. Their assertions were not weakened and those UI files were not changed for this repair.
- Syntax checks passed for 12 affected Python files. `git diff --check` passed; Git reports an existing CRLF conversion warning for `tests/test_excel_contracts.py`.
- Isolated authenticated backend checks used a read-only load of the existing encrypted session, temporary config/state/cache directories, disabled session-refresh writes, and an in-process ASGI client. No server was started, no user process was restarted, and no live Codex configuration was changed. Temporary clients/executors and directories were cleaned up.
- Live native tool conversion returned HTTP 200 with one terminal `response.completed`. The model emitted one probe despite a two-call prompt, so real multi-call generation is **not** claimed; ordered multi-call batches, mixed custom/function calls, invalid-batch rejection, and replay are verified offline.
- A separate live tool-result replay returned HTTP 200 and one completed terminal response in both rounds, with no remaining tool calls in the final answer. The diagnostic exact-phrase comparison was false; this confirms protocol continuity, not exact wording compliance.
- Live `/v1/images/generations` returned HTTP 200 with one image; passing that image to `/v1/images/edits` returned HTTP 200 with one edited image. Existing vision/upload regressions pass.
- Changes are uncommitted. Existing unrelated worktree edits and untracked files were preserved.

## Activation Required

The currently running proxy and Codex session were deliberately left untouched. After active tasks finish, close the Excel Proxy dashboard, launch `D:\Work\ghcp_proxy\启动.vbs`, refresh/enable Codex integration in the dashboard, and restart Codex. Merely saving source files or opening a second launcher does not reload the existing process. The regenerated catalog advertises parallel calls and the managed provider includes the native image-tool header.
