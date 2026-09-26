# First Output Latency Investigation Plan

> **Execution:** Inline in the current task; no delegation, commits, production restart, or changes to reasoning defaults.

**Goal:** Identify where first-output time is spent and make only evidence-backed latency changes.

**Architecture:** Measure the existing streaming endpoint with short synthetic prompts. Preserve the definition of first output (generated text, reasoning, or tool arguments; not heartbeat/lifecycle frames). Add narrowly scoped observability or fix buffering only when the measured path warrants it.

**Tech Stack:** Repository Python virtualenv, httpx, unittest, selected offline contract runner.

## Constraints
- Preserve the existing dirty working tree; exclude mutants/.
- Do not lower reasoning effort, change the selected model, remove cancellation safeguards, or replay user conversation history.
- Keep live probes bounded, identify them as diagnostics, and fully consume successful streams. Never print session credentials or prompt contents from user requests.
- Do not claim first-byte or lifecycle-event latency is first-token latency.

## Execution and checks
- [x] Baseline: `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_usage_timing test_excel_contracts`; 33 tests passed.
- [x] Live control: `/v1/models` returned HTTP 200. Four sequential short requests alternated medium/xhigh/xhigh/medium with a dedicated cache key. No automatic POST retries or user-history replay.
- [x] Compare production timing and live event timelines, including two direct-upstream probes with transport tracing.
- [x] Reproduce both unnecessary cache traversal and event-loop blocking as failing regressions; implement the two targeted fixes without changing pool/rate/default reasoning settings.
- [x] Run affected and complete selected offline contracts, compile edited Python files in memory, and run scoped `git diff --check`.

## Findings (2026-09-25, local Asia/Shanghai)

The first byte and lifecycle events are not generated output. Successful probes all completed normally, generated five output tokens, reported zero reasoning output tokens, and used 22,361 input tokens with more than 99% cached input. Two initial probes were rejected with HTTP 422 before generation; removing arbitrary diagnostic metadata produced the valid probes below. These rejected requests are excluded from latency comparisons.

| Route | Effort | Headers (s) | First generated output (s) | Total (s) |
| --- | --- | --- | --- | --- |
| Running local proxy, probe 1 | medium | 5.604 | 6.447 | 7.071 |
| Running local proxy, probe 2 | xhigh | 5.434 | 6.086 | 6.443 |
| Running local proxy, probe 3 | xhigh | 4.144 | 4.478 | 4.970 |
| Running local proxy, probe 4 | medium | 2.826 | 5.365 | 5.897 |
| Direct upstream, new connection | medium | 2.451 | 3.386 | 3.698 |
| Direct upstream, reused connection | medium | 9.970 | 10.734 | 11.294 |

- Direct-upstream requests read the existing saved session without persisting it, use the shell's HTTP(S) proxy environment, and retain certificate verification. They bypass the application proxy, not the machine's configured network proxy.
- On the reused connection, the POST body finished sending at 4 ms; response headers arrived at 9,970 ms. No TCP/TLS setup occurred for this request. This reproduces slow output without the local application's rate limiter, connection pool contention, session scan, or presentation transform. It does not distinguish network/proxy transit from server-side queueing or processing.
- New-connection TLS setup took approximately 802 ms. Connection reuse is already present in production; removing its concurrency limits is not supported by these results.
- These are a small, sequential diagnostic sample, not a production before/after benchmark or proof that reasoning effort never matters. Lowering xhigh did not reliably fix the short-prompt floor.

## Implemented changes

1. `app/excel_session_capture.py`: stop recursive traversal inside EBWebView profile cache directories once the fixed Local Storage/leveldb location is known. Preserve newest-first discovery and continue reading fresh session data on every forced refresh; no TTL or stale-token cache was introduced.
2. `app/proxy.py`: offload forced session refresh to `asyncio.to_thread`, so synchronous filesystem discovery does not pause unrelated streaming requests on the event loop. Refresh semantics, model, reasoning, pool limits, and cancellation behavior remain unchanged.
3. `tools/probe-first-output.py`: explicit `--live` opt-in, at most eight short requests, optional `--direct`, timing-only transport traces, and separate first-byte/first-generated-output results. No headers or upstream error bodies are printed; unsuccessful probes stop the run. Direct probes consume upstream quota but are not recorded by the local proxy's usage tracker.

### Paired local component measurement

Read-only alternating original/updated discovery returned identical session headers and the same seven databases in the same order. No production session or configuration was written.

- After the regression suite finished, full session-read samples were **688.8 / 532.1 / 635.1 ms before**, **323.1 / 241.7 / 237.5 ms after**: medians **635.1 -> 241.7 ms**, about **393 ms less local preflight**.
- A separate discovery-only run measured **328.1 -> 122.0 ms** medians. A run concurrent with the offline suite measured **1,269.6 -> 544.9 ms** for full reads; keep that contended run separate rather than presenting it as normal-load performance.
- The existing dashboard first-output clock starts after session discovery. These changes reduce client-visible preflight and cross-request event-loop stalls; they do not claim a corresponding reduction in the dashboard's existing first-output metric.

## Verification and remaining limits

- Both new performance regressions failed before implementation and pass afterward.
- Focused session, timing, HTTP contract, and probe tests: **46 passed**.
- Complete selected offline suite: **271 tests; 269 passed, 2 existing homepage assertion failures** (`test_home_has_navigation_but_no_request_table`, `test_home_replaces_subscription_quota_with_api_costs`), matching failures already documented in the launch-latency task. Unrelated UI assertions were not changed.
- Edited Python syntax and scoped diff whitespace checks passed.
- No live process was stopped or restarted; no commit or push was made. The application changes take effect on the user's next normal proxy restart, so no live end-to-end post-deployment speedup is claimed.

### Repeating the probes

These commands deliberately consume a small amount of model quota. Run from `D:\Work\ghcp_proxy` in PowerShell:

```powershell
.\.venv\Scripts\python.exe -B tools/probe-first-output.py --live
.\.venv\Scripts\python.exe -B tools/probe-first-output.py --live --direct --efforts medium medium
```

The direct comparison shares the normal network exit and account; distinguishing that network exit from upstream service latency requires a separate controlled comparison, not removing local safeguards or automatically changing reasoning effort.
