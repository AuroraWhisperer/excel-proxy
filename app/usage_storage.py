"""SQLite storage for usage archives, independent of consumers."""

from contextlib import closing
from dataclasses import dataclass, field
import os
import sqlite3
from threading import Lock
from typing import Callable

from constants import SQLITE_CACHE_FILE, TOKEN_DIR


_sqlite_cache_lock = Lock()
_sqlite_cache_enabled = True
_sqlite_cache_error = None


@dataclass
class UsageArchiveStore:
    init_storage: Callable[[], bool] = lambda: False
    lock: object = field(default_factory=Lock)
    connect: Callable[[], object] = lambda: None
    mark_unavailable: Callable[[str], None] = lambda error: None

    def read_rows(self):
        if not self.init_storage():
            return None
        try:
            with self.lock:
                with closing(self.connect()) as connection:
                    return connection.execute(
                        "SELECT payload_json FROM archived_usage_events ORDER BY recorded_at ASC"
                    ).fetchall()
        except Exception as exc:
            self.mark_unavailable(str(exc))
            return None

    def insert_rows(self, rows):
        """Commit before the caller replaces its detailed log or in-memory history."""
        with self.lock:
            with closing(self.connect()) as connection:
                connection.executemany(
                    """
                    INSERT INTO archived_usage_events (archive_key, recorded_at, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(archive_key) DO NOTHING
                    """,
                    rows,
                )
                connection.commit()

    def delete_events(self, keys: list[str]):
        if not keys or not self.init_storage():
            return
        try:
            with self.lock:
                with closing(self.connect()) as connection:
                    connection.executemany(
                        "DELETE FROM archived_usage_events WHERE archive_key = ?",
                        [(key,) for key in keys],
                    )
                    connection.commit()
        except Exception as exc:
            self.mark_unavailable(str(exc))


class UsageCacheStore:
    """Own the SQLite database lifecycle used by usage archives."""

    def __init__(self):
        # Schema creation and WAL setup are process-level work.  Running them
        # from every dashboard read turns a read-only page refresh into a
        # SQLite write/metadata storm, especially on Windows.
        self._initialized = False
        self._cache_dir_ready = False

    @property
    def lock(self) -> Lock:
        return _sqlite_cache_lock

    def mark_unavailable(self, error: str):
        global _sqlite_cache_enabled, _sqlite_cache_error
        if _sqlite_cache_enabled:
            print(f"[sqlite] cache disabled: {error}", flush=True)
            _sqlite_cache_enabled = False
            _sqlite_cache_error = error

    def connect(self) -> sqlite3.Connection:
        if not _sqlite_cache_enabled:
            raise RuntimeError("sqlite cache disabled")
        if not self._cache_dir_ready:
            cache_dir = os.path.dirname(SQLITE_CACHE_FILE) or TOKEN_DIR
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_dir_ready = True
        connection = sqlite3.connect(SQLITE_CACHE_FILE, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> bool:
        if not _sqlite_cache_enabled:
            return False
        if self._initialized:
            return True
        try:
            with self.lock:
                if self._initialized:
                    return True
                cache_dir = os.path.dirname(SQLITE_CACHE_FILE) or TOKEN_DIR
                os.makedirs(cache_dir, exist_ok=True)
                self._cache_dir_ready = True
                with closing(self.connect()) as connection:
                    # These are deliberately process-startup operations, not
                    # per-request connection setup.  NORMAL is sufficient for
                    # this cache and avoids making every cache write wait for
                    # the strongest possible fsync behavior.
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=NORMAL")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS archived_usage_events (
                            archive_key TEXT PRIMARY KEY,
                            recorded_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_archived_usage_events_recorded_at
                        ON archived_usage_events (recorded_at DESC)
                        """
                    )
                    connection.commit()
                self._initialized = True
            return True
        except Exception as exc:
            self.mark_unavailable(str(exc))
            return False

    def usage_archive_store(self):
        return UsageArchiveStore(
            init_storage=self.initialize,
            lock=self.lock,
            connect=self.connect,
            mark_unavailable=self.mark_unavailable,
        )


usage_cache_store = UsageCacheStore()


def create_usage_archive_store():
    """Use the shared usage database and lock."""
    return usage_cache_store.usage_archive_store()
