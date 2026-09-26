# Windows Launch Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return only the latest 100 dashboard requests and reduce measured startup latency without deleting history.

**Architecture:** Keep stored history and aggregate totals intact. Measure the existing launcher and proxy initialization in isolated processes before choosing the smallest startup-path change. Execute inline and preserve unrelated uncommitted work.

**Tech Stack:** Python, unittest, existing Windows VBS / pywebview launcher.

## Global Constraints
- Use the repository virtual environment and selected offline regression runner; no broad pytest discovery.
- Do not stop or restart the user's app, change production settings, or delete production history.
- Exclude mutants/ and preserve existing uncommitted changes.

## Task 1: Bound the dashboard request list
**Files:** app/constants.py, tests/test_excel_only.py.
**Interfaces:** DASHBOARD_RECENT_REQUEST_LIMIT controls the existing dashboard list slice, not persisted retention or aggregate usage.
- [x] Add and run `test_dashboard_limits_recent_requests_without_truncating_totals`: 101 synthetic events must produce 100 newest-first rows while totals retain 101 requests.
- [x] Set `DASHBOARD_RECENT_REQUEST_LIMIT = 100` and update its explanatory comment.
- [x] Verify with `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_excel_only`.

## Task 2: Measure and fix the startup bottleneck
**Files:** inspect app/windows_launcher.py, app/proxy.py, app/usage_tracking.py; change only the measured blocking path and focused regressions.
- [x] Measure launcher imports/readiness, isolated proxy initialization, history loading, and dashboard construction independently using `time.perf_counter`. Use read-only history snapshots, temporary runtime paths, disabled real HTTP, and no production startup callbacks.
- [x] Reproduce and remove slow negative-port probes and duplicate archive-key rebuilding with focused failing tests before the fixes.
- [x] Repeat the same measurements, run focused offline regressions and syntax/diff checks, and distinguish component timing from isolated native-window loading.

## Implemented startup changes
- `proxy_running()` first checks TCP readiness with a 0.2-second timeout. A listening service still requires the existing HTTP identity check and its full five-second response timeout; foreign services and unresponsive HTTP endpoints remain errors.
- `load_history()` reuses the archive keys already initialized on first load. Reloads with existing recent rows still rebuild the key index. The already-deduplicated recent rows initialize their snapshot directly.
- No VBS changes, history deletion, production configuration writes, or user-process restarts.

## Paired measurements (2026-09-25)
The four processes loaded the same snapshot of 83,082 archived rows plus 5,000 recent rows. Both normalized-history SHA-256 and aggregate-total SHA-256 matched across every run. Baseline files were saved from the working tree before this task's startup changes, preserving earlier unrelated work.

| Measured component | Before, run 1 / run 2 (seconds) | After, run 1 / run 2 (seconds) |
| --- | --- | --- |
| Recent-history loading | 1.7510 / 1.5477 | 0.3401 / 0.1866 |
| Proxy-module initialization | 6.5225 / 6.2876 | 5.0458 / 4.2914 |
| Closed-loopback-port probe | 2.0450 / 2.0401 | 0.2180 / 0.2161 |
| First dashboard payload | 1.9581 / 2.0510 | 2.0476 / 0.9942 |

These are component measurements, not a claim about complete VBS-to-visible-window latency. Launcher import took 0.112 seconds, webview import 0.052 seconds, and native WinForms backend import 0.454 seconds in separate probes.

## Native VBS smoke verification
The actual `启动.vbs` was launched with a copied history database/log, fresh temporary config/cache/WebView2 storage, and isolated loopback port 59380. A temporary `sitecustomize.py` redirected the hardcoded server port and Codex config paths, disabled session discovery and upstream HTTP, and kept the native window hidden. No production app was stopped or restarted.

- VBS invocation to native WebView2 `/ui` page loaded: **6.476 seconds**.
- The launcher checked HTTP 200 before creating the window; the loaded native window reported the exact isolated `/ui` URL.
- Closing the hidden window stopped its proxy normally. Both owned processes exited, the PID file was removed, and temporary runtime data was cleaned up.
- This measures a hidden native window with isolated settings, not the visible window or the user's live session-discovery/config-restoration workload.

## Final verification
- Focused startup, history, Excel-only, usage timing, model identity, and prompt archive regressions: **44 passed**.
- Full selected offline suite: **260 tests, 258 passed, 2 existing homepage assertion failures**. Both failures also reproduce with the saved pre-change startup modules: `test_home_has_navigation_but_no_request_table` and `test_home_replaces_subscription_quota_with_api_costs`. The existing homepage is outside this task's edit scope.
- Syntax checks passed for all six changed Python source/test files. Scoped `git diff --check` passed; only Git line-ending conversion warnings appeared.
- Existing uncommitted changes were preserved, and no commit or push was made.
