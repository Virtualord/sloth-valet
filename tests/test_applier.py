from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import EnvPaths, make_file, write_config

from sloth_valet.applier import (
    Applier,
    ApplyStatus,
    MoveOutcome,
    UndoStatus,
    sha256_file,
)
from sloth_valet.journal import Journal, RunMode
from sloth_valet.models import (
    Classification,
    ClassificationFailure,
    ClassificationResult,
    FileCandidate,
    MoveStatus,
)
from sloth_valet.planner import ClassifiedFile, Planner


def _candidate(paths: EnvPaths, name: str = "invoice.pdf") -> FileCandidate:
    source = make_file(paths.watch / name, content=b"fixture content\n")
    file_stat = source.stat()
    return FileCandidate(
        source=source,
        watch_root=paths.watch,
        destination_root=paths.destination,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
    )


def _classified(candidate: FileCandidate) -> ClassifiedFile:
    return ClassifiedFile(
        candidate,
        ClassificationResult(
            classification=Classification(
                category="finance",
                subfolder="statements",
                new_name="renamed.pdf",
                confidence=0.95,
                reason="test",
            ),
            parsed_json=True,
        ),
    )


def _apply_success(
    paths: EnvPaths,
    journal: Journal,
) -> tuple[FileCandidate, Path, str, MoveOutcome]:
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    destination = plan.moves[0].destination
    run_id = journal.start_run(RunMode.APPLY)
    outcomes = Applier(paths.load(), journal).apply(plan, run_id)
    return candidate, destination, run_id, outcomes[0]


def test_same_filesystem_move_is_journaled_done(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate, destination, run_id, outcome = _apply_success(paths, journal)

    assert outcome.status is ApplyStatus.MOVED
    assert not candidate.source.exists()
    assert destination.read_text(encoding="utf-8") == "fixture content\n"
    records = journal.moves_for_run(run_id)
    assert len(records) == 1
    assert records[0].status is MoveStatus.DONE
    assert records[0].sha256 == sha256_file(destination)
    assert records[0].size == destination.stat().st_size
    assert records[0].completed_at is not None
    journal.close()


def test_pending_row_exists_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    run_id = journal.start_run(RunMode.APPLY)
    real_rename = os.rename
    observed: list[str] = []

    def checking_rename(source: Path, destination: Path) -> None:
        connection = sqlite3.connect(journal.path)
        try:
            row = connection.execute(
                "SELECT status FROM moves WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        observed.append(str(row[0]))
        real_rename(source, destination)

    monkeypatch.setattr("sloth_valet.applier.os.rename", checking_rename)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.MOVED
    assert observed == ["pending"]
    journal.close()


def test_cross_filesystem_move_copies_verifies_and_removes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    destination = plan.moves[0].destination
    run_id = journal.start_run(RunMode.APPLY)

    def cross_device(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("sloth_valet.applier.os.rename", cross_device)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.MOVED
    assert not candidate.source.exists()
    assert destination.read_text(encoding="utf-8") == "fixture content\n"
    record = journal.moves_for_run(run_id)[0]
    assert record.sha256 == sha256_file(destination)
    journal.close()


def test_cross_filesystem_hash_mismatch_removes_only_partial_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    destination = plan.moves[0].destination
    run_id = journal.start_run(RunMode.APPLY)

    def cross_device(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("sloth_valet.applier.os.rename", cross_device)
    monkeypatch.setattr("sloth_valet.applier.sha256_file", lambda _path: "0" * 64)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.FAILED
    assert candidate.source.read_text(encoding="utf-8") == "fixture content\n"
    assert not destination.exists()
    assert journal.moves_for_run(run_id)[0].status is MoveStatus.FAILED
    journal.close()


def test_rename_failure_leaves_source_and_marks_journal_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    run_id = journal.start_run(RunMode.APPLY)

    def failing_rename(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr("sloth_valet.applier.os.rename", failing_rename)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.FAILED
    assert candidate.source.exists()
    assert not plan.moves[0].destination.exists()
    record = journal.moves_for_run(run_id)[0]
    assert record.status is MoveStatus.FAILED
    assert record.error is not None
    journal.close()


def test_destination_appearing_after_planning_is_not_overwritten(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    destination = plan.moves[0].destination
    destination.parent.mkdir(parents=True)
    destination.write_text("someone else", encoding="utf-8")
    run_id = journal.start_run(RunMode.APPLY)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.FAILED
    assert candidate.source.exists()
    assert destination.read_text(encoding="utf-8") == "someone else"
    assert journal.moves_for_run(run_id) == ()
    journal.close()


def test_source_changed_after_scanning_is_refused(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    candidate.source.write_bytes(b"changed after scan")
    run_id = journal.start_run(RunMode.APPLY)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.FAILED
    assert candidate.source.read_text(encoding="utf-8") == "changed after scan"
    assert journal.moves_for_run(run_id) == ()
    journal.close()


def test_review_move_is_applied_and_marked_as_review(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths, "messy.pdf")
    failure = ClassificationResult(
        classification=None,
        failure=ClassificationFailure.LOW_CONFIDENCE,
        detail="too uncertain",
    )
    plan = Planner(paths.load()).plan([ClassifiedFile(candidate, failure)])
    run_id = journal.start_run(RunMode.APPLY)

    outcomes = Applier(paths.load(), journal).apply(plan, run_id)

    assert outcomes[0].status is ApplyStatus.REVIEWED
    assert not candidate.source.exists()
    review_file = paths.destination / "_review" / "messy.pdf"
    assert review_file.exists()
    record = journal.moves_for_run(run_id)[0]
    assert record.is_review is True
    journal.close()


def test_reconcile_pending_source_only_keeps_source_and_marks_failed(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    source = make_file(paths.watch / "pending.pdf")
    destination = paths.destination / "misc" / "pending.pdf"
    run_id = journal.start_run(RunMode.APPLY)
    move_id = journal.record_pending(
        run_id,
        source=source,
        destination=destination,
        sha256=sha256_file(source),
        size=source.stat().st_size,
    )

    repairs = Applier(paths.load(), journal).reconcile_pending()

    assert repairs[0].move_id == move_id
    assert repairs[0].status is MoveStatus.FAILED
    assert source.exists()
    assert not destination.exists()
    record = journal.get_move(move_id)
    assert record is not None and record.status is MoveStatus.FAILED
    journal.close()


def test_reconcile_completed_rename_marks_done(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    source = make_file(paths.watch / "pending.pdf")
    destination = paths.destination / "misc" / "pending.pdf"
    destination.parent.mkdir(parents=True)
    digest = sha256_file(source)
    size = source.stat().st_size
    run_id = journal.start_run(RunMode.APPLY)
    move_id = journal.record_pending(
        run_id,
        source=source,
        destination=destination,
        sha256=digest,
        size=size,
    )
    os.rename(source, destination)

    repairs = Applier(paths.load(), journal).reconcile_pending()

    assert repairs[0].status is MoveStatus.DONE
    record = journal.get_move(move_id)
    assert record is not None and record.status is MoveStatus.DONE
    journal.close()


def test_reconcile_both_copies_existing_is_ambiguous_and_keeps_both(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    source = make_file(paths.watch / "pending.pdf")
    destination = paths.destination / "misc" / "pending.pdf"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(source.read_bytes())
    run_id = journal.start_run(RunMode.APPLY)
    journal.record_pending(
        run_id,
        source=source,
        destination=destination,
        sha256=sha256_file(source),
        size=source.stat().st_size,
    )

    repairs = Applier(paths.load(), journal).reconcile_pending()

    assert repairs[0].status is MoveStatus.FAILED
    assert "both exist" in repairs[0].detail
    assert source.exists() and destination.exists()
    journal.close()


def test_crash_between_pending_and_rename_keeps_recoverable_pending_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    run_id = journal.start_run(RunMode.APPLY)

    def interrupted_rename(_source: Path, _destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("sloth_valet.applier.os.rename", interrupted_rename)

    with pytest.raises(KeyboardInterrupt):
        Applier(paths.load(), journal).apply(plan, run_id)

    pending = journal.pending_moves()
    assert len(pending) == 1
    assert candidate.source.exists()

    repairs = Applier(paths.load(), journal).reconcile_pending()
    assert repairs[0].status is MoveStatus.FAILED
    assert journal.pending_moves() == ()
    journal.close()


def test_crash_after_rename_before_done_is_completed_by_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate = _candidate(paths)
    plan = Planner(paths.load()).plan([_classified(candidate)])
    destination = plan.moves[0].destination
    run_id = journal.start_run(RunMode.APPLY)
    real_mark_done = journal.mark_done
    interrupted_once = True

    def crash_once(move_id: int, *, now: object = None) -> None:
        nonlocal interrupted_once
        if interrupted_once:
            interrupted_once = False
            raise KeyboardInterrupt
        real_mark_done(move_id)

    monkeypatch.setattr(journal, "mark_done", crash_once)

    with pytest.raises(KeyboardInterrupt):
        Applier(paths.load(), journal).apply(plan, run_id)

    assert not candidate.source.exists()
    assert destination.exists()
    assert len(journal.pending_moves()) == 1

    repairs = Applier(paths.load(), journal).reconcile_pending()

    assert repairs[0].status is MoveStatus.DONE
    record = journal.moves_for_run(run_id)[0]
    assert record.status is MoveStatus.DONE
    journal.close()


def test_undo_restores_a_verified_move(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate, destination, run_id, _ = _apply_success(paths, journal)

    outcomes = Applier(paths.load(), journal).undo_run(run_id)

    assert outcomes[0].status is UndoStatus.RESTORED
    assert candidate.source.read_text(encoding="utf-8") == "fixture content\n"
    assert not destination.exists()
    record = journal.moves_for_run(run_id)[0]
    assert record.status is MoveStatus.REVERTED
    journal.close()


def test_undo_refuses_when_destination_hash_changed(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate, destination, run_id, _ = _apply_success(paths, journal)
    destination.write_text("tampered", encoding="utf-8")

    outcomes = Applier(paths.load(), journal).undo_run(run_id)

    assert outcomes[0].status is UndoStatus.SKIPPED
    assert "hash" in (outcomes[0].reason or "")
    assert not candidate.source.exists()
    assert destination.read_text(encoding="utf-8") == "tampered"
    journal.close()


def test_undo_refuses_when_destination_is_missing(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate, destination, run_id, _ = _apply_success(paths, journal)
    destination.unlink()

    outcomes = Applier(paths.load(), journal).undo_run(run_id)

    assert outcomes[0].status is UndoStatus.SKIPPED
    assert "missing" in (outcomes[0].reason or "")
    assert not candidate.source.exists()
    journal.close()


def test_undo_refuses_to_overwrite_an_occupied_original_path(tmp_path: Path) -> None:
    paths = write_config(tmp_path)
    journal = Journal(paths.reports_dir.parent / "journal.sqlite3")
    candidate, destination, run_id, _ = _apply_success(paths, journal)
    candidate.source.write_text("new file at old path", encoding="utf-8")

    outcomes = Applier(paths.load(), journal).undo_run(run_id)

    assert outcomes[0].status is UndoStatus.SKIPPED
    assert "occupied" in (outcomes[0].reason or "")
    assert candidate.source.read_text(encoding="utf-8") == "new file at old path"
    assert destination.exists()
    journal.close()
