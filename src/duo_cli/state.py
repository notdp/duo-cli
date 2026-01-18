"""SQLite state storage for session information."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class SqliteBackend:
    """SQLite-based state storage."""
    
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path("/tmp/duo.db")
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._init_table()
    
    @property
    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn
    
    def _init_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (namespace, key)
            )
        """)
    
    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO state (namespace, key, value) VALUES (?, ?, ?)",
            ("_global", key, value),
        )
    
    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM state WHERE namespace = ? AND key = ?",
            ("_global", key),
        ).fetchone()
        return row[0] if row else None
    
    def hset(self, name: str, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO state (namespace, key, value) VALUES (?, ?, ?)",
            (name, key, value),
        )
    
    def hget(self, name: str, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM state WHERE namespace = ? AND key = ?",
            (name, key),
        ).fetchone()
        return row[0] if row else None
    
    def hgetall(self, name: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM state WHERE namespace = ?",
            (name,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    
    def delete(self, name: str) -> None:
        self._conn.execute("DELETE FROM state WHERE namespace = ?", (name,))


@dataclass
class SwarmState:
    """Helper for managing swarm state."""
    
    backend: SqliteBackend
    pr_number: str
    
    @property
    def key(self) -> str:
        return f"duo:{self.pr_number}"
    
    def set(self, field: str, value: str) -> None:
        self.backend.hset(self.key, field, value)
    
    def get(self, field: str) -> str | None:
        return self.backend.hget(self.key, field)
    
    def get_all(self) -> dict[str, str]:
        return self.backend.hgetall(self.key)
    
    def set_agent(self, name: str, **fields: str) -> None:
        """Set multiple fields for an agent."""
        for field, value in fields.items():
            self.set(f"{name}:{field}", value)
    
    def get_agent(self, name: str) -> dict[str, str | None]:
        """Get all fields for an agent."""
        return {
            "session": self.get(f"{name}:session"),
            "fifo": self.get(f"{name}:fifo"),
            "pid": self.get(f"{name}:pid"),
            "log": self.get(f"{name}:log"),
            "model": self.get(f"{name}:model"),
        }
    
    def init(self, repo: str, branch: str, base: str, runner: str = "sdk") -> None:
        """Initialize swarm state."""
        self.set("repo", repo)
        self.set("pr", self.pr_number)
        self.set("branch", branch)
        self.set("base", base)
        self.set("runner", runner)
        self.set("stage", "1")
        self.set("started_at", str(int(time.time())))
    
    def delete(self) -> None:
        """Delete all swarm state."""
        self.backend.delete(self.key)
