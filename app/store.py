from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import AddRequest


@dataclass(frozen=True)
class MemoryChunk:
    id: str
    user_id: str
    session_id: str
    request_id: str
    sequence: int
    messages: tuple[dict, ...]
    timestamp_ms: int | None
    created_at: str

    @property
    def content(self) -> str:
        lines: list[str] = []
        for message in self.messages:
            role = str(message.get("role", "unknown")).upper()
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n\n".join(lines)


def _memory_id(user_id: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}\0{request_id}".encode()).hexdigest()
    return f"mem_{digest[:24]}"


def _payload_hash(request: AddRequest) -> str:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConflictingRequestError(ValueError):
    pass


class MemoryStore:
    """SQLite-backed, idempotent storage scoped strictly by ``user_id``."""

    def __init__(self, path: Path, retention_days: int = 30):
        self.path = path
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingestions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ingestions_user
                    ON ingestions(user_id, sequence);

                CREATE TABLE IF NOT EXISTS messages (
                    ingestion_sequence INTEGER NOT NULL,
                    message_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp_ms INTEGER,
                    PRIMARY KEY (ingestion_sequence, message_index),
                    FOREIGN KEY (ingestion_sequence)
                        REFERENCES ingestions(sequence) ON DELETE CASCADE
                );
                """
            )
        self.delete_expired()

    def add(self, request: AddRequest) -> bool:
        """Synchronously persist all messages. Returns False for an exact retry."""
        digest = _payload_hash(request)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._write_lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_hash FROM ingestions WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise ConflictingRequestError(
                        "request_id already exists with a different payload"
                    )
                return False

            cursor = connection.execute(
                """
                INSERT INTO ingestions
                    (request_id, user_id, session_id, memory_id, payload_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.user_id,
                    request.session_id,
                    _memory_id(request.user_id, request.request_id),
                    digest,
                    now,
                ),
            )
            sequence = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO messages
                    (ingestion_sequence, message_index, role, content, timestamp_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (sequence, index, message.role, message.content, message.timestamp)
                    for index, message in enumerate(request.messages)
                ],
            )
        return True

    def list_ingested_chunks(self, user_id: str) -> list[MemoryChunk]:
        """Return one candidate per Add request for one exact user.

        Message order inside each Add payload is preserved. No neighboring Add
        requests are merged or expanded, matching the evaluated Jev-only setup.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.sequence, i.request_id, i.user_id, i.session_id,
                       i.memory_id, i.created_at, m.message_index, m.role,
                       m.content, m.timestamp_ms
                FROM ingestions AS i
                JOIN messages AS m ON m.ingestion_sequence = i.sequence
                WHERE i.user_id = ?
                ORDER BY i.sequence, m.message_index
                """,
                (user_id,),
            ).fetchall()

        grouped: dict[int, dict] = {}
        for row in rows:
            sequence = int(row["sequence"])
            entry = grouped.setdefault(
                sequence,
                {
                    "id": str(row["memory_id"]),
                    "user_id": str(row["user_id"]),
                    "session_id": str(row["session_id"]),
                    "request_id": str(row["request_id"]),
                    "sequence": sequence,
                    "messages": [],
                    "timestamps": [],
                    "created_at": str(row["created_at"]),
                },
            )
            timestamp = (
                int(row["timestamp_ms"]) if row["timestamp_ms"] is not None else None
            )
            entry["messages"].append(
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "timestamp": timestamp,
                }
            )
            if timestamp is not None:
                entry["timestamps"].append(timestamp)

        return [
            MemoryChunk(
                id=entry["id"],
                user_id=entry["user_id"],
                session_id=entry["session_id"],
                request_id=entry["request_id"],
                sequence=entry["sequence"],
                messages=tuple(entry["messages"]),
                timestamp_ms=(
                    min(entry["timestamps"]) if entry["timestamps"] else None
                ),
                created_at=entry["created_at"],
            )
            for entry in grouped.values()
        ]

    def delete_expired(self) -> int:
        """Delete evaluation data older than the configured, at-most-30-day window."""
        modifier = f"-{self.retention_days} days"
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ingestions WHERE julianday(created_at) < julianday('now', ?)",
                (modifier,),
            )
            return max(cursor.rowcount, 0)

    def count_users(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT user_id) AS count FROM ingestions"
            ).fetchone()
        return int(row["count"])
