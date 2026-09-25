"""
Static configuration data, constants, and pricing tables for ghcp_proxy.

Extracted from proxy.py to keep the main module focused on runtime logic.
This file contains only module-level constants, dicts, and string data —
no functions or classes.
"""

import os

from app_paths import user_cache_dir, user_config_dir, user_state_dir

# ─── Constants ────────────────────────────────────────────────────────────────

UPSTREAM_REQUESTS_PER_WINDOW = 5
UPSTREAM_REQUEST_WINDOW_SECONDS = 1.0
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300

CONFIG_DIR        = user_config_dir()
TOKEN_DIR         = user_state_dir()
CACHE_DIR         = user_cache_dir()
CLIENT_PROXY_SETTINGS_FILE = os.path.join(CONFIG_DIR, "client-proxy.json")
USAGE_LOG_FILE    = os.path.join(TOKEN_DIR, "usage-log.jsonl")
REQUEST_ERROR_LOG_FILE = os.path.join(TOKEN_DIR, "request-errors.log")
REQUEST_TRACE_LOG_FILE = os.path.join(TOKEN_DIR, "request-trace.jsonl")
REQUEST_PROMPT_ARCHIVE_DIR = os.path.join(TOKEN_DIR, "request-prompts")
PROXY_PID_FILE = os.path.join(TOKEN_DIR, "ghcp-proxy.pid")
PROXY_STDOUT_LOG_FILE = os.path.join(TOKEN_DIR, "ghcp-proxy.stdout.log")
PROXY_STDERR_LOG_FILE = os.path.join(TOKEN_DIR, "ghcp-proxy.stderr.log")
PROXY_BASE_URL    = "http://127.0.0.1:8000"
CODEX_PROXY_BASE_URL = f"{PROXY_BASE_URL}/v1"
DASHBOARD_FILE    = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
SQLITE_CACHE_FILE = os.path.join(
    os.path.expanduser(os.environ.get("GHCP_CACHE_DB_PATH", os.path.join(CACHE_DIR, ".ghcp_proxy-cache-v2.sqlite3")))
)
CODEX_CONFIG_DIR    = os.path.expanduser("~/.codex")
CODEX_PRIMARY_CONFIG_FILE = os.path.join(CODEX_CONFIG_DIR, "config.toml")
CODEX_MANAGED_CONFIG_FILE = os.path.join(CODEX_CONFIG_DIR, "managed_config.toml")
CODEX_PROXY_MODEL_CATALOG_FILE = os.path.join(CODEX_CONFIG_DIR, "ghcp-proxy-models.json")
# Codex presents the usable window at 95% of this raw prompt limit; 272k
# therefore reports as the expected ~258k before auto compaction.
CODEX_PROXY_MODEL_CONTEXT_WINDOW = 272000
CODEX_PROXY_MODEL_AUTO_COMPACT_TOKEN_LIMIT = 240000
CODEX_PROXY_CONFIG = """\
model_provider = "custom"
model = "gpt-5.6-sol-excel"
approvals_reviewer = "user"

[model_providers.custom]
name = "Excel"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
"""
DETAILED_REQUEST_HISTORY_LIMIT = 5000
# Cap on the number of detailed request rows serialized into the dashboard
# bulk payload. The in-memory deque still holds DETAILED_REQUEST_HISTORY_LIMIT
# events for aggregations (cost summary, daily history, sessions) and for
# lazy /api/request-prompt lookups, but the dashboard table only paginates
# 100 per page; shipping all 5000 events on every refresh sent megabytes of
# JSON the UI never rendered. 500 covers ~5 pages of history without
# capping common debugging workflows.
DASHBOARD_RECENT_REQUEST_LIMIT = 500
# Rolling retention for the request-trace log. Appends past this count are
# compacted down to the most recent N rows via a temp-file rewrite (same
# pattern as the usage log). See proxy._enforce_trace_retention.
REQUEST_TRACE_HISTORY_LIMIT = 1000
# Trim only when we've drifted this far past the limit so the rewrite cost
# is amortized across many appends instead of firing on every line.
REQUEST_TRACE_RETENTION_SLACK = 64
# Per-field cap for body payloads captured in the trace log. Keeps a
# 1000-row file to a bounded size even when upstream system prompts are
# multi-megabyte. See proxy._trim_trace_field.
REQUEST_TRACE_BODY_MAX_BYTES = 8192
# Maximum characters of each prompt slot (system/user) captured alongside
# usage events and the request trace so the dashboard can surface a
# human-readable preview without retaining full upstream payloads.
REQUEST_PROMPT_PREVIEW_MAX_CHARS = 4000
# Maximum characters of the assistant's reasoning ("thinking") text retained on
# the finished usage event so dashboards can surface the first portion of the
# model's chain of thought without bloating per-request rows.
RESPONSE_REASONING_PREVIEW_MAX_CHARS = 1000
FAKE_COMPACTION_PREFIX = "ghcp_proxy_summary_v1:"
FAKE_COMPACTION_SUMMARY_LABEL = "[Compacted conversation summary]"
COMPACTION_SUMMARY_PROMPT = """Please create a detailed summary of the conversation so far. The history is being compacted so moving forward, all conversation history will be removed and you'll only have this summary to work from. Be sure to make note of the user's explicit requests, your actions, and any key technical details.

The summary should include the following parts:
1. <overview> - high-level summary of goals and approach
2. <history> - chronological analysis of the conversation
3. <work_done> - changes made, current state, and any issues encountered
4. <technical_details> - key concepts, decisions, and quirks discovered
5. <important_files> - files central to the work and why they matter
6. <next_steps> - pending tasks and planned actions
7. <checkpoint_title> - 2-6 word description of the main work done

---

## Section Guidelines

### Overview

Provide a concise summary (2-3 sentences) capturing the user's goals, intent, and expectations. Describe your overall approach and strategy for addressing their needs, and note any constraints or requirements that were established.
This section should give a reader immediate clarity on what this conversation is about and how you're tackling it.

### History

Capture the narrative arc of the conversation\u2014what was asked for, what was done, and how the work evolved. Structure this around the user's requests: each request becomes an entry with the actions you took nested underneath, in chronological order.
Note any major pivots or changes in direction, and include outcomes where relevant\u2014especially for debugging or when something didn't go as expected. Focus on meaningful actions, not granular details of every exchange.

### Work Done

Document the concrete work completed during this conversation. This section should enable someone to pick up exactly where you left off. Include:

- Files created, modified, or deleted
- Tasks completed and their outcomes
- What you were most recently working on
- Current state: what works, what doesn't, what's untested

### Technical Details

Capture the technical knowledge that would be painful to rediscover. Think of this as a knowledge base for your future self\u2014anything that took effort to learn belongs here. This includes:

- Key concepts and architectural decisions (with rationale)
- Issues encountered and how they were resolved
- Quirks, gotchas, or non-obvious behaviors
- Dependencies, versions, or environment details that matter
- Workarounds or constraints you discovered

Also make note of any questions that remain unanswered or assumptions that you aren't fully confident about.

### Important Files

List the files most central to the task, prioritizing those you've actively worked on over files you merely viewed. This isn't an exhaustive inventory\u2014it's a curated list of what matters most for continuing the work. For each file, include:

- The file name
- Why it's important to the project
- Summary of changes made (if any)
- Key line numbers or sections to reference

### Next Steps

If there's pending work, describe what you were actively working on when compaction occurred. List remaining tasks, outline your planned approach, and flag any blockers or open questions.
If you've finished all requested work, you can simply note that no next steps are needed.

### Checkpoint Title

Provide a concise 2-6 word title capturing the essence of what was accomplished in this work segment. This title will be used to identify this checkpoint when reviewing session history. Examples:
- "Implementing user authentication"
- "Fixing database connection bugs"
- "Refactoring payment module"
- "Adding unit tests for API"

---

## Example

Here is an example of the structure you should follow:
<example>
<overview>
[2-3 sentences describing the user's goals and your approach]
</overview>
<history>
1. The user asked to [request]
   - [action taken]
   - [action taken]
   - [outcome/result]

2. The user asked to [request]
   - [action taken]
   - [action taken]
   - [outcome/result]
</history>
<work_done>
Files updated:
- [file]: [what changed]

Work completed:
- [x] [Task]
- [x] [Task]
- [In progress] [Task in progress or incomplete]
</work_done>
<technical_details>
- [Key technical concept or decision]
- [Issue encountered and how it was resolved]
- [Non-obvious behavior or quirk discovered]
- [Unresolved question or uncertain area]
</technical_details>
<important_files>
- [file1]
   - [Why it matters]
   - [Changes made, if any]
   - [Key line numbers]
- [file2]
   - [Why it matters]
   - [Changes made, if any]
   - [Key line numbers]
</important_files>
<next_steps>
Remaining work:
- [Task]
- [Task]

Immediate next steps:
- [Action to take]
- [Action to take]
</next_steps>

<checkpoint_title>Concise 2-6 word description of this checkpoint</checkpoint_title>
</example>

---

Please write the summary now, following the structure and guidelines above. Be concise where possible, but don't sacrifice important context for brevity."""

# ─── Model pricing & SKU tables ──────────────────────────────────────────────
MODEL_PRICING = {
    "gpt-5.6-luna-excel": {
        "provider": "OpenAI Excel",
        "credit_unit_usd": 0.04,
        "input_per_million": 0.20,
        "cached_input_per_million": 0.02,
        "cache_write_per_million": 0.25,
        "output_per_million": 1.20,
        "long_context_threshold": 272_000,
        "long_context_input_per_million": 0.40,
        "long_context_cached_input_per_million": 0.04,
        "long_context_cache_write_per_million": 0.50,
        "long_context_output_per_million": 1.80,
    },
    "gpt-5.6-terra-excel": {
        "provider": "OpenAI Excel",
        "credit_unit_usd": 0.04,
        "input_per_million": 2.50,
        "cached_input_per_million": 0.25,
        "cache_write_per_million": 3.125,
        "output_per_million": 15.00,
        "long_context_threshold": 272_000,
        "long_context_input_per_million": 5.00,
        "long_context_cached_input_per_million": 0.50,
        "long_context_cache_write_per_million": 6.25,
        "long_context_output_per_million": 22.50,
    },
    "gpt-5.6-sol-excel": {
        "provider": "OpenAI Excel",
        "credit_unit_usd": 0.04,
        "input_per_million": 4.00,
        "cached_input_per_million": 0.40,
        "cache_write_per_million": 5.00,
        "output_per_million": 20.00,
    },
}

MODEL_PRICING_ALIASES = {
    "gpt-5.6 luna excel": "gpt-5.6-luna-excel",
    "gpt-5.6 sol excel": "gpt-5.6-sol-excel",
    "gpt-5.6 terra excel": "gpt-5.6-terra-excel",
}
