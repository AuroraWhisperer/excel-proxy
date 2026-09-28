"""Background request-trace and body-dump writes with bounded retention."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from threading import Lock

import util
from constants import (
    REQUEST_TRACE_HISTORY_LIMIT,
    REQUEST_TRACE_RETENTION_SLACK,
    TOKEN_DIR,
)

_REQUEST_TRACE_LOCK = Lock()
_REQUEST_BODY_DUMP_LOCK = Lock()
_REQUEST_BODY_DUMP_EXECUTOR: ThreadPoolExecutor | None = None
_REQUEST_BODY_DUMP_EXECUTOR_LOCK = Lock()
_REQUEST_TRACE_EXECUTOR: ThreadPoolExecutor | None = None
_REQUEST_TRACE_EXECUTOR_LOCK = Lock()


def _write_request_trace_line(trace_path: str, line: str) -> None:
    try:
        log_dir = os.path.dirname(trace_path) or TOKEN_DIR
        os.makedirs(log_dir, exist_ok=True)
        with _REQUEST_TRACE_LOCK:
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(line)
            _enforce_trace_retention_locked(trace_path)
    except OSError as exc:
        print(
            f"Warning: failed to write request trace log: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _enforce_body_dump_retention_locked(dump_dir: str) -> None:
    """Cap body-dump directory at REQUEST_TRACE_HISTORY_LIMIT files."""
    limit = REQUEST_TRACE_HISTORY_LIMIT
    if limit <= 0:
        return
    try:
        entries = os.listdir(dump_dir)
    except OSError:
        return
    if len(entries) <= limit + max(REQUEST_TRACE_RETENTION_SLACK, 0):
        return
    paths = []
    for name in entries:
        full = os.path.join(dump_dir, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        paths.append((mtime, full))
    paths.sort()
    for _, path in paths[: max(0, len(paths) - limit)]:
        try:
            os.unlink(path)
        except OSError:
            pass


def _enforce_trace_retention_locked(trace_path: str) -> None:
    """Keep the trace log bounded at REQUEST_TRACE_HISTORY_LIMIT rows."""
    limit = REQUEST_TRACE_HISTORY_LIMIT
    if limit <= 0:
        return
    threshold = limit + max(REQUEST_TRACE_RETENTION_SLACK, 0)
    try:
        size = os.path.getsize(trace_path)
    except OSError:
        return
    if size < threshold * 256:
        return
    try:
        with open(trace_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= threshold:
        return
    try:
        with open(trace_path, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])
    except OSError as exc:
        print(
            f"Warning: trace retention rewrite failed: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _get_request_trace_executor() -> ThreadPoolExecutor:
    global _REQUEST_TRACE_EXECUTOR
    if _REQUEST_TRACE_EXECUTOR is not None:
        return _REQUEST_TRACE_EXECUTOR
    with _REQUEST_TRACE_EXECUTOR_LOCK:
        if _REQUEST_TRACE_EXECUTOR is None:
            _REQUEST_TRACE_EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ghcp-trace"
            )
    return _REQUEST_TRACE_EXECUTOR


def _get_request_body_dump_executor() -> ThreadPoolExecutor:
    global _REQUEST_BODY_DUMP_EXECUTOR
    if _REQUEST_BODY_DUMP_EXECUTOR is not None:
        return _REQUEST_BODY_DUMP_EXECUTOR
    with _REQUEST_BODY_DUMP_EXECUTOR_LOCK:
        if _REQUEST_BODY_DUMP_EXECUTOR is None:
            _REQUEST_BODY_DUMP_EXECUTOR = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="ghcp-body-dump"
            )
    return _REQUEST_BODY_DUMP_EXECUTOR


def _write_request_body_dump(out_path: str, dump_dir: str, snapshot: dict) -> None:
    """Background worker: serialize the snapshot and persist it.

    Runs on the body-dump executor so the event loop is never blocked by
    disk I/O. Catches every exception so a malformed payload cannot leak
    out of the worker.
    """
    try:
        with _REQUEST_BODY_DUMP_LOCK:
            os.makedirs(dump_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    snapshot, f, separators=(",", ":"), default=util._json_default
                )
            _enforce_body_dump_retention_locked(dump_dir)
    except Exception as exc:  # pragma: no cover - dump must never raise
        print(
            f"Warning: failed to write request body dump: {exc}",
            file=sys.stderr,
            flush=True,
        )


def submit_trace_line(trace_path: str, line: str):
    return _get_request_trace_executor().submit(
        _write_request_trace_line, trace_path, line
    )


def submit_body_dump(out_path: str, dump_dir: str, snapshot: dict):
    return _get_request_body_dump_executor().submit(
        _write_request_body_dump, out_path, dump_dir, snapshot
    )


def shutdown() -> None:
    """Drain any pending writes before isolated runtime directories are removed."""
    for executor in (_REQUEST_TRACE_EXECUTOR, _REQUEST_BODY_DUMP_EXECUTOR):
        if executor is not None:
            executor.shutdown(wait=True)
