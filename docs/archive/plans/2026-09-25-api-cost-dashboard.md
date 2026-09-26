# API Cost Dashboard Implementation Plan

**Goal:** Continue the interrupted account-quota replacement with a monthly API-equivalent USD estimate based on recorded Excel usage.
**Architecture:** Reuse dashboard event snapshots, usage normalization, and the existing model reference pricing table. Add a clearly labelled estimate to the existing dashboard response; preserve unrelated configuration, history, and stream work.
**Tech Stack:** Python/FastAPI, existing HTML/CSS/JavaScript, isolated unittest contracts.

## Constraints
- Reference prices are not a verified official bill. Unknown prices and missing usage remain explicitly incomplete.
- Cached input is a subset of input; cache writes and reasoning are not counted twice.
- Keep the incumbent dark/orange dashboard style and all other home controls.
- Do not restart the user's app or modify runtime/account configuration.

## Steps
- [ ] Reproduce existing cost and page test failures; inspect the authoritative event and price logic.
- [ ] Add the monthly API-cost payload and focused tests for cache, missing data, models, and endpoint integration.
- [ ] Replace only the quota panel with total, component breakdown, and model rates; update related documentation.
- [ ] Run focused and full selected offline regressions, syntax checks, isolated desktop/mobile UI checks, and diff review.
