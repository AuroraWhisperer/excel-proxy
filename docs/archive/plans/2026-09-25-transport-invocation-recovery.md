# Transport Invocation Recovery Implementation Plan

**Goal:** Recover unambiguous BPS tool representations without executing JavaScript, changing literal arguments, or dispatching incomplete tool batches.

**Architecture:** Extend the existing transport decoder rather than adding a second dispatch path. Decode a complete single-call expression as data, then reuse catalog resolution, schema validation, atomic batch validation, and exact native-call replay. Keep failures terminal and expose only structural diagnostics.

**Tech Stack:** Python, unittest, existing Responses JSON/SSE adapters.

## Constraints and evidence
- Preserve the existing dirty worktree, runtime settings, and running proxy. No automatic restart, upstream model replay, commit, or push.
- The screenshot and local trace confirm `invalid_transport_envelope`. The trace does not retain the failed native argument payload, so its precise malformed shape is not proven.
- Reference: https://github.com/ranxi2001/sub2api/releases/tag/v2.8.13 and its `backend/internal/service/basispoints/invocation_recovery_test.go` at that tag. Reimplement the compatibility behavior locally; do not copy Go source or add dependencies.
- Borrow bounded single-call recovery, literal preservation, and safe structural diagnostics. Account pooling, credential services, request-body capture, billing, and UI changes are separate work, not part of this repair.

## Task 1: Reproduce and repair decoding
**Files:** `app/excel_upstream.py`, `tests/test_excel_tool_compatibility.py`.
- [x] Add tests for `await functions.exec_command({"cmd":"pwd"});`, custom string literals, object-valued outer arguments, qualified names, and exact replay.
- [x] Run `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_tool_compatibility.ExcelToolCompatibilityTests` and verify the new positive cases fail.
- [x] Parse a single complete invocation with a JSON argument; reject multiple calls, expressions, unknown tools, wrong argument types, and incomplete payloads. Continue using the existing catalog/schema validators.
- [x] Preserve the original native call and JSON integers (including `9007199254740993`) without evaluation or rewriting literal strings.

## Task 2: Diagnostics and end-to-end contracts
**Files:** `app/excel_upstream.py`, `tests/test_excel_tool_compatibility.py`.
- [x] Include only fixed stage/type labels and JSON line/column numbers in transport rejection messages; never include argument values or parser exception text.
- [x] Cover upstream JSON/SSE and downstream JSON/SSE, exact custom input, no partial tool dispatch, no generation retry, and unchanged failed-response semantics.
- [x] Run `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py`, in-memory syntax compilation, and `git diff --check`.
- [x] Record actual verification results and the need to restart after active tasks finish.

## Verification results (2026-09-25)
- Before the decoder fix: 25 focused tests ran; the new positive cases produced 11 failing subtests. The unsafe invocation cases remained rejected.
- Before diagnostic implementation: the new structural-hint tests produced four errors and the HTTP diagnostic assertions produced three failures, as expected.
- Final focused run: 89 tests passed across `test_excel_tool_compatibility`, `test_excel_contracts`, `test_excel_stream_recovery`, and `test_excel_continuity`.
- Full documented offline runner: 260 tests ran; 258 passed. Two unrelated `test_dashboard_pages.DashboardPagesTests` tests failed: `test_home_has_navigation_but_no_request_table` expects `/api/dashboard`, and `test_home_replaces_subscription_quota_with_api_costs` expects `id="cost-heading"`. The current page lacks both. Neither that page nor those tests was edited in this task; leave the separate dashboard work intact.
- Both edited Python files pass syntax parsing; `git diff --check` passes (Git reports existing line-ending conversion warnings).
- No live generation, running-service restart, settings change, commit, or push performed. Restart the proxy after active tasks finish, then verify with a new task. Because the original failed native payload was not retained, live reproduction of that exact failure remains unverified.

## Reference adoption assessment
| Reference mechanism | Local outcome |
| --- | --- |
| Recover a complete single catalog invocation | Implemented in the existing decoder; no JavaScript runtime or new dependency. |
| Preserve custom literals and large JSON integers | Covered by exact-value and native-replay regressions. |
| Report safe field/type diagnostics | Implemented without recording body text, commands, credentials, or exception messages. |
| Refuse partial tool execution | Existing atomic-batch behavior retained and verified with an invalid second invocation over JSON/SSE. |
| Error-only request-body capture | Deferred: requires explicit retention, quota, redaction, and opt-in controls. Diagnostics added here do not turn on raw capture. |
| Credential guard and multi-account administration | Deferred: different account/deployment model; do not send local session credentials to new services. |
| Usage conversion and reasoning-level display | Separate accounting/UI scope; do not mix it into a tool-translation repair. |
