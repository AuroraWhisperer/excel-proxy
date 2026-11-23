# BPS Request Content Parity Implementation Plan

Implementation proceeds in parallel with disjoint file ownership, followed by public HTTP integration and the repository's isolated offline regressions.

**Goal:** Close the five remaining request-content differences identified in the previous reply against bps-local-public `203c716ccd1cad0452b5dc7535c46e6531e73af0`.

**Architecture:** Extend the existing Python request, input and client-configuration modules. Use the reference's default protocol/catalog, preserve submitted history, rebuild client tool history within each request, normalize supported content before attachments, and let Codex use its own model instructions. Keep existing desktop, authentication, image-generation and admission components.

**Tech Stack:** Python 3.12, httpx, websockets, FastAPI, unittest, Windows.

## Global Constraints

- Work in `D:\Work\excel-proxy` and preserve all existing edits.
- Use `.venv/Scripts/python.exe -B tools/run-offline-tests.py`; no broad pytest discovery.
- No real credentials, runtime cache reads, upstream generations or app restart.
- The user has authorized direct implementation and parallel work; no execution-choice approval is needed.
- Agents own disjoint source files; the root owns public HTTP integration, runner registration and comparison documentation.
- Preserve explicitly supplied caller instructions and model choice. Do not delete stored user history or unrelated configuration.

## Task 1: Protocol and Request Assembly

**Files:** `app/excel_tool_catalog.py`, `app/excel_upstream.py`, focused tests in `tests/test_bps_request_alignment.py` and existing request/catalog tests.

**Interfaces:** Keep `prepare_responses_body(source, *, tools_version_id=None, cache_scope="local") -> dict` and existing catalog helper signatures. History translation continues through `translate_input_items`.

- [x] Compare `request.go`, `catalog.go` and `sanitize.go`; implement one protocol message, reference schema descriptions, serial tool guidance, FUNCTION JSON envelopes and CUSTOM raw input markers.
- [x] Append structured-output instructions to the protocol message; remove the extra reminder, appended-catalog escape and fallback session cache-key aliases from generation.
- [x] Test exact representative protocol text/catalog fields, instruction placement, explicit key whitespace, hosted-tool filtering and unchanged explicit model/effort:

```python
body = prepare_responses_body({"model": "gpt-6-astra", "input": "hello"})
assert body["model"] == "gpt-6-astra"
assert body["reasoning_effort"] == "medium"
assert "prompt_cache_key" not in body
assert len(body["input"]) == 2
```

- [x] Run the new module and focused existing request tests through the isolated runner.

## Task 2: Input, History and Content

**Files:** `app/excel_input.py`, `app/excel_images.py`, `app/excel_tool_transport.py`, narrowly required cache code, `tests/test_bps_history_alignment.py`, existing input/image/replay tests.

**Interfaces:** Keep `translate_input_items(raw_input, allowed_tools=None, *, tool_specs=None) -> list`; raise `ValueError` for invalid requests. Tool response conversion must no longer depend on persistent native-call writes for the default generation path.

- [x] Reject invalid input and non-object array entries; preserve compaction history, attribution and tool result text according to `tools.go` instead of silently pruning or rewriting it.
- [x] Rebuild tool calls from supplied complete history with reference envelopes and IDs; reject orphan tool results. Stop cross-request native-cache lookup and writes in this path without deleting user cache files.
- [x] Normalize supported string/image content using `content.go`, including tool results, and validate image URLs/detail before attachment side effects. Keep existing explicit file-ID and inline-upload capabilities where they require the local attachment path.
- [x] Verify missing/invalid input and orphan results raise before dispatch; complete repeated tool history gives the same request regardless of stored native cache. Example boundary assertion:

```python
with self.assertRaises(ValueError):
    translate_input_items([{"type": "function_call_output", "call_id": "missing", "output": "done"}])
```

- [x] Run focused input, history and image regressions through the isolated runner.

## Task 3: Structured Output and Codex Configuration

**Files:** `app/structured_output.py`, `app/codex_config.py`, `tests/test_structured_output.py`, `tests/test_codex_config.py`.

**Interfaces:** Preserve `request_format(body)`, `instructions(fmt)` and validation; use existing configuration service install/migration methods.

- [x] Reject format objects without a valid type, use reference JSON-value instructions, preserve object-only validation for json_object and schema validation for json_schema.
- [x] Stop installing project-owned model catalogs/base instructions, verbosity and truncation overrides. Migrate only the project's owned catalog references and select supported bare upstream model names; preserve user-owned unrelated configuration and backups.
- [x] Verify valid array JSON and malformed format handling:

```python
fmt = request_format({"text": {"format": {"type": "json_schema", "name": "items", "schema": {"type": "array"}}}})
assert "exactly one JSON value" in instructions(fmt)
with self.assertRaises(ValueError):
    request_format({"text": {"format": {"schema": {"type": "array"}}}})
```

- [x] Test fresh configuration, migration, custom settings preservation and repeated application in temporary directories; run focused modules.

## Task 4: Integration and Verification

**Files:** `app/proxy.py` and related boundary modules only when upstream preparation requires it; `tools/run-offline-tests.py`, `docs/参考项目与改进.md`, `docs/开发说明.md` and affected API docs.

- [x] Audit the public HTTP path for earlier sanitization, attachment uploads or compatibility mutations that undo the new behavior; extend the owning boundary only where required.
- [x] Register new focused suites and verify public HTTP 400/no-dispatch cases, JSON/SSE equivalence, complete tool round trips and explicit Astra preservation with mocked upstreams or loopback WebSockets.
- [x] Update old tests where their asserted behavior has intentionally changed; retain unrelated validation, ownership and lifetime checks.
- [x] Run the complete selected offline suite, inspect failures, repair actual regressions and run `git diff --check`.
- [x] Update comparison docs around the resulting implementation and record remaining differences outside these five categories, without claiming a real-model fingerprint result.

## Final Verification

- The complete selected offline runner passed **776 tests in 97.597 seconds** on 2026-11-23: `.venv/Scripts/python.exe -B tools/run-offline-tests.py`.
- Request fixtures verify the reference protocol/catalog, explicit Astra selection, cache-key escaping, complete history and custom transport. Public HTTP tests cover JSON/SSE behavior and rejection before attachments or generation.
- Additional review found and fixed tool/args envelope aliases and conflicting fields, plus JSON-schema prompt key order and HTML/Unicode escaping. New failing fixtures were verified before each fix.
- Configuration tests use temporary directories and cover install, repair, owned-reference migration, custom settings, backups, shutdown and idempotence. Runtime user configuration was not read or changed.
- The timeout regression now triggers a real timeout after stream ownership begins, preserving close, accounting and lock-release assertions without a short wall-clock race.
- `git diff --check` passes. No live upstream model request or fingerprint retest was made; the running user application was not restarted.
