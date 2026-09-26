# Excel Summary and Request Compatibility Implementation Plan

> Execute inline in this task, preserving the existing uncommitted changes.

**Goal:** Restore real Excel reasoning summaries, support image requests through Excel attachments, and keep update logs compact.

**Architecture:** Keep the current Excel request mapper and SSE transport. Translate the summary option into the gateway's verified shape, retain encrypted reasoning without inventing display text, upload inline message images using the existing Excel session, and share one update-log formatter. Compute task and turn identity before replacing image data with attachment references.

**Tech Stack:** Repository virtualenv Python, unittest, httpx, FastAPI.

## Global Constraints

- Preserve pre-existing worktree changes and exclude `mutants/`.
- Use `.venv/Scripts/python.exe` on this Windows checkout.
- Isolate test configuration, state, and cache directories.
- Do not discard images, change the selected model, or restart the user's proxy.
- Do not print credentials or encrypted reasoning.

## Evidence

- The failed conversation's first request contains `input_image`; concurrent text requests succeeded.
- A minimal text request returned 200; the same request with an inline message image returned 422. This only established a wire-format incompatibility, not a lack of vision support.
- The user-provided reference, `Kaixxrua/excel-codex-bridge/src/excel_codex_bridge/images.py`, uploads message images to `/basispoints/api/attachments`, then references `openai_file_id`. Tool-result images remain inline.
- A live upload and image request both returned 200, and the model correctly read the supplied screenshot text.
- `reasoning={"effort":"medium","summary":"auto"}` returned real summary SSE events.
- Nested `summary="detailed"` returned 422. `effort="xhigh", summary="auto"` returned 200 with response summary mode `detailed`.
- The update log enumerated 1,357 dirty entries, mostly virtualenv files.

## Task 1: Reasoning summaries

**Files:** `excel_upstream.py`, `format_translation.py`, `test_excel_upstream.py`, `test_reasoning_translation.py`.

- [x] Update the wire-shape regression to expect:

```python
self.assertEqual(body["reasoning"], {"effort": "xhigh", "summary": "auto"})
```

- [x] Verify summary modes `auto`, `concise`, and `detailed` map to gateway `auto`, and absent/disabled summaries remain absent.
- [x] Change the encrypted-only reasoning regression to require `summary == []`, preserved `encrypted_content`, and no invented `content`.
- [x] Run these cases before implementation and confirm the missing summary forwarding / placeholder failures.
- [x] Forward the normalized effort with `summary="auto"` when a summary is requested. Remove the encrypted-only placeholder in the client normalizer and redacted Anthropic conversion.
- [x] Verify the existing Excel SSE transform preserves real summary events and the finished reasoning item.

## Task 2: Image upload compatibility

**Files:** `excel_images.py`, `excel_upstream.py`, `proxy.py`, `test_excel_upstream.py`, `test_excel_request_compat.py`, `test_excel_images.py`, `test_proxy_client_config.py`, `readme.md`.

- [x] Remove the blanket image rejection and advertise `input_modalities=["text", "image"]`, `vision=True`.
- [x] Add `ExcelImageUploads.rewrite(body, client, headers)` returning the rewritten body and reused cache keys. Use multipart field `file`, the existing session headers without JSON content headers, and the same upstream client.
- [x] Replace message `input_image.image_url=data:...` with `input_image.file_id`; preserve image detail, source body, and inline tool images.
- [x] Cache a bounded number of file IDs by account and image digest. Deduplicate simultaneous uploads and retry rejected cached references once with fresh uploads.
- [x] Report malformed image data or upload failures explicitly, preserving authentication/rate-limit statuses. Never silently omit pictures.
- [x] Verify both Responses routes, streaming and non-streaming, concurrent conversations, continuation, account isolation, and expired cached references.
- [x] Verify the production route reads the user's screenshot using the authenticated upstream.

## Task 3: Update logging

**Files:** `.gitignore`, `auto_update.py`, `proxy.py`.

- [x] Ignore `.venv/`.
- [x] Add `format_update_result_for_log(result)` that replaces the dirty path list with `dirty_count` in the log representation only.
- [x] Use the formatter in startup, periodic checks, and update application logs.
- [x] Manually pass the attached 1,357-entry result to the formatter; assert a short single line and unchanged original dirty entries.

## Verification

- [x] Run targeted unittest modules in isolated runtime directories, including Excel continuity and reasoning translation.
- [x] Syntax-check changed Python and run `git diff --check`.
- [x] Use the corrected mapper in a small authenticated text request and confirm real summary events survive the Excel stream transform.
- [x] Review the final diff and document image behavior and the proxy restart requirement.

## Results

- Latest targeted unittest run: 98 tests, 97 passed. The existing `test_local_model_is_merged_once` assertion omits `gpt-6-astra-excel`; the same failure was reproduced using the untouched pre-task source and test files. It is outside this repair. All 14 focused image-upload, route, and capability checks passed.
- Two simultaneous image conversations through the production ASGI route and authenticated upstream returned 200. They read the supplied screenshots as `Thinking process completed.` and `422`. A non-streaming continuation correctly counted 3 words in the first screenshot. There were exactly two attachment uploads across the three requests.
- Live corrected request and production Excel SSE transform: HTTP 200, 86 summary deltas (353 characters), one completed summary part, and preserved encrypted replay state.
- Some live responses requested with summaries still contained only encrypted reasoning. Those now remain empty instead of displaying invented text.
- The optional `stream_options.reasoning_summary_delivery` probe returned 422, so the change does not add that unsupported option.
- Attached update log: 83,388 characters reduced to 283; all 1,357 dirty entries remained available in the original result.
- Seven changed Python files passed syntax checks. `git diff --check` passed. `.venv/Scripts/python.exe` is now ignored by Git.
- The running proxy was not restarted. Restart it when ready to activate the changes. The initial text-only conclusion was corrected after inspecting and live-testing the reference project's attachment flow.
