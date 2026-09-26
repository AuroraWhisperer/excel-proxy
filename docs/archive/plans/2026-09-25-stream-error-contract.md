# Structured Stream Errors Implementation Plan

> **For agentic workers:** Execute inline, task-by-task. No delegation, commits, service restarts, or edits to unrelated dashboard work.

**Goal:** Stop locally rejected Excel responses from abruptly closing the client HTTP stream and expose safe diagnostic codes.

**Architecture:** Classify local response-validation failures separately from actual HTTP transport errors. Emit one terminal Responses error event, preserve failed usage/trace status, and close upstream resources without replaying generation or exposing incomplete tools.

**Tech Stack:** Python, httpx, FastAPI/Starlette, unittest.

## Global Constraints
- Preserve all pre-existing uncommitted changes.
- Run offline contracts using the repository virtualenv; no broad pytest discovery or live model calls.
- Do not treat incomplete responses as success, expose upstream free text, or retry ambiguous model requests.

### Task 1: Reproduce failures
**Files:** `tests/test_excel_contracts.py`
- [x] Add HTTP regressions for invalid/empty completion, unknown tools, incomplete streams, safe codes, and no replay.
- [x] Verify the new tests fail on abrupt closure and generic non-streaming errors.
```powershell
.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_contracts.ExcelHTTPContractTests
```

### Task 2: Safely terminate rejected responses
**Files:** `app/upstream_errors.py`, `app/proxy.py`
- [x] Classify only local response-validation raises:
```python
class ExcelResponseError(httpx.RemoteProtocolError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
```
- [x] Catch this type in the managed stream, finalize with `response_validation_error`, emit one `response.failed` event containing a failed response and a safe error code/message, then stop iteration. Keep transport errors unchanged.
- [x] Record safe code/message fields in lifecycle traces; return structured 502 JSON for non-streaming failures.
- [x] Run focused and full offline contracts, check syntax and the diff; include the required proxy restart in the handoff.
```powershell
.venv/Scripts/python.exe -B tools/test-proxy-contracts.py
```

Client compatibility check: Codex ignores generic `error` events; use `response.failed`. Codex owns its retry policy, so do not claim to suppress all client retries or forge `invalid_prompt` errors.

## Verification (2026-09-25)

- Focused Excel contracts and continuity: 40 tests passed.
- Full offline contract runner: 177 tests passed.
- Syntax compilation passed for all three changed Python files; git diff --check passed.
- Existing dashboard/quota changes preserved. No live model requests, service restart, commit, or push performed.
- Deployment remains pending: restart the proxy after active tasks finish. Live Codex end-to-end behavior has not been exercised.
