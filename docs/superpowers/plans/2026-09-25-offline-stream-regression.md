# Offline Streaming Regression Implementation Plan

**Goal:** Run deterministic offline fixtures through production SSE translators and report actionable mismatches.

**Architecture:** Synthetic wire bytes feed `format_translation.iter_sse_messages` through the four production translator classes. An independent downstream event observer checks exact text, tool identity/arguments, block lifecycles and terminal semantics. No server startup, configuration loading, credentials or external requests.

**Tech Stack:** Repository virtualenv Python, standard library, existing production imports.

## Constraints
- Preserve the pre-existing worktree changes; exclude `mutants/`.
- No new dependency, user configuration edits, copied production conversion logic or real request traces.
- Network byte chunks, SSE events and semantic deltas are separate fixture layers.
- All report content is synthetic; HTML escapes input, output, differences and exceptions.

## Implementation and verification
1. Add `tools/verify-streaming-protocol.py` with synthetic fixtures, fresh translator instances, independent observations and per-case exception isolation. Verify the twelve requested scenario families across applicable bridge paths.
2. Add fixed-seed 30-plan fragmentation checks, deterministic interleaving, genuine request-mapper tool-result replay, and large-payload exact comparisons. Verify unique fragmentation plans and nonzero failures.
3. Run unchanged production code first. Keep reproducible cases and baseline failure evidence, then make only demonstrated fixes in `bridge_streams.py`, `anthropic_stream.py` or the shared SSE parser. Re-run every scenario.
4. Generate self-contained HTML and JSON; inject a known wrong observation with `--inject-mismatch`, verify failure/exit code, and inspect escaping and report consistency.
5. Add `docs/offline-stream-regression.md` with exact entrypoint data flow, commands, actual findings and excluded integration boundaries. Syntax-check changed Python, review diff, compare preserved user files, and remove owned scratch artifacts.

## Acceptance
- All supported scenario cases pass or report an explicit failure; unsupported targets state evidence and never count as passes.
- At least 30 reproducible network fragmentations preserve identical event semantics.
- Interrupted streams never appear normally completed.
- Failure injection produces a failed case and exit code 1 without changing production code.
- Reports contain counts, names, outcomes, duration, reasons, inputs, expected/actual and escaped differences.
- Report files are inspected, not merely generated.
