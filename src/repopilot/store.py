from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import AuditEvent, RunRecord, utc_now


class RunStore:
    """Small SQLite event store used by the API, CLI and evaluation runner."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id);
                """
            )

    def save_run(self, run: RunRecord) -> RunRecord:
        run.updated_at = utc_now()
        payload = run.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at
                """,
                (run.id, payload, run.created_at.isoformat(), run.updated_at.isoformat()),
            )
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM runs ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [RunRecord.model_validate_json(row[0]) for row in rows]

    def add_event(self, event: AuditEvent) -> AuditEvent:
        with self._lock, self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=?", (event.run_id,)
            ).fetchone()[0]
            event.sequence = count + 1
            connection.execute(
                "INSERT INTO events(run_id, data, created_at) VALUES (?, ?, ?)",
                (event.run_id, event.model_dump_json(), event.created_at.isoformat()),
            )
        return event

    def events(self, run_id: str, after: int = 0) -> list[AuditEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM events WHERE run_id=? ORDER BY id LIMIT -1 OFFSET ?",
                (run_id, max(0, after)),
            ).fetchall()
        return [AuditEvent.model_validate_json(row[0]) for row in rows]
