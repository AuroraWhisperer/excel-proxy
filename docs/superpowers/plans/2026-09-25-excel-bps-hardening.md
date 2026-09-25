# Excel BPS Hardening Implementation Plan

**Goal:** Preserve tool identity, prevent unsafe generation retries, protect error diagnostics, reject unsupported request contracts, and provide a manual connection test with automated offline regression checks.

**Architecture:** Keep the existing Excel adapter and dashboard structure. Normalize Excel errors at the proxy boundary, reuse the real request path for the manual test, and run an explicit unittest suite with isolated application directories and blocked HTTP transports.

**Tech Stack:** Python 3.11+, FastAPI, httpx, unittest, existing Vue dashboard, GitHub Actions.

## Constraints

- Preserve the current uncommitted changes and existing client integrations.
- Use the repository virtual environment; exclude `mutants/`.
- No real model requests during implementation or validation.
- Keep connection-establishment retries; never replay a generation after a response or ambiguous transport failure.
- Keep normal error diagnostics content-free. Detailed error fields require the existing debug prompt logging setting and must redact credentials.
- Connection tests are manual, use a listed Excel model, and report that they consume upstream quota.

## 1. Request contracts and replay identity

Files: `excel_upstream.py`, `test_excel_upstream.py`, `test_excel_contracts.py`.

- [x] Add regression cases for valid, missing, incompatible, oversized and colliding result IDs, including custom-tool history and repeat replay.
- [x] Preserve valid result IDs; otherwise derive a stable `fc_` ID distinct from the native call ID while keeping `call_id` and output content.
- [x] Reject `previous_response_id`, forced tool selection and structured output before sending upstream. Return the offending parameter in a 400 response.

Acceptance examples:

```python
assert replay[0]['id'] != replay[1]['id']
assert replay[1]['call_id'] == original_result['call_id']
assert replay[1]['output'] == original_result['output']
assert response.status_code == 400
assert upstream_requests == []
```

## 2. Retry and error boundaries

Files: `proxy.py`, `upstream_errors.py`, `test_excel_continuity.py`, `test_excel_contracts.py`.

- [x] Reproduce partial SSE followed by EOF and assert exactly one model request.
- [x] Remove the non-streaming protocol-error retry loop; retain the shared connect-only retry policy.
- [x] Return controlled Excel HTTP/SSE errors with the original HTTP status and request ID where available.
- [x] Default error traces to diagnostic metadata. When detailed tracing is enabled, retain only bounded error fields and redact known credentials, bearer values and credential-bearing URLs.
- [x] Cover streaming and non-streaming rejections, terminal failure, trace settings and request ID retention.

Acceptance examples:

```python
assert send.await_count == 1
assert result.status_code == 502
assert 'PRIVATE_PROMPT' not in result.body.decode()
assert 'FAKE_SECRET' not in json.dumps(trace)
```

## 3. Manual Excel connection test

Files: `proxy.py`, `dashboard.html`, `test_excel_contracts.py`, `readme.md`.

- [x] Add `POST /api/config/excel-session/test` with JSON `{ "model": "gpt-5.6-sol-excel" }`.
- [x] Enforce a loopback host, same-origin browser requests, JSON input and an allowlisted Excel model. Reuse `_handle_excel_responses` with a fixed short text request and a bounded timeout.
- [x] Return `ok`, `category`, `message` and `model`; include only safe status/request ID diagnostics. Treat HTTP 200 without completed text as a failed test.
- [x] Add a model selector and manual test button to the existing Excel card. Disable repeat clicks, explain quota use, and announce results through an accessible live region.
- [x] Verify missing credentials, upstream rejection, protocol failure, timeout and success using mock transports; inspect the actual dashboard with a separate fixture server.

## 4. Regression entrypoint and CI

Files: `tools/test-proxy-contracts.py`, `.github/workflows/proxy-contracts.yml`, `readme.md`, `AGENTS.md`.

- [x] Add an explicit unittest runner for Excel, image, replay, reasoning and prompt-archive regressions, supporting optional test names for focused runs.
- [x] Isolate config/state/cache under a temporary directory before imports. Block real httpx transports and run without bytecode writes.
- [x] Run the suite on pull requests and main pushes using Windows and Linux Python 3.11 virtual environments.
- [x] Run the full selected suite once after the changes, syntax-check edited Python/JavaScript, review the diff against the initial working files, and document actual results and unverified live-upstream behavior.

Execution proceeds inline in the current task; the user has already requested implementation.

## Validation result

- 115 selected offline tests passed with the repository Python 3.12 environment on Windows.
- Python syntax and dashboard inline JavaScript syntax checks passed; `git diff --check` passed.
- Browser QA used the actual dashboard and connection-test route with a mock BPS transport on a separate loopback server. Verified model options, disabled controls while running, success, access failure, request IDs, and clearing results on selection changes. No browser console errors.
- The temporary UI server and browser tab were closed. No real model request, user-app restart, commit, push or remote CI run was performed.
- CI is configured for Python 3.11 on Windows and Linux; those runner environments have not been executed locally.
