"""Usage event lifecycle, request identity, history loading and persistence."""

import inspect
import json
import os
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable
from uuid import uuid4

import httpx
from fastapi import Request

import codex_agent_compat
import usage_records
from constants import (
    TOKEN_DIR,
    USAGE_LOG_FILE,
    REQUEST_ERROR_LOG_FILE,
    DETAILED_REQUEST_HISTORY_LIMIT,
    RESPONSE_REASONING_PREVIEW_MAX_CHARS,
)
from util import (
    _json_default,
    utc_now,
    utc_now_iso,
)
from usage_metrics import (
    normalize_usage_payload,
    _normalize_model_name,
    _usage_event_source,
    _usage_event_estimated_cost,
    _server_request_chain_key,
    _extract_payload_usage,
    _native_usage_event_dedupe_key,
    deduplicate_usage_events,
)
from usage_storage import UsageArchiveStore

from usage_capture import SSEUsageCapture
from usage_records import (
    _normalize_recorded_usage_event,
    _usage_event_archive_summary,
    _usage_event_archive_key,
)


# ---------------------------------------------------------------------------

_NATIVE_USAGE_EVENT_DEDUPE_KEY_LIMIT = 100_000


# ---------------------------------------------------------------------------


@dataclass
class UsageTrackingState:
    usage_log_lock: object = field(default_factory=Lock)
    history_loaded: bool = True
    history_loading: bool = False
    recent_usage_events: deque[dict] = field(default_factory=deque)
    archived_usage_events: list[dict] = field(default_factory=list)
    # The dashboard asks for the combined all-time history on every update.
    # Keep a deduplicated snapshot in memory and invalidate it only when the
    # history actually changes instead of hashing the entire archive per
    # browser refresh.
    all_usage_events_snapshot: list[dict] | None = None
    recent_usage_events_snapshot: list[dict] | None = None
    native_lifecycle_revision: int = 0
    native_usage_event_dedupe_keys: OrderedDict[str, None] = field(
        default_factory=OrderedDict
    )
    session_request_id_lock: object = field(default_factory=Lock)
    latest_server_request_ids_by_chain: dict[tuple[str, str], str] = field(
        default_factory=dict
    )
    active_server_request_ids_by_request: dict[str, dict[str, str | None]] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------


def request_body_session_id(request_body: dict | None = None) -> str | None:
    if not isinstance(request_body, dict):
        return None
    for key in ("session_id", "sessionId"):
        value = request_body.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    metadata = request_body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("session_id", "sessionId"):
            value = metadata.get(key)
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    return normalized
        user_id = metadata.get("user_id")
        user_id_payload = None
        if isinstance(user_id, str):
            normalized = user_id.strip()
            if normalized:
                try:
                    user_id_payload = json.loads(normalized)
                except json.JSONDecodeError:
                    user_id_payload = None
        elif isinstance(user_id, dict):
            user_id_payload = user_id
        if isinstance(user_id_payload, dict):
            for key in ("session_id", "sessionId"):
                value = user_id_payload.get(key)
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized:
                        return normalized

    codex_session_id = codex_agent_compat.codex_session_id(request_body)
    if codex_session_id:
        return codex_session_id

    return None


def request_session_id(
    request: Request, request_body: dict | None = None
) -> str | None:
    for header_name in (
        "session_id",
        "session-id",
        "x-claude-code-session-id",
        "x-session-affinity",
        "x-opencode-session",
    ):
        header_value = request.headers.get(header_name)
        if isinstance(header_value, str):
            normalized = header_value.strip()
            if normalized:
                return normalized

    return request_body_session_id(request_body)


def _drop_outbound_headers(headers: dict, header_names: tuple[str, ...]) -> None:
    names = {name.lower() for name in header_names}
    for key in list(headers.keys()):
        if isinstance(key, str) and key.lower() in names:
            headers.pop(key, None)


def _prepare_responses_affinity_headers(outbound_headers: dict) -> None:
    _drop_outbound_headers(
        outbound_headers,
        ("request-id", "x-github-request-id", "session_id", "session-id"),
    )
    _drop_outbound_headers(outbound_headers, ("x-request-id",))


def _display_model_name(model_name: str | None) -> str | None:
    return _normalize_model_name(model_name) or model_name


def _initiator_log_label(initiator: str | None) -> str:
    return "Agent" if initiator == "agent" else "User"


# ---------------------------------------------------------------------------


def _iter_usage_history_lines(file, end_offset: int, *, newest_first: bool):
    if not newest_first:
        file.seek(0)
        while file.tell() < end_offset:
            line = file.readline(end_offset - file.tell())
            if not line:
                break
            yield line
        return

    # Read from the tail in bounded blocks, splitting bytes before decoding so
    # UTF-8 characters and large prompt rows can cross block boundaries.
    pending = b""
    while end_offset:
        size = min(64 * 1024, end_offset)
        end_offset -= size
        file.seek(end_offset)
        lines = (file.read(size) + pending).split(b"\n")
        pending = lines.pop(0)
        yield from reversed(lines)
    if pending:
        yield pending


class UsageTracker:
    """
    Self-contained usage tracker that owns its state, archive store, and
    completion callbacks. All mutable state lives on ``self.state`` and
    ``self.archive_store``; there are no module-level globals.
    """

    def __init__(
        self,
        *,
        state: UsageTrackingState | None = None,
        archive_store: UsageArchiveStore | None = None,
        usage_log_file: str | None = None,
        error_log_file: str | None = None,
        on_request_finished: Callable | None = None,
        on_usage_event_recorded: Callable | None = None,
    ):
        self.state = state or UsageTrackingState()
        self.archive_store = archive_store or UsageArchiveStore()
        self.usage_log_file = usage_log_file or USAGE_LOG_FILE
        self.error_log_file = error_log_file or REQUEST_ERROR_LOG_FILE
        self.on_request_finished = on_request_finished
        self.on_usage_event_recorded = on_usage_event_recorded

    # ------------------------------------------------------------------
    # Delegating helpers (pure functions stay module-level)
    # ------------------------------------------------------------------

    def request_session_id(
        self, request: Request, request_body: dict | None = None
    ) -> str | None:
        return request_session_id(request, request_body)

    def create_sse_capture(self, stream_type: str) -> SSEUsageCapture:
        return SSEUsageCapture(stream_type)

    def _register_native_usage_event_locked(self, event: dict) -> bool:
        """Return whether a native usage observation has not been recorded yet.

        The scanner is intentionally read-only and may revisit a rollout after
        a cursor reset or when Codex preserves a turn in another rollout file.
        Only native events have a durable logical-turn fingerprint, so leave
        proxied request attempts untouched.
        """
        key = _native_usage_event_dedupe_key(event)
        if not key:
            return True
        keys = self.state.native_usage_event_dedupe_keys
        if key in keys:
            keys.move_to_end(key)
            return False
        keys[key] = None
        while len(keys) > _NATIVE_USAGE_EVENT_DEDUPE_KEY_LIMIT:
            keys.popitem(last=False)
        return True

    def _rebuild_native_usage_event_dedupe_keys_locked(self) -> None:
        self.state.native_usage_event_dedupe_keys.clear()
        for event in (
            *self.state.archived_usage_events,
            *self.state.recent_usage_events,
        ):
            self._register_native_usage_event_locked(event)

    def _refresh_native_lifecycle_metadata_locked(self) -> None:
        """Backfill lifecycle fields after a rollout finishes.

        Native token observations are usually ingested before Codex appends
        ``task_complete`` to the rollout file.  Those observations are
        intentionally deduplicated, so the later file update does not produce
        another usage event that could carry the completed duration.  Refresh
        the existing in-memory rows from the rollout metadata before exposing
        dashboard snapshots.
        """
        if usage_records._native_turn_metadata_for_rollout is None:
            return

        events = (*self.state.archived_usage_events, *self.state.recent_usage_events)
        changed = False
        for event in events:
            if not isinstance(event, dict):
                continue
            if _usage_event_source(event) != "codex_native":
                continue
            path = event.get("native_rollout_path")
            turn_id = event.get("native_turn_id")
            if (
                not isinstance(path, str)
                or not path
                or not isinstance(turn_id, str)
                or not turn_id
            ):
                continue
            # A completed timestamp or duration is sufficient for display.
            if (
                event.get("native_turn_completed_at")
                or event.get("native_turn_duration_ms") is not None
            ):
                continue
            try:
                metadata = usage_records._native_turn_metadata_for_rollout(
                    path, turn_id
                )
            except Exception:
                continue
            for key, value in metadata.items():
                if value is not None and event.get(key) != value:
                    event[key] = value
                    changed = True
        if changed:
            self.state.native_lifecycle_revision += 1

    def native_lifecycle_revision(self) -> int:
        """Refresh native lifecycle fields and return the current revision."""
        with self.state.usage_log_lock:
            self._refresh_native_lifecycle_metadata_locked()
            return self.state.native_lifecycle_revision

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def replace_history(
        self,
        *,
        recent_events: list[dict] | None = None,
        archived_events: list[dict] | None = None,
    ):
        normalized_recent = [
            normalized
            for event in (recent_events or [])
            if (normalized := _normalize_recorded_usage_event(event)) is not None
        ]
        normalized_archived = [
            normalized
            for event in (archived_events or [])
            if (normalized := _normalize_recorded_usage_event(event)) is not None
        ]
        with self.state.usage_log_lock:
            self.state.recent_usage_events.clear()
            self.state.archived_usage_events.clear()
            self.state.all_usage_events_snapshot = None
            self.state.recent_usage_events_snapshot = None
            self.state.native_usage_event_dedupe_keys.clear()
            for event in normalized_archived:
                if self._register_native_usage_event_locked(event):
                    self.state.archived_usage_events.append(event)
            for event in normalized_recent:
                if self._register_native_usage_event_locked(event):
                    self.state.recent_usage_events.append(event)
            self.state.native_lifecycle_revision += 1

    def snapshot_archived_usage_events(self) -> list[dict]:
        with self.state.usage_log_lock:
            return deduplicate_usage_events(self.state.archived_usage_events)

    # ------------------------------------------------------------------
    # Session / request context tracking (private methods)
    # ------------------------------------------------------------------

    def _remember_server_request_id(
        self, event: dict | None, *, only_if_missing: bool = False
    ):
        if not isinstance(event, dict):
            return
        chain_key = _server_request_chain_key(
            event.get("session_id"),
            event.get("client_request_id"),
            event.get("subagent"),
        )
        server_request_id = event.get("server_request_id")
        if not isinstance(server_request_id, str) or not server_request_id:
            return
        with self.state.session_request_id_lock:
            if only_if_missing:
                self.state.latest_server_request_ids_by_chain.setdefault(
                    chain_key, server_request_id
                )
            else:
                self.state.latest_server_request_ids_by_chain[chain_key] = (
                    server_request_id
                )

    def _remember_active_server_request_id(self, event: dict | None):
        if not isinstance(event, dict):
            return
        request_id = event.get("request_id")
        server_request_id = event.get("server_request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        if not isinstance(server_request_id, str) or not server_request_id:
            return
        with self.state.session_request_id_lock:
            self.state.active_server_request_ids_by_request[request_id] = {
                "session_id": event.get("session_id"),
                "session_id_origin": event.get("session_id_origin"),
                "client_request_id": event.get("client_request_id"),
                "subagent": event.get("subagent"),
                "initiator": event.get("initiator"),
                "server_request_id": server_request_id,
            }

    def _forget_active_server_request_id(self, request_id: str | None):
        if not isinstance(request_id, str) or not request_id:
            return
        with self.state.session_request_id_lock:
            self.state.active_server_request_ids_by_request.pop(request_id, None)

    def _get_active_server_request_id(
        self,
        session_id: str | None,
        client_request_id: str | None,
        subagent: str | None,
        *,
        initiator: str | None = None,
    ) -> str | None:
        target_subagent = subagent if isinstance(subagent, str) and subagent else None
        with self.state.session_request_id_lock:
            for context in reversed(
                list(self.state.active_server_request_ids_by_request.values())
            ):
                context_subagent = context.get("subagent")
                if isinstance(context_subagent, str):
                    context_subagent = context_subagent or None
                else:
                    context_subagent = None
                if context_subagent != target_subagent:
                    continue
                if initiator is not None and context.get("initiator") != initiator:
                    continue
                if isinstance(session_id, str) and session_id:
                    if context.get("session_id") != session_id:
                        continue
                elif isinstance(client_request_id, str) and client_request_id:
                    if context.get("client_request_id") != client_request_id:
                        continue
                elif target_subagent is not None:
                    continue
                server_request_id = context.get("server_request_id")
                if isinstance(server_request_id, str) and server_request_id:
                    return server_request_id
        return None

    def _get_latest_server_request_id(
        self,
        session_id: str | None,
        client_request_id: str | None,
        subagent: str | None,
    ) -> str | None:
        chain_key = _server_request_chain_key(session_id, client_request_id, subagent)
        with self.state.session_request_id_lock:
            return self.state.latest_server_request_ids_by_chain.get(chain_key)

    def _resolve_server_request_id(
        self,
        request: Request,
        initiator: str | None,
        request_body: dict | None = None,
        *,
        session_id: str | None = None,
        client_request_id: str | None = None,
        subagent: str | None = None,
    ) -> tuple[str, str | None]:
        forwarded_server_request_id = request.headers.get(
            "x-request-id"
        ) or request.headers.get("request-id")

        if session_id is None:
            session_id = request_session_id(request, request_body)
        if client_request_id is None:
            client_request_id = request.headers.get("x-client-request-id")
        if subagent is None:
            subagent = request.headers.get("x-openai-subagent")

        prior_server_request_id = forwarded_server_request_id or None

        if initiator == "agent":
            active_server_request_id = self._get_active_server_request_id(
                session_id,
                client_request_id,
                subagent,
                initiator="user",
            )
            if isinstance(active_server_request_id, str) and active_server_request_id:
                prior_server_request_id = active_server_request_id

        if prior_server_request_id is None and session_id is not None:
            latest_server_request_id = self._get_latest_server_request_id(
                session_id,
                client_request_id,
                subagent,
            )
            if isinstance(latest_server_request_id, str) and latest_server_request_id:
                prior_server_request_id = latest_server_request_id

        generated_server_request_id = str(uuid4())
        return generated_server_request_id, prior_server_request_id

    # ------------------------------------------------------------------
    # Persistence (private methods)
    # ------------------------------------------------------------------

    def _rewrite_usage_log(self, events: list[dict]):
        log_dir = os.path.dirname(self.usage_log_file) or TOKEN_DIR
        os.makedirs(log_dir, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix="usage-log-", suffix=".jsonl", dir=log_dir
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                for event in events:
                    temp_file.write(
                        json.dumps(event, separators=(",", ":"), default=_json_default)
                    )
                    temp_file.write("\n")
            os.replace(temp_path, self.usage_log_file)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _delete_archived_events(self, keys: list[str]):
        self.archive_store.delete_events(keys)

    def _persist_event(self, event: dict):
        if not isinstance(event, dict):
            return

        os.makedirs(os.path.dirname(self.usage_log_file) or TOKEN_DIR, exist_ok=True)
        serialized = json.dumps(event, separators=(",", ":"), default=_json_default)
        native_dedupe_key = _native_usage_event_dedupe_key(event)
        with self.state.usage_log_lock:
            if not self._register_native_usage_event_locked(event):
                return
            try:
                with open(self.usage_log_file, "a", encoding="utf-8") as f:
                    f.write(serialized)
                    f.write("\n")
            except Exception:
                if native_dedupe_key:
                    self.state.native_usage_event_dedupe_keys.pop(
                        native_dedupe_key, None
                    )
                raise
            self.state.recent_usage_events.append(event)
            # Preserve the combined dashboard snapshot incrementally when it
            # has already been materialized.  Archived rows are immutable for
            # aggregation purposes, so compaction does not require a full
            # archive rebuild.
            if self.state.all_usage_events_snapshot is not None:
                self.state.all_usage_events_snapshot.append(event)
            if self.state.recent_usage_events_snapshot is not None:
                self.state.recent_usage_events_snapshot.append(event)
        self._compact_if_needed()
        self._remember_server_request_id(event)
        if self.on_usage_event_recorded is not None:
            self.on_usage_event_recorded(event)

    # ------------------------------------------------------------------
    # Archival
    # ------------------------------------------------------------------

    def load_archived_history(self):
        rows = self.archive_store.read_rows()
        if rows is None:
            return

        loaded_events: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            normalized_event = _normalize_recorded_usage_event(
                payload, refresh_native_tiers=False
            )
            if normalized_event is not None:
                loaded_events.append(normalized_event)
        with self.state.usage_log_lock:
            self.state.archived_usage_events.clear()
            self.state.archived_usage_events.extend(
                deduplicate_usage_events(loaded_events)
            )
            self.state.all_usage_events_snapshot = None
            self.state.recent_usage_events_snapshot = None
            self._rebuild_native_usage_event_dedupe_keys_locked()

    def _compact_if_needed(self):
        with self.state.usage_log_lock:
            if self.state.history_loading or not self.state.history_loaded:
                return
            overflow = (
                len(self.state.recent_usage_events) - DETAILED_REQUEST_HISTORY_LIMIT
            )
            if overflow <= 0:
                return
            if not self.archive_store.init_storage():
                return

            detailed_events = list(self.state.recent_usage_events)
            events_to_archive = detailed_events[:overflow]
            remaining_events = detailed_events[overflow:]
            archive_rows = []
            archived_summaries = []
            archive_keys = []
            for event in events_to_archive:
                summary = _usage_event_archive_summary(event)
                archive_key = _usage_event_archive_key(summary)
                recorded_at = (
                    summary.get("finished_at")
                    or summary.get("started_at")
                    or utc_now_iso()
                )
                archive_rows.append(
                    (
                        archive_key,
                        recorded_at,
                        json.dumps(
                            summary, separators=(",", ":"), default=_json_default
                        ),
                    )
                )
                archived_summaries.append(summary)
                archive_keys.append(archive_key)

            try:
                self.archive_store.insert_rows(archive_rows)
                self._rewrite_usage_log(remaining_events)
            except Exception:
                self._delete_archived_events(archive_keys)
                return

            self.state.recent_usage_events.clear()
            self.state.recent_usage_events.extend(remaining_events)
            self.state.recent_usage_events_snapshot = None
            archived_replacements = []
            for original_event, summary in zip(events_to_archive, archived_summaries):
                normalized_event = _normalize_recorded_usage_event(summary)
                if normalized_event is not None:
                    self.state.archived_usage_events.append(normalized_event)
                    archived_replacements.append((original_event, normalized_event))

            if (
                self.state.all_usage_events_snapshot is not None
                and archived_replacements
            ):
                replacements = {
                    id(original): normalized
                    for original, normalized in archived_replacements
                }
                self.state.all_usage_events_snapshot = [
                    replacements.get(id(event), event)
                    for event in self.state.all_usage_events_snapshot
                ]

    def load_history(self, *, limit: int | None = None):
        if limit is not None:
            self.state.history_loaded = False
        loaded_events: list[dict] = []
        try:
            with self.state.usage_log_lock:
                f = open(self.usage_log_file, "rb")
                recent_count = len(self.state.recent_usage_events)
                self.state.history_loading = True
                end_offset = f.seek(0, os.SEEK_END)
            with f:
                # Only read bytes present at the snapshot boundary. Requests
                # can append to disk and remain visible during normalization.
                for line in _iter_usage_history_lines(
                    f, end_offset, newest_first=limit is not None
                ):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    normalized_event = _normalize_recorded_usage_event(
                        payload, refresh_native_tiers=False
                    )
                    if normalized_event is not None:
                        loaded_events.append(normalized_event)
                        if limit is not None and len(loaded_events) >= limit:
                            break
            if limit is not None:
                loaded_events.reverse()
            loaded_events = deduplicate_usage_events(loaded_events)

            with self.state.usage_log_lock:
                # Compaction is deferred during loading, so this suffix is
                # exactly the requests persisted after the file snapshot.
                appended_events = list(self.state.recent_usage_events)[recent_count:]
                if self.state.recent_usage_events:
                    self.state.recent_usage_events.clear()
                    self._rebuild_native_usage_event_dedupe_keys_locked()
                # At startup the archive loader has already built these keys;
                # only a reload needs to remove keys from replaced recent rows.
                for event in (*loaded_events, *appended_events):
                    if self._register_native_usage_event_locked(event):
                        self.state.recent_usage_events.append(event)
                self.state.all_usage_events_snapshot = None
                self.state.recent_usage_events_snapshot = list(
                    self.state.recent_usage_events
                )
                self.state.native_lifecycle_revision += 1
                self.state.history_loaded = limit is None
        except FileNotFoundError:
            self.state.history_loaded = limit is None
            return
        except OSError:
            return
        finally:
            with self.state.usage_log_lock:
                self.state.history_loading = False

        # Restore newest-first without replacing request-chain IDs registered
        # by live traffic while older history was loading.
        for event in reversed(self.snapshot_usage_events()):
            self._remember_server_request_id(event, only_if_missing=True)
        if limit is None:
            self._compact_if_needed()

    # ------------------------------------------------------------------
    # Usage event lifecycle
    # ------------------------------------------------------------------

    def start_event(
        self,
        request: Request,
        requested_model: str | None,
        resolved_model: str | None,
        initiator: str | None,
        request_id: str | None = None,
        request_body: dict | None = None,
        upstream_path: str | None = None,
        outbound_headers: dict | None = None,
        prompt_preview: dict | None = None,
        initiator_verdict: dict | None = None,
    ) -> dict:
        display_requested_model = _display_model_name(requested_model)
        display_resolved_model = _display_model_name(resolved_model)
        parts = [
            "INFO:",
            f"Local request ({_initiator_log_label(initiator)}):",
            f"{request.method} {request.url.path}",
        ]
        if display_requested_model is not None:
            parts.append(f"requested_model={display_requested_model}")
        if (
            display_resolved_model is not None
            and display_resolved_model != display_requested_model
        ):
            parts.append(f"resolved_model={display_resolved_model}")
        print(" ".join(parts), flush=True)

        event_request_id = request_id or uuid4().hex
        client_request_id = request.headers.get("x-client-request-id")
        if not client_request_id and isinstance(outbound_headers, dict):
            outbound_client_request_id = outbound_headers.get("x-client-request-id")
            if isinstance(outbound_client_request_id, str):
                normalized_outbound_client_request_id = (
                    outbound_client_request_id.strip()
                )
                if normalized_outbound_client_request_id:
                    client_request_id = normalized_outbound_client_request_id
        subagent = request.headers.get("x-openai-subagent")
        session_id = request_session_id(request, request_body)
        project_path = None
        session_id_origin = "request" if session_id else None

        server_request_id, prior_server_request_id = self._resolve_server_request_id(
            request,
            initiator,
            request_body,
            session_id=session_id,
            client_request_id=client_request_id,
            subagent=subagent,
        )
        if isinstance(outbound_headers, dict):
            _prepare_responses_affinity_headers(outbound_headers)
        started_at = utc_now_iso()
        event = {
            "request_id": event_request_id,
            "started_at": started_at,
            "path": request.url.path,
            "method": request.method,
            "upstream_path": upstream_path,
            "requested_model": requested_model,
            "resolved_model": resolved_model or requested_model,
            "initiator": initiator,
            "session_id": session_id,
            "session_id_origin": session_id_origin,
            "project_path": project_path,
            "client_request_id": client_request_id,
            "subagent": subagent,
            "server_request_id": server_request_id,
            "prior_server_request_id": prior_server_request_id,
            "_started_monotonic": time.perf_counter(),
        }
        if isinstance(prompt_preview, dict) and prompt_preview:
            event["request_prompt"] = prompt_preview
        if isinstance(initiator_verdict, dict) and initiator_verdict:
            candidate_initiator = initiator_verdict.get("candidate_initiator")
            resolved_initiator = initiator_verdict.get("resolved_initiator")
            verdict_snapshot = {
                key: value
                for key, value in initiator_verdict.items()
                if value is not None
            }
            if verdict_snapshot:
                event["initiator_verdict"] = verdict_snapshot
            if isinstance(candidate_initiator, str) and candidate_initiator:
                event["candidate_initiator"] = candidate_initiator
            if isinstance(resolved_initiator, str) and resolved_initiator:
                event["resolved_initiator"] = resolved_initiator
        self._remember_server_request_id(event)
        self._remember_active_server_request_id(event)
        return event

    def mark_first_output(self, event: dict | None):
        if not isinstance(event, dict):
            return
        if event.get("_first_output_monotonic") is None:
            event["_first_output_monotonic"] = time.perf_counter()

    def finish_event(
        self,
        event: dict | None,
        status_code: int,
        *,
        upstream: httpx.Response | None = None,
        response_payload: dict | None = None,
        response_text: str | None = None,
        reasoning_text: str | None = None,
        usage: dict | None = None,
    ):
        if not isinstance(event, dict):
            return

        finished_at = utc_now()
        self._forget_active_server_request_id(event.get("request_id"))
        if self.on_request_finished is not None:
            callback = self.on_request_finished
            callback_kwargs = {"finished_at": finished_at}
            try:
                callback_parameters = inspect.signature(callback).parameters
            except (TypeError, ValueError):
                callback_parameters = {}
            successful_parameter = callback_parameters.get("successful")
            if (
                successful_parameter is not None
                and successful_parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            ) or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in callback_parameters.values()
            ):
                callback_kwargs["successful"] = status_code < 400
            callback(event.get("request_id"), **callback_kwargs)
        finished_event = {
            **{
                key: value
                for key, value in event.items()
                if not str(key).startswith("_")
            },
            "finished_at": finished_at.isoformat(),
            "status_code": status_code,
            "success": status_code < 400,
        }

        started_monotonic = event.get("_started_monotonic")
        if isinstance(started_monotonic, (int, float)):
            finished_event["duration_ms"] = max(
                0, int(round((time.perf_counter() - started_monotonic) * 1000))
            )

        first_output_monotonic = event.get("_first_output_monotonic")
        if isinstance(started_monotonic, (int, float)) and isinstance(
            first_output_monotonic, (int, float)
        ):
            finished_event["time_to_first_token_ms"] = max(
                0, int(round((first_output_monotonic - started_monotonic) * 1000))
            )

        if upstream is not None:
            for header_name in ("x-request-id", "request-id"):
                header_value = upstream.headers.get(header_name)
                if header_value:
                    finished_event["upstream_request_id"] = header_value
                    break
            content_type = upstream.headers.get("content-type")
            if content_type:
                finished_event["response_content_type"] = content_type

            # Also keep generic x-ratelimit-* if any upstream variant ever emits them.
            rate_limit_fields = {}
            for header_name in (
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
                "x-ratelimit-used",
                "x-ratelimit-resource",
                "retry-after",
            ):
                header_value = upstream.headers.get(header_name)
                if header_value is None:
                    continue
                short_key = header_name.replace("x-ratelimit-", "").replace("-", "_")
                rate_limit_fields[short_key] = header_value
            if rate_limit_fields:
                finished_event["rate_limit"] = rate_limit_fields

        if isinstance(response_payload, dict):
            payload_response_id = response_payload.get("id")
            if isinstance(payload_response_id, str):
                finished_event["response_id"] = payload_response_id
            payload_model = response_payload.get("model")
            if isinstance(payload_model, str):
                finished_event["response_model"] = payload_model

        if isinstance(reasoning_text, str) and reasoning_text:
            # Mirror how response_text-style fields are surfaced: keep a bounded
            # excerpt so dashboards / trace viewers can show what the model was
            # actually thinking without retaining megabytes of reasoning.
            finished_event["reasoning_text"] = reasoning_text[
                :RESPONSE_REASONING_PREVIEW_MAX_CHARS
            ]
            if len(reasoning_text) > RESPONSE_REASONING_PREVIEW_MAX_CHARS:
                finished_event["reasoning_text_truncated"] = True
                finished_event["reasoning_text_chars"] = len(reasoning_text)

        derived_usage = usage
        if isinstance(derived_usage, dict):
            derived_usage = normalize_usage_payload(derived_usage)
        if derived_usage is None and isinstance(response_payload, dict):
            derived_usage = _extract_payload_usage(response_payload)
        if isinstance(derived_usage, dict):
            finished_event["usage"] = derived_usage

        model_name = (
            finished_event.get("response_model")
            or finished_event.get("resolved_model")
            or finished_event.get("requested_model")
        )
        finished_event["cost_usd"] = _usage_event_estimated_cost(
            finished_event,
            model_name=model_name,
            usage=derived_usage,
        )
        self._remember_server_request_id(finished_event)
        self._persist_event(finished_event)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot_usage_events(self) -> list[dict]:
        with self.state.usage_log_lock:
            self._refresh_native_lifecycle_metadata_locked()
            if self.state.recent_usage_events_snapshot is None:
                self.state.recent_usage_events_snapshot = deduplicate_usage_events(
                    self.state.recent_usage_events
                )
            return list(self.state.recent_usage_events_snapshot)

    def snapshot_all_usage_events(self) -> list[dict]:
        with self.state.usage_log_lock:
            self._refresh_native_lifecycle_metadata_locked()
            if self.state.all_usage_events_snapshot is None:
                self.state.all_usage_events_snapshot = deduplicate_usage_events(
                    [*self.state.archived_usage_events, *self.state.recent_usage_events]
                )
            return list(self.state.all_usage_events_snapshot)

    # ------------------------------------------------------------------
    # Error recording
    # ------------------------------------------------------------------

    def record_request_error(self, event: dict):
        if not isinstance(event, dict):
            return

        os.makedirs(os.path.dirname(self.error_log_file) or TOKEN_DIR, exist_ok=True)
        serialized = json.dumps(event, separators=(",", ":"), default=_json_default)
        with open(self.error_log_file, "a", encoding="utf-8") as f:
            f.write(serialized)
            f.write("\n")

    # ------------------------------------------------------------------
    # Backward-compatible public aliases
    # ------------------------------------------------------------------

    def compact_history_if_needed(self):
        return self._compact_if_needed()

    def latest_server_request_id(
        self,
        session_id: str | None,
        client_request_id: str | None,
        subagent: str | None,
    ) -> str | None:
        return self._get_latest_server_request_id(
            session_id, client_request_id, subagent
        )
