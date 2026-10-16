"""SQLite key/value cache shared by the web workers and the narrative thread.

A connection is opened per call: sqlite3 connections must not cross threads, and the volume
(a few reads per page view) does not justify a pool. Every statement is parameterized.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
BUSY_TIMEOUT_S = 10.0


class Cache:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "key TEXT PRIMARY KEY, value TEXT, stored_at TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_S)

    def get(self, key: str, max_age: timedelta | None) -> tuple[dict, datetime] | None:
        """Return (value, stored time), or None when absent or older than max_age."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value, stored_at FROM entries WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            stored_at = datetime.fromisoformat(row[1])
        except (ValueError, TypeError):
            log.warning("corrupt cache row for key %s ignored", key)
            return None
        if max_age is not None and datetime.now(UTC) - stored_at > max_age:
            return None
        return value, stored_at

    def set(self, key: str, value: dict) -> None:
        stored_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO entries (key, value, stored_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), stored_at),
            )
