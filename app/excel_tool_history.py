"""Bounded, transactional storage for native tool-call replay identities."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
import json
import os
import sqlite3
import threading
import time

from app_paths import user_state_dir


_NATIVE_CALL_CACHE_LIMIT = 512

_NATIVE_CALL_CACHE_BYTES = 8 * 1024 * 1024

_NATIVE_CALL_ENTRY_BYTES = 4 * 1024 * 1024

_NATIVE_CALL_DB_LIMIT = 8192

_NATIVE_CALL_DB_BYTES = 64 * 1024 * 1024

_NATIVE_CALL_DB = os.path.join(user_state_dir(), "excel-native-calls.sqlite3")

_NATIVE_CALL_RETENTION_SECONDS = 60 * 24 * 60 * 60

_native_call_cache_lock = threading.Lock()

_native_call_cache: OrderedDict[str, tuple[str, int, float]] = OrderedDict()


def _trim_native_call_cache(now: float) -> None:
    for key, (_, _, last_used) in list(_native_call_cache.items()):
        if last_used < now - _NATIVE_CALL_RETENTION_SECONDS:
            del _native_call_cache[key]
    total = sum(size for _, size, _ in _native_call_cache.values())
    while _native_call_cache and (
        len(_native_call_cache) > _NATIVE_CALL_CACHE_LIMIT
        or total > _NATIVE_CALL_CACHE_BYTES
    ):
        _, (_, size, _) = _native_call_cache.popitem(last=False)
        total -= size


def remember_native_call(item: dict) -> bool:
    return remember_native_calls([item])


def remember_native_calls(items: list[dict]) -> bool:
    records = {}
    for item in items:
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        encoded = json.dumps(item, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        if size > _NATIVE_CALL_ENTRY_BYTES:
            return False
        records[call_id] = (encoded, size)
    if (
        len(records) > _NATIVE_CALL_DB_LIMIT
        or sum(size for _, size in records.values()) > _NATIVE_CALL_DB_BYTES
    ):
        return False
    if not records:
        return True
    with _native_call_cache_lock:
        # Persist the complete validated batch before exposing any call.
        os.makedirs(os.path.dirname(_NATIVE_CALL_DB), exist_ok=True)
        with closing(sqlite3.connect(_NATIVE_CALL_DB)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_calls (call_id TEXT PRIMARY KEY, item TEXT NOT NULL, last_used REAL NOT NULL, byte_size INTEGER NOT NULL)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(native_calls)")}
            if "byte_size" not in columns:
                db.execute(
                    "ALTER TABLE native_calls ADD COLUMN byte_size INTEGER NOT NULL DEFAULT 0"
                )
                db.execute(
                    "UPDATE native_calls SET byte_size = length(CAST(item AS BLOB))"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS native_calls_last_used ON native_calls(last_used)"
            )
            now = time.time()
            db.execute(
                "DELETE FROM native_calls WHERE last_used < ?",
                (now - _NATIVE_CALL_RETENTION_SECONDS,),
            )
            db.executemany(
                "INSERT OR REPLACE INTO native_calls (call_id, item, last_used, byte_size) VALUES (?, ?, ?, ?)",
                [(key, encoded, now, size) for key, (encoded, size) in records.items()],
            )
            count, total = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM native_calls"
            ).fetchone()
            evicted = []
            if count > _NATIVE_CALL_DB_LIMIT or total > _NATIVE_CALL_DB_BYTES:
                for key, size in db.execute(
                    "SELECT call_id, byte_size FROM native_calls ORDER BY last_used, call_id"
                ):
                    if key in records:
                        continue
                    evicted.append(key)
                    count -= 1
                    total -= size
                    if (
                        count <= _NATIVE_CALL_DB_LIMIT
                        and total <= _NATIVE_CALL_DB_BYTES
                    ):
                        break
                db.executemany(
                    "DELETE FROM native_calls WHERE call_id = ?",
                    [(key,) for key in evicted],
                )
        for key in evicted:
            _native_call_cache.pop(key, None)
        for key, (encoded, size) in records.items():
            _native_call_cache[key] = (encoded, size, now)
            _native_call_cache.move_to_end(key)
        _trim_native_call_cache(now)
    return True


def remembered_native_calls(call_ids: list[str]) -> dict[str, dict]:
    call_ids = list(dict.fromkeys(call_ids))
    with _native_call_cache_lock:
        now = time.time()
        _trim_native_call_cache(now)
        result = {
            key: json.loads(_native_call_cache[key][0])
            for key in call_ids
            if key in _native_call_cache
        }
        for key in result:
            encoded, size, _ = _native_call_cache[key]
            _native_call_cache[key] = (encoded, size, now)
            _native_call_cache.move_to_end(key)
        if not call_ids or not os.path.isfile(_NATIVE_CALL_DB):
            return result
        # Read a whole history in bounded SQL batches, not one connection per
        # evicted item. A long transcript can exceed SQLite's parameter limit.
        with closing(sqlite3.connect(_NATIVE_CALL_DB)) as db, db:
            for start in range(0, len(call_ids), 500):
                batch = call_ids[start : start + 500]
                missing = [key for key in batch if key not in result]
                if missing:
                    placeholders = ",".join("?" for _ in missing)
                    for key, item in db.execute(
                        f"SELECT call_id, item FROM native_calls WHERE call_id IN ({placeholders}) "
                        "AND last_used >= ? AND length(CAST(item AS BLOB)) <= ?",
                        [
                            *missing,
                            now - _NATIVE_CALL_RETENTION_SECONDS,
                            _NATIVE_CALL_ENTRY_BYTES,
                        ],
                    ):
                        result[key] = json.loads(item)
                found = [key for key in batch if key in result]
                if found:
                    placeholders = ",".join("?" for _ in found)
                    db.execute(
                        f"UPDATE native_calls SET last_used = ? WHERE call_id IN ({placeholders})",
                        [now, *found],
                    )
        return result
