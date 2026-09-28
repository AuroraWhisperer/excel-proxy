"""Request context shared by request orchestration and stream lifecycle."""

from dataclasses import dataclass


@dataclass
class UpstreamRequestPlan:
    request_id: str
    upstream_url: str
    headers: dict
    body: dict
    usage_event: dict | None
    requested_model: str | None
    resolved_model: str | None
    source_body: dict | None = None
    replay_subagent: str | None = None
    trace_context: dict | None = None
    request_affinity: str | None = None
