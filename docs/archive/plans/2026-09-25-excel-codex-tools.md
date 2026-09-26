# Codex Client Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use the user's supplied full Codex base instructions for Excel-routed models, remove proxy-authored Excel identity language, and preserve tool execution and continued work.

**Architecture:** Keep the existing Basis Points authentication and tool relay. Replace the proxy's identity wording with client-tool protocol instructions. Preserve streaming/non-streaming tool conversion and native call replay.

**Tech Stack:** Python, FastAPI, httpx, unittest.

## Global Constraints

- Use the repo-local virtualenv for all Python tests and tooling.
- Use syntax checks or targeted manual verification for changes.
- Do not include `mutants/` in normal searches or manual edits.
- Keep caller instructions, developer messages, tools, tool outputs, and encrypted reasoning.
- Do not claim to remove server-injected prompts or provide image tools absent from the client.

### Task 1: Preserve the working tool protocol with neutral identity wording

**Files:** `excel_upstream.py`, `test_excel_upstream.py`, `docs/excel-codex-tools.md`.

**Interfaces:** Existing `prepare_responses_body(source, *, tools_version_id=None)`, `extract_native_client_tool_call(response, source)`, and both proxy response paths remain intact.

- [x] Remove the incorrectly scoped Direct/chat-only additions, restoring caller instructions and tool replay/conversion.
- [x] Change proxy-authored identity wording to the external Codex client; retain the required `run_officejs` transport schema. Explicitly continue after tool results and use catalog-provided image tools when available.
- [x] Add `test_client_tools_continue_across_shell_patch_and_image_calls`: replay shell, custom patch, and namespaced image-tool results, checking unchanged task/turn identity, incrementing iterations, encrypted reasoning, caller instructions, and stable history prefixes.
- [x] Run focused tool/history/stream checks and syntax checks with the repository virtualenv and isolated runtime directories.
- [x] Document the final behavior and remaining server-side uncertainty.

Execution stays in the current session. No commit or running-service restart is requested.

## Validation results

- 44 focused tool, history, request, and stream checks passed; syntax and diff checks passed.
- Live Basis Points check using the existing session: three requests returned HTTP 200. The model invoked `read_note`, then `save_svg`, then replied `DONE` after receiving both results.
- Both client tools executed against an automatically cleaned temporary directory. The SVG was parsed as XML and checked for the unique label read from the note and a circle element.
- This verifies real upstream tool continuation and SVG creation, not the Codex desktop image-generation plugin or the absence of server-side instructions.

### Task 2: Use the supplied Codex base prompt in the model catalog

**Files:** `prompts/codex-excel.md`, `proxy_client_config.py`, `test_proxy_client_config.py`, `docs/excel-codex-tools.md`.

**Interfaces:** `_build_codex_model_catalog_payload()` supplies `base_instructions` for each routed model. `prepare_responses_body()` preserves caller `instructions` in a developer message.

- [x] Decode the supplied attachment's JSON string escapes once into a UTF-8 Markdown resource, preserving its complete content.
- [x] Select this resource as `base_instructions` for Excel-routed model entries, including remapped aliases. Keep other model entries unchanged and continue to preserve incoming caller instructions.
- [x] Verify that every Excel entry has the full prompt, non-Excel entries retain their existing value, and a catalog-derived Responses request carries the complete prompt unchanged through tool replay.
- [x] Document that the reference bridge also adds caller instructions and a tool protocol rather than removing the BPS server prefix. Record the active local configuration and any activation step needed.

Task 2 validation: 49 targeted checks passed. The full supplied 21,259-character prompt also passed a live three-request BPS tool cycle (read note, write SVG, finish); every request returned HTTP 200. The current primary Codex configuration still uses the native `openai` provider without a GHCP model catalog, so activation requires loading the regenerated GHCP catalog in a new Codex session.

### Task 3: Preserve long-running tool and context continuity

**Files:** `excel_upstream.py`, `proxy.py`, `format_translation.py`, `test_excel_continuity.py`, `docs/excel-codex-tools.md`.

**Interfaces:** Keep `prepare_responses_body`, the Excel stream transform, and `/responses/compact`. Add disk-backed replay behind the existing native-call cache. Compaction must round-trip through `responses_to_compaction_response` and `sanitize_input`.

- [x] Reproduce missing terminal output, malformed HTTP tails after completion, premature EOF, the 512-call replay limit, and the compact endpoint's incorrect response shape with focused unittest checks.
- [x] Accumulate `response.output_item.done` items and merge them only after a real `response.completed`; return immediately after a terminal event. Reject unfinished streams and untranslatable native calls before exposing executable client calls.
- [x] Force Excel summarization to non-streaming without executable tools, reject missing/incomplete summaries, return a compaction item, and expand the summary on replay while retaining the current task.
- [x] Persist exact native calls to `excel-native-calls.sqlite3` in the state directory, with 60 days of idle retention, batched reads, and the existing 512-entry memory bound. Remove volatile client metadata from turn fingerprints while preserving the latest user's explicit turn ID.
- [x] Run a synthetic history of 600 tool calls, clear the memory cache and start a separate Python process to verify disk replay; exercise the actual HTTP routes through compaction and subsequent tool work with a simulated upstream.
- [ ] User-run live long-task check: the agent's real-account test command was rejected by automatic approval with `blocked by policy`; the user chose to run the real-account test themselves. Do not bypass the rejection.
- [x] Record verified behavior and remaining boundaries. Do not implement endless automatic continuation after a genuine final answer, user cancellation, or an upstream denial.

Core assertions:

```python
assert replayed_native_calls == original_native_calls  # even after cache eviction
assert decode_fake_compaction(compacted["output"][0]["encrypted_content"]) == summary
assert "run_officejs" not in downstream_stream
assert completed_event_count == 1  # never synthesize success from EOF
```

Run the focused unittest suite with `.venv/Scripts/python.exe`, setting `GHCP_CONFIG_DIR`, `GHCP_STATE_DIR`, and `GHCP_CACHE_DIR` to an auto-cleaned temporary directory before imports. Keep the user's running proxy untouched.

Task 3 validation: 69 focused tests passed, including four shared compaction regression checks. Syntax and diff checks passed. The account source remains the saved Excel session. No live service was restarted and the current Codex login/configuration was not changed.
