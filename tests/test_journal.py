from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sloth_valet.journal import Journal, JournalError, RunMode, RunStatus
from sloth_valet.models import MoveStatus

FIXED_TIME = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)


def test_pending_then_done_records_every_required_field(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state" / "journal.sqlite3")
    run_id = journal.start_run(RunMode.APPLY, now=FIXED_TIME)
    move_id = journal.record_pending(
        run_id,
        source=tmp_path / "watch" / "invoice.pdf",
        destination=tmp_path / "dest" / "finance" / "invoice.pdf",
        sha256="a" * 64,
        size=123,
        is_review=True,
        now=FIXED_TIME,
    )
    journal.mark_done(move_id, now=FIXED_TIME)

    record = journal.get_move(move_id)

    assert record is not None
    assert record.run_id == run_id
    assert record.source == tmp_path / "watch" / "invoice.pdf"
    assert record.destination == tmp_path / "dest" / "finance" / "invoice.pdf"
    assert record.sha256 == "a" * 64
    assert record.size == 123
    assert record.status is MoveStatus.DONE
    assert record.is_review is True
    assert record.created_at == FIXED_TIME
    assert record.completed_at == FIXED_TIME
    journal.close()


def test_finish_run_counts_and_last_run_with_done_moves(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    first = journal.start_run(RunMode.APPLY, now=FIXED_TIME)
    first_move = journal.record_pending(
        first,
        source=tmp_path / "a",
        destination=tmp_path / "b",
        sha256="b" * 64,
        size=1,
        now=FIXED_TIME,
    )
    journal.mark_done(first_move, now=FIXED_TIME)
    journal.finish_run(
        first,
        moved_count=1,
        skipped_count=2,
        now=FIXED_TIME,
    )
    second = journal.start_run(RunMode.DRY_RUN, now=FIXED_TIME.replace(hour=4))
    journal.finish_run(second, skipped_count=3, now=FIXED_TIME.replace(hour=4))

    record = journal.get_run(first)
    last_with_done = journal.last_run(with_done_moves=True)
    last = journal.last_run()

    assert record is not None
    assert record.status is RunStatus.COMPLETED
    assert record.mode is RunMode.APPLY
    assert record.moved_count == 1
    assert record.skipped_count == 2
    assert record.finished_at == FIXED_TIME
    assert last_with_done is not None and last_with_done.id == first
    assert last is not None and last.id == second
    journal.close()


def test_failed_and_reverted_transitions(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    run_id = journal.start_run(RunMode.APPLY)
    failed = journal.record_pending(
        run_id,
        source=tmp_path / "a",
        destination=tmp_path / "b",
        sha256="c" * 64,
        size=1,
    )
    reverted = journal.record_pending(
        run_id,
        source=tmp_path / "c",
        destination=tmp_path / "d",
        sha256="d" * 64,
        size=1,
    )

    journal.mark_failed(failed, "disk full")
    journal.mark_reverted(reverted)

    failed_record = journal.get_move(failed)
    reverted_record = journal.get_move(reverted)
    assert failed_record is not None and failed_record.status is MoveStatus.FAILED
    assert failed_record.error == "disk full"
    assert reverted_record is not None and reverted_record.status is MoveStatus.REVERTED
    journal.close()


def test_pending_moves_are_listed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    run_id = journal.start_run(RunMode.APPLY)
    move_id = journal.record_pending(
        run_id,
        source=tmp_path / "a",
        destination=tmp_path / "b",
        sha256="e" * 64,
        size=1,
    )

    pending = journal.pending_moves()

    assert [record.id for record in pending] == [move_id]
    journal.close()


def test_report_path_round_trip_and_last_run_with_report(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")
    first = journal.start_run(RunMode.DRY_RUN, now=FIXED_TIME)
    second = journal.start_run(RunMode.APPLY, now=FIXED_TIME.replace(hour=4))
    journal.finish_run(first, now=FIXED_TIME)
    journal.finish_run(second, now=FIXED_TIME.replace(hour=4))
    report = tmp_path / "reports" / "first.md"
    journal.set_report_path(first, report)

    record = journal.get_run(first)
    last_with_report = journal.last_run(with_report=True)

    assert record is not None
    assert record.report_path == report
    assert last_with_report is not None and last_with_report.id == first
    journal.close()


def test_phase_five_journal_is_migrated_with_a_report_path_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            moved_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, NULL, ?, ?, 0, 0, 0, 0)",
        ("old", FIXED_TIME.isoformat(), "completed", "apply"),
    )
    connection.commit()
    connection.close()

    with Journal(path) as journal:
        journal.set_report_path("old", tmp_path / "old.md")
        record = journal.get_run("old")

    assert record is not None
    assert record.report_path == tmp_path / "old.md"


def test_unknown_run_is_rejected(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.sqlite3")

    with pytest.raises(JournalError):
        journal.record_pending(
            "missing",
            source=tmp_path / "a",
            destination=tmp_path / "b",
            sha256="f" * 64,
            size=1,
        )
    with pytest.raises(JournalError):
        journal.finish_run("missing")
    journal.close()
