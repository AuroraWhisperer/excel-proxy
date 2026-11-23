# BPS Call Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the default Excel generation call with bps-local-public commit `203c716ccd1cad0452b5dc7535c46e6531e73af0`, while preserving the application's existing desktop and client-tool features.

**Architecture:** Retain the existing request translator, account admission gate, and WebSocket transport. Assign server-owned task/turn identity when admission grants an actual slot, preserve the original response model, and align the default handshake and request parameters. The user explicitly requested parallel implementation; agents own disjoint files and the root integrates and verifies their changes.

**Tech Stack:** Python 3.12, httpx, websockets, FastAPI, unittest, Windows.

## Global Constraints

- Work in `D:\Work\excel-proxy`; preserve all existing staged and unstaged changes.
- Use `.venv/Scripts/python.exe -B tools/run-offline-tests.py`; do not use broad pytest discovery.
- Exclude `mutants/` from normal searches and edits.
- Do not restart the user's app, read live credentials, or generate real upstream traffic for these checks.
- Keep ARC disabled, matching the reference default; preserve image and tool support.
- Use the existing service-state lock and a persistent task namespace; do not rotate task identity when a token refreshes.
- No commits are planned because this working tree also contains unrelated ongoing changes.

## Task 1: Handshake parity (handshake agent)

**Files:** `app/excel_session.py`, `app/excel_websocket.py`, `tests/test_excel_websocket.py`.

**Interface:** Keep `ExcelWebSocketTransport.handle_async_request(request)` and the existing stream ownership unchanged.

- [x] Add loopback handshake assertions and verify the actual emitted headers:

```python
self.assertEqual(handshake.headers["User-Agent"], "Mozilla/5.0")
self.assertEqual(handshake.headers["Accept"], "*/*")
self.assertEqual(profile["X-Openai-Internal-Basispoints-Browser-Name"], "unknown")
```

- [x] Set the default Excel client profile, browser name, platform, origin, User-Agent and Accept to the reference values; compare extension negotiation as well.
- [x] Run `.venv/Scripts/python.exe -B tools/run-offline-tests.py test_excel_websocket`.

## Task 2: Task identity and request parameters (root)

**Files:** `app/excel_task_identity.py` (new), `app/rate_limiting.py`, `app/excel_upstream.py`, `app/proxy.py`, `app/responses_stream.py`, `tests/test_generation_admission.py`, `tests/test_excel_upstream.py`, `tests/test_excel_task_identity.py` (new), and the offline runner.

**Interfaces:** `GenerationAdmission.slot(account)` yields a one-based slot number. `slot_task_id(account, slot)` returns a persistent 64-character HMAC identity. `throttled_client_send` receives the prepared dictionary through `request.extensions["excel_body"]`, stamps identity after admission, and rebuilds the request with the same timeout/extensions. `prepare_responses_body` accepts a `cache_scope` keyword for the reference cache hash.

- [x] Add regressions for repeated identical requests, model switches, simultaneous slots, account isolation, token renewal, persistent reload, and connect-only retry identity.
- [x] Allocate the lowest free slot and release it with the upstream stream. Derive tasks using:

```python
message = f"bps-account-slot-v1\0{account}\0{slot}".encode()
task_id = hmac.new(persistent_key.encode(), message, hashlib.sha256).hexdigest()
turn_id = secrets.token_hex(32)
```

- [x] Generate a new turn once per dispatched generation, including corrective generations; preserve it across pre-send connection retries. Ignore client-supplied task and turn identifiers.
- [x] Use the reference 200000 upstream compaction threshold, hashed cache key, and server-owned metadata. Keep local Codex compaction settings separate from the upstream fallback.

```python
encoded = json.dumps([cache_scope, cache_key], ensure_ascii=False, separators=(",", ":"))
cache_key = "bps-" + hashlib.sha256(encoded.encode()).hexdigest()[:32]
```

- [x] Run the new identity and admission tests, then request/stream/continuity regressions.

## Task 3: Model selection and returned identity (audit agents)

**Files:** `app/excel_models.py`, `tests/test_module_boundaries.py` (auth agent); `app/excel_responses.py`, `app/excel_tool_transport.py`, `tests/test_excel_response_processor.py`, `tests/test_usage_model_identity.py` (response agent).

**Interfaces:** Keep alias-to-upstream mapping and supported public model names. Preserve an upstream response model and use the alias only when the upstream omits it. Add `extensions={"excel_body": body}` to response processor request builders for Task 2 integration.

- [x] Prove that a process-wide model override can replace explicit Astra; remove that override and verify explicit selection remains authoritative.
- [x] Prove that a Luna model returned for an Astra request can be hidden by response conversion; preserve the returned field through JSON, streamed tools, usage and traces:

```python
translated_payload.setdefault("model", excel_model_id)
```

- [x] Report other differences in authentication, reasoning, prompts, tools, optional ARC and repair behavior, distinguishing observed implementation differences from unproven server effects.
- [x] Run the model catalog and response processor regressions, then root runs the selected full offline regression suite once all changes are integrated.

## Review and delivery

- [x] Check all changed call sites and regression outcomes; ensure admission does not stamp image requests or duplicate generation frames.
- [x] Document the final aligned behavior and meaningful retained differences in the existing reference-project documentation.
- [x] Report implementation/test results without claiming that offline tests establish which model BPS actually ran.

## Final verification and audit

- Rejected `item_reference` before attachment uploads or generation instead of silently discarding history.
- Moved approved outbound body dumps after admission stamps task/turn; queue rejection creates no outbound dump. Final usage/trace includes the dispatched identity, upstream model and reported response model. Corrective generations record their own identity.
- Verified actual default settings in a separate virtualenv subprocess with test-only overrides removed: 4 slots, 64 pending, 30 seconds, rate disabled.
- Final full selected runner: **719 tests passed**, 61.218 seconds. The first integrated run found only three outdated expectations for renewal turn IDs and per-result iteration; corrected them while retaining the account, model, history and retry checks.
- `git diff --check` passed. No app restart or real upstream generation was performed.
- Retained input-side differences: JSON tool catalog, extra protocol reminder, native-plan/parallel-call guidance, conditional compaction pruning and notification filtering, tool-result/attribution normalization, broader raw transport, schema validation and one corrective generation.
- Retained transport/state differences: nonstream disconnect monitoring after admission, optional global start limiting, rejection handling, connect retry classifications and missing task-key recovery. These are documented in `docs/参考项目与改进.md`; default call alignment does not mean byte-identical prompts or complete transport equivalence.

## Follow-up audit and transport repair

- [x] Confirm additional input validation, content normalization, tool replay, structured output, cache-key and Codex configuration differences using source inspection and isolated synthetic requests; record their trigger conditions in the comparison document.
- [x] Reproduce HTTPX NO_PROXY routing around the mounted WebSocket transport, then give the configured WebSocket origin priority without changing ordinary HTTP(S) proxy or TLS behavior. Preserve transport closure and exact origin matching, including default ports.
- [x] Verify the production client routing path and a real loopback WebSocket handshake/create frame. New checks fail before the repair; the two focused modules pass all 27 tests afterward.
- [x] Run the complete selected offline runner after integration: **725 tests passed**, 114.667 seconds. `git diff --check` passed. No real upstream request or user-app restart was performed.
