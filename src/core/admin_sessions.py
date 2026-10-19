"""Persistent, revocable admin sessions shared by HTTP and browser viewers."""

import hashlib
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

ADMIN_SESSION_MAX_AGE = 30 * 24 * 60 * 60


class AdminSessionStore:
    """Keep token hashes in private runtime storage, never raw bearer tokens."""

    def __init__(self, get_path: Callable[[], Path]):
        self._get_path = get_path

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @contextmanager
    def _connect(self, path: Path):
        with closing(sqlite3.connect(path)) as database, database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS admin_sessions "
                "(token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
            )
            yield database

    def add(self, token: str):
        path = self._get_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect(path) as database:
            now = time.time()
            database.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
            database.execute(
                "INSERT OR REPLACE INTO admin_sessions (token_hash, expires_at) VALUES (?, ?)",
                (self._digest(token), now + ADMIN_SESSION_MAX_AGE),
            )

    def __contains__(self, token: str) -> bool:
        path = self._get_path()
        if not token or not path.is_file():
            return False
        with self._connect(path) as database:
            return (
                database.execute(
                    "SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
                    (self._digest(token), time.time()),
                ).fetchone()
                is not None
            )

    def __bool__(self) -> bool:
        path = self._get_path()
        if not path.is_file():
            return False
        with self._connect(path) as database:
            return (
                database.execute(
                    "SELECT 1 FROM admin_sessions WHERE expires_at > ? LIMIT 1", (time.time(),)
                ).fetchone()
                is not None
            )

    def discard(self, token: str):
        path = self._get_path()
        if path.is_file():
            with self._connect(path) as database:
                database.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?", (self._digest(token),)
                )

    def clear(self):
        path = self._get_path()
        if path.is_file():
            with self._connect(path) as database:
                database.execute("DELETE FROM admin_sessions")
