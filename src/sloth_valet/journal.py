"""SQLite journal: pending before every move, done after, and recoverable runs."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from sloth_valet.models import MoveRecord, MoveStatus


class JournalError(RuntimeError):
    """Raised when the journal cannot represent the state the caller requested."""


class RunMode(StrEnum):
    DRY_RUN = "dry-run"
    APPLY = "apply"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One invocation of the pipeline, including dry runs."""

    id: str
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    mode: RunMode
    moved_count: int
    review_count: int
    skipped_count: int
    failed_count: int
    report_path: Path | None = None


class Journal:
    """Small, synchronous SQLite journal with explicit state transitions."""

    def __init__(self, path: Path) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    moved_count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    report_path TEXT
                );

                CREATE TABLE IF NOT EXISTS moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    is_review INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS moves_run_id_index ON moves(run_id);
                CREATE INDEX IF NOT EXISTS moves_status_index ON moves(status);
                """
            )
            run_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(runs)")
            }
            if "report_path" not in run_columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN report_path TEXT")

    def start_run(self, mode: RunMode, *, now: datetime | None = None) -> str:
        run_id = uuid.uuid4().hex
        timestamp = _timestamp(now)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (id, started_at, status, mode)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, timestamp, RunStatus.RUNNING.value, mode.value),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        moved_count: int = 0,
        review_count: int = 0,
        skipped_count: int = 0,
        failed_count: int = 0,
        now: datetime | None = None,
    ) -> None:
        if status is RunStatus.RUNNING:
            raise JournalError("a finished run cannot still be running")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, moved_count = ?, review_count = ?,
                    skipped_count = ?, failed_count = ?
                WHERE id = ?
                """,
                (
                    _timestamp(now),
                    status.value,
                    moved_count,
                    review_count,
                    skipped_count,
                    failed_count,
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise JournalError(f"run {run_id!r} does not exist")

    def record_pending(
        self,
        run_id: str,
        *,
        source: Path,
        destination: Path,
        sha256: str,
        size: int,
        is_review: bool = False,
        now: datetime | None = None,
    ) -> int:
        timestamp = _timestamp(now)
        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT INTO moves (
                        run_id, source, destination, sha256, size, status,
                        created_at, is_review
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(source),
                        str(destination),
                        sha256,
                        size,
                        MoveStatus.PENDING.value,
                        timestamp,
                        int(is_review),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise JournalError(f"run {run_id!r} does not exist") from exc
        if cursor.lastrowid is None:
            raise JournalError("SQLite did not return a move id")
        return int(cursor.lastrowid)

    def mark_done(self, move_id: int, *, now: datetime | None = None) -> None:
        self._transition(move_id, MoveStatus.DONE, now=now)

    def mark_reverted(self, move_id: int, *, now: datetime | None = None) -> None:
        self._transition(move_id, MoveStatus.REVERTED, now=now)

    def mark_failed(
        self,
        move_id: int,
        error: str,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE moves SET status = ?, completed_at = ?, error = ?
                WHERE id = ?
                """,
                (MoveStatus.FAILED.value, _timestamp(now), error, move_id),
            )
        if cursor.rowcount != 1:
            raise JournalError(f"move {move_id!r} does not exist")

    def _transition(
        self,
        move_id: int,
        status: MoveStatus,
        *,
        now: datetime | None,
    ) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE moves SET status = ?, completed_at = ?, error = NULL
                WHERE id = ?
                """,
                (status.value, _timestamp(now), move_id),
            )
        if cursor.rowcount != 1:
            raise JournalError(f"move {move_id!r} does not exist")

    def set_report_path(
        self,
        run_id: str,
        report_path: Path,
    ) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE runs SET report_path = ? WHERE id = ?",
                (str(report_path), run_id),
            )
        if cursor.rowcount != 1:
            raise JournalError(f"run {run_id!r} does not exist")

    def get_move(self, move_id: int) -> MoveRecord | None:
        row = self._connection.execute(
            "SELECT * FROM moves WHERE id = ?", (move_id,)
        ).fetchone()
        return _move_from_row(row) if row is not None else None

    def moves_for_run(
        self,
        run_id: str,
        *,
        statuses: tuple[MoveStatus, ...] | None = None,
    ) -> tuple[MoveRecord, ...]:
        query = "SELECT * FROM moves WHERE run_id = ?"
        parameters: list[object] = [run_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            parameters.extend(status.value for status in statuses)
        query += " ORDER BY id"
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_move_from_row(row) for row in rows)

    def pending_moves(self) -> tuple[MoveRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM moves WHERE status = ? ORDER BY id",
            (MoveStatus.PENDING.value,),
        ).fetchall()
        return tuple(_move_from_row(row) for row in rows)

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _run_from_row(row) if row is not None else None

    def last_run(
        self,
        *,
        with_done_moves: bool = False,
        with_report: bool = False,
    ) -> RunRecord | None:
        conditions: list[str] = []
        parameters: list[object] = []
        if with_done_moves:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM moves AS m "
                "WHERE m.run_id = r.id AND m.status = ?"
                ")"
            )
            parameters.append(MoveStatus.DONE.value)
        if with_report:
            conditions.append("r.report_path IS NOT NULL")
        query = "SELECT r.* FROM runs AS r"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1"
        row = self._connection.execute(query, parameters).fetchone()
        return _run_from_row(row) if row is not None else None

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _timestamp(value: datetime | None) -> str:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _move_from_row(row: sqlite3.Row) -> MoveRecord:
    return MoveRecord(
        source=Path(row["source"]),
        destination=Path(row["destination"]),
        status=MoveStatus(row["status"]),
        created_at=_parse_timestamp(row["created_at"]),
        completed_at=(
            _parse_timestamp(row["completed_at"])
            if row["completed_at"] is not None
            else None
        ),
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        sha256=str(row["sha256"]),
        size=int(row["size"]),
        error=row["error"],
        is_review=bool(row["is_review"]),
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=str(row["id"]),
        started_at=_parse_timestamp(row["started_at"]),
        finished_at=(
            _parse_timestamp(row["finished_at"])
            if row["finished_at"] is not None
            else None
        ),
        status=RunStatus(row["status"]),
        mode=RunMode(row["mode"]),
        moved_count=int(row["moved_count"]),
        review_count=int(row["review_count"]),
        skipped_count=int(row["skipped_count"]),
        failed_count=int(row["failed_count"]),
        report_path=(
            Path(row["report_path"]) if row["report_path"] is not None else None
        ),
    )
