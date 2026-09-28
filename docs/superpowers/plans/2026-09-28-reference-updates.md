# Reference Update Compatibility Implementation Plan

**Goal:** Adopt applicable fixes from the two acknowledged projects without changing tool execution semantics.

**Architecture:** Extend the existing Excel history translator and tool catalog. Keep complete-batch validation, one correction attempt, native replay identities, and unchanged executable inputs. Add model aliases through the shared model catalog.

**Tech Stack:** Python, FastAPI, httpx mock transports, unittest, JSON Schema.

## Constraints and baseline

- Use only `origin` for normal Git operations; read reference repositories through GitHub HTTP APIs.
- Run Python through `.venv/Scripts/python.exe`; use the isolated offline runner.
- Do not restart the user's application, use live credentials, change Windows timezone, or migrate Codex conversations.
- Baseline: 647 offline tests passed before edits.
- Sub2API: compare `dcf523bf1da062f25cde3a129e3e129398f1f4ec` with `3dafea660a0a9c1a607c914a0391ddf916673811` (58 commits, including merges). Also inspect older tool catalog/image fixes that the earlier review did not adopt.
- Bridge: compare `8a277dfcdbb647d2ef4d714e31b6a98260a63a79` with `4d7354b2bd3f50e266a7d2b6f0debf92821594eb` (7 commits).

## 1. History compatibility

Files: `app/excel_input.py`, `tests/test_excel_request_compat.py`.

- [x] Add failing tests for attributed messages, agent messages, tool image references, and stable replay.
- [x] Normalize message attribution into a context-only text part. Preserve ordinary message roles, IDs, phases, content, and caller data. Lower `agent_message` to user-role context without granting system/developer authority.
- [x] Move non-inline tool image references into an adjacent image message, retaining call-ID labels, image order and text positions. Keep inline screenshots in tool results.
- [x] Verify both HTTP routes and streaming modes, image upload preflight, and unchanged tool payloads.

Representative cases:

```python
agent = {"type": "agent_message", "role": "system", "author": "/root/worker",
         "content": [{"type": "input_text", "text": "worker result"}]}
image_result = {"type": "function_call_output", "call_id": "call_picture",
                "output": [{"type": "input_image", "file_id": "file-existing"}]}
# agent -> message/user with attribution text; image_result -> labeled output + image message.
# Repeating translation must not add another label or change task/turn identity.
```

## 2. Tool catalog consistency and batch recovery

Files: `app/excel_tool_catalog.py`, `app/excel_upstream.py`, `tests/test_excel_tool_compatibility.py`, `tests/test_excel_raw_transport.py`.

- [x] Reproduce duplicate catalog entries whose descriptions differ and conflicting schemas that currently overwrite one another.
- [x] Retain the first compatible declaration once in both prompt and validator; normalize schema aliases for comparison, ignoring only description/discovery annotations. Reject changed execution contracts before sending upstream.
- [x] Verify namespaced tools, custom formats, strict/unknown constraints, schema enforcement, and `tool_choice=none`.
- [x] Verify existing mixed-batch recovery preserves the valid sibling, command bytes and replay IDs, and emits each successful tool exactly once in JSON and SSE modes.

```python
tools = [{"type": "function", "name": "shell", "parameters": {"type": "object"},
          "description": "current"},
         {"type": "function", "name": "shell", "inputSchema": {"type": "object"},
          "description": "older", "defer_loading": True}]
# One current catalog entry; changing the second schema or strict flag must fail.
```

## 3. Models, documentation, final verification

Files: `app/excel_models.py`, existing model/catalog tests, `docs/api.md`, `docs/参考项目与改进.md`.

- [x] Add `gpt-6-sol-excel` and `gpt-6-luna-excel`, routed to their respective upstream names with a conservative 272,000-token advertised window. Preserve existing models, defaults and limits.
- [x] Verify model listing, request routing, reasoning levels, Codex catalog refresh, and tool responses under the new aliases.
- [x] Document fixed reference versions, adopted changes, unchanged applicable implementations, and deferred features.
- [x] Run targeted regressions, then the complete offline runner, syntax parsing and `git diff --check`.

```powershell
.\.venv\Scripts\python.exe -B tools/run-offline-tests.py test_excel_request_compat test_excel_tool_compatibility test_excel_raw_transport test_module_boundaries test_excel_upstream test_codex_config test_excel_only
.\.venv\Scripts\python.exe -B tools/run-offline-tests.py
git diff --check
```

Execution stays in this session. No commit, push, runtime configuration update, or live-account request is part of this change.

## Verification results

- Baseline: 647 tests passed in 42.257 seconds.
- Reproductions: new history/catalog/model cases failed against the old implementation; the mixed-batch recovery case passed without changing its implementation.
- Final isolated offline suite: 663 tests passed in 49.035 seconds (16 new tests).
- Syntax: all 100 Python files under `app/` and `tests/` parsed successfully.
- Ruff fatal checks (`--isolated --select E9,F63,F7,F82`) passed for all modified Python files. The inherited broader Ruff configuration already reports 53 findings in these files at HEAD; no unrelated cleanup was applied. The new request validation deliberately uses ValueError to match the existing HTTP 400 boundary.
- `git diff --check` passed. Concurrent edits to dashboard assets, their tests, and the READMEs were left to their owner.
- No real Excel account, generated command execution, or application restart was used for verification.
