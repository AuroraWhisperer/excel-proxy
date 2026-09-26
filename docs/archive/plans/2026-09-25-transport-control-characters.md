# Transport Control Character Recovery Implementation Plan

**Goal:** Decode unambiguous multiline tool literals without changing their contents, executing JavaScript, or dispatching partial batches.

**Architecture:** Extend the existing JSON decoder only for literal control characters inside strings. Continue to validate the complete envelope and catalog schema, and preserve the original native call for replay. Rejections retain safe structural diagnostics with a fixed JSON error category.

**Tech Stack:** Python standard library JSON parser, unittest, existing offline HTTP/SSE harness.

## Evidence and constraints
- The September 25 trace confirms upstream HTTP 200 and `response.completed`, followed by local `invalid_transport_envelope` at `arguments.code` (line 1, columns 294 and 6184). This is not an upstream connection failure.
- Failed native arguments were not retained. Literal control characters are a reproduced compatibility gap, not a proven reconstruction of those requests.
- Preserve all existing uncommitted work. Do not restart the user's proxy, replay paid model requests, enable prompt logging, commit, or push.
- Keep malformed quotes, truncated values, extra calls, unknown tools, and schema mismatches rejected. No new parser dependency, evaluation, or guessed string repair.

## Task 1: Reproduce and minimally repair literal decoding
**Files:** `tests/test_excel_tool_compatibility.py`, `app/excel_upstream.py`.
- [x] Add `test_literal_control_characters_preserve_arguments_and_replay` for function/custom calls, LF/CRLF/tab literals, JSON/decorated/double-encoded/invocation representations, and exact native replay.
- [x] Add `test_multiline_transport_rejects_incomplete_or_ambiguous_payloads` for missing quotes/braces, unescaped quotes, multiple objects/invocations, and wrong schema types.
- [x] Run the new unit tests before the fix: ten positive subcases failed; ambiguous/incomplete payloads remained rejected. The full pre-fix run also confirmed six new HTTP subcase failures and four missing-diagnostic errors.
- [x] Use `json.loads(..., strict=False)` at the existing inner-envelope and literal-invocation parsing points. Leave outer protocol decoding and complete-envelope/schema validation unchanged.

## Task 2: Verify HTTP contracts and improve safe diagnostics
**Files:** `tests/test_excel_tool_compatibility.py`, `app/excel_upstream.py`.
- [x] Exercise multiline custom calls over upstream/downstream JSON and SSE; check exact input, completed status, and one upstream request.
- [x] Classify JSON parser errors using a fixed allowlist; never expose parser messages or argument snippets. Test missing delimiters, trailing content, and unterminated strings, including a valid-first/invalid-second atomic batch.
- [x] Run the focused tests, the full documented offline runner, in-memory syntax compilation, and `git diff --check`. Record failures already present outside this change separately.
- [x] Report the verified compatibility improvement, the unavailable original payload, and restart requirements.

## Final verification (2026-09-25)
- Final transport compatibility module: 32 tests passed. Broader transport/upstream/stream/replay run: 149 tests passed.
- Final full offline runner: 263 tests, 261 passed, two unchanged baseline dashboard failures: `test_home_has_navigation_but_no_request_table` and `test_home_replaces_subscription_quota_with_api_costs`. Both failed before the production decoder change; neither dashboard code nor those tests was modified here.
- In-memory syntax compilation passed for both modified Python files. `git diff --check` passed; new test/plan files have no trailing whitespace.
- The running proxy process predates this patch. No process was stopped or restarted. Close the native proxy window and relaunch through `启动.vbs` after active tasks finish to load the change.
- Scope remains a verified same-class compatibility repair. The exact failed native arguments behind the screenshot remain unavailable; safe JSON error categories improve diagnosis if another malformed representation recurs.
