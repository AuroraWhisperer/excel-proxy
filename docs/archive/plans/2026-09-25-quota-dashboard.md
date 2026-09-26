# Quota Dashboard Implementation Plan

**Goal:** Add explicitly labelled, read-only Codex account quota checking to the home dashboard, and move recent Excel requests to `/ui/requests`.

**Architecture:** Keep the existing FastAPI application and dark dashboard style. Reuse the reference monitor's Codex app-server initialize/account/read/account/rateLimits/read protocol, not its log scanning or cost estimates. Codex quota must never be described as Excel quota.

**Tech Stack:** Python standard library, FastAPI/httpx, existing inline browser JavaScript.

## Constraints
- No model calls, login/reset operations, credentials in responses, or new dependencies.
- Local-host and same-origin protection for quota refresh; bounded child lifetime, no shell, hidden Windows process.
- Do not change the user's running proxy, Codex configuration, or reference repository.
- Run tests only through `.venv/Scripts/python.exe -B tools/test-proxy-contracts.py`.

## Tasks
- [x] Split `app/static/dashboard.html` into home and `requests.html`, sharing `dashboard.css`; expose explicit page/style routes in `app/proxy.py`. Verify distinct page content and working navigation with `tests/test_dashboard_pages.py`.
- [x] Add `app/account_quota.py`: executable discovery, read-only JSON-RPC, sanitized rate-limit windows, and a 60-second in-memory cache. Add GET cached status and POST refresh endpoints. Verify protocol, cleanup, errors, missing/expired windows, and origin protection with `tests/test_account_quota.py`.
- [x] Add homepage quota states and manual detection button; retain existing request filtering/paging/details on the separate page. Update `readme.md` with the source distinction.
- [x] Run focused then complete offline contracts, syntax checks, isolated browser checks, and diff review.

## Verification results
- Full isolated offline contract suite: 171 tests passed.
- Python syntax and both inline JavaScript scripts pass; lifecycle checks verify initial polling, pagehide cleanup, and pageshow restoration without duplicate timers.
- Isolated browser fixture verified ready, missing, expired, and failed quota states; request navigation, pagination, filtering, and literal prompt detail rendering; no browser warnings or errors. Fixture balances are synthetic, not real account data.
- UI mechanical detector reported only type-hierarchy warnings caused by its inability to resolve the root-relative stylesheet route. Both stylesheet HTTP delivery and inherited heading hierarchy were verified in the browser.
- Real read-only CLI probe returned the safe non-ChatGPT-login error. Real quota retrieval remains dependent on a ChatGPT subscription login; no login or user configuration was changed.
- Broken-pipe cleanup regression reproduced first, then passed after the fix.
- Temporary browser tab and isolated fixture server were closed; the user's running application was not restarted.
