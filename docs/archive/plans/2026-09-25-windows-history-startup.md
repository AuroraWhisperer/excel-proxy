# Windows History Startup Implementation Plan

> **For agentic workers:** Execute inline in this task. Use checkbox steps and verify each deliverable.

**Goal:** Remove repeated failed optional imports from startup history loading without changing stored history.

**Architecture:** Resolve the optional native metadata reader once when usage_tracking loads; skip native backfill when unavailable. Reuse the reader in normalization and lifecycle refresh. Leave VBS, active processes, user data, and unrelated edits alone unless measurements prove another startup bottleneck.

**Tech Stack:** Python 3.12, unittest, PowerShell, existing Windows launcher.

## Global Constraints
- Use D:/Work/ghcp_proxy/.venv/Scripts/python.exe for tests and tooling.
- Run offline tests via tools/test-proxy-contracts.py; no broad pytest discovery.
- Do not restart the user's application or mutate production settings/history.
- Do not touch mutants/ or unrelated uncommitted changes.

## Task 1: Reproduce and establish baseline
**Files:** tests/test_usage_startup.py (existing incomplete work).
- [x] Run the existing three regressions and confirm their failure.
- [x] Measure 1,000 synthetic historical native rows using perf_counter. Baseline samples: 0.3834, 0.4106, 0.3929 seconds.

## Task 2: Resolve optional reader once
**Files:** app/usage_tracking.py; tests/test_usage_startup.py; tools/test-proxy-contracts.py.
**Interfaces:** `_native_turn_metadata_for_rollout` is an optional callable with `(path, turn_id)` arguments; `_normalize_recorded_usage_event` keeps its existing signature and return contract.
- [x] Replace repeated imports with a single optional module-level import:
```python
try:
    from codex_native_ingest import native_turn_metadata_for_rollout as _native_turn_metadata_for_rollout
except Exception:
    _native_turn_metadata_for_rollout = None
```
- [x] Guard normalization backfill on reader availability, preserving reader error handling and stored fields.
- [x] Return immediately from lifecycle refresh when the reader is unavailable.
- [x] Cover missing import, available reader, preserved metadata, reader failure, and repeated refresh. Register the test module in the offline runner.
- [x] Verify: ./.venv/Scripts/python.exe -B tools/test-proxy-contracts.py test_usage_startup test_usage_model_identity test_windows_launcher (18 tests passed).

## Task 3: Verify startup impact
**Files:** inspect app/windows_launcher.py and app/proxy.py startup only; do not change unless needed.
- [x] Rerun the identical synthetic benchmark. Updated samples: 0.0058, 0.0058, 0.0060 seconds.
- [x] Measure isolated proxy-module initialization with copied history. Use a read-only SQLite backup and copied JSONL history, temporary config/state/cache directories, and blocked real HTTP transports.
- [x] Run the selected offline suite and review git diff --check. The full runner executed 225 tests: 223 passed and 2 unrelated dashboard-page assertions failed. Targeted startup regressions passed; changed Python files passed syntax checks and scoped git diff --check.
- [x] Report measured component/process times separately from unverified native window display time.

## Measurements (2026-09-25)

Two alternating before/after subprocess pairs loaded the same snapshot containing 82,884 archived rows and 5,000 recent rows. The before variant used the original `usage_tracking.py` from HEAD; both variants used the current surrounding application code.

| Component | Before (seconds) | After (seconds) |
| --- | --- | --- |
| Proxy-module initialization, run 1 | 12.8152 | 5.2261 |
| Proxy-module initialization, run 2 | 13.6601 | 5.1624 |
| Archived history, run 1 | 10.4138 | 3.1067 |
| Archived history, run 2 | 11.2571 | 3.0757 |
| Recent history, run 1 | 1.9761 | 1.6491 |
| Recent history, run 2 | 1.9594 | 1.6501 |

All four normalized-history SHA-256 values matched. Average proxy initialization dropped by approximately 61%. Timings exclude the subsequent verification hash calculation and do not claim to measure VBS-to-visible-window latency. Startup/shutdown callbacks and the native window were not launched; the user's existing application was not restarted.

## Existing regression failures

The dashboard tests expect `/api/dashboard` and `id="cost-heading"` in the current homepage, which does not contain them:
- `test_dashboard_pages.DashboardPagesTests.test_home_has_navigation_but_no_request_table`
- `test_dashboard_pages.DashboardPagesTests.test_home_replaces_subscription_quota_with_api_costs`

Both failures reproduced with the original `usage_tracking.py` from HEAD using the isolated offline runner. They are independent of this startup patch; the homepage and its tests were not modified by this task. Other concurrent stream-related edits in `usage_tracking.py` were preserved.

One failed benchmark left `C:/Users/Tom/AppData/Local/Temp/ghcp-startup-benchmark-bcfinnqr`; automatic approval policy rejected its cleanup with `blocked by policy`. The directory was left in place. The subsequent successful benchmark used a separate repository-local temporary directory and cleaned it up normally.
