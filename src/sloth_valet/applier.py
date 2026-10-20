"""Journaled, collision-resistant moves and cautious undo."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sloth_valet.config import ValetConfig
from sloth_valet.journal import Journal, JournalError
from sloth_valet.models import MoveRecord, MoveStatus, PlannedMove
from sloth_valet.planner import Plan


class MoveError(RuntimeError):
    """A planned move could not be completed safely."""


class ApplyStatus(StrEnum):
    MOVED = "moved"
    REVIEWED = "reviewed"
    PLANNED = "planned"
    FAILED = "failed"


class UndoStatus(StrEnum):
    RESTORED = "restored"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    move: PlannedMove
    status: ApplyStatus
    journal_id: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UndoOutcome:
    move: MoveRecord
    status: UndoStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PendingRepair:
    move_id: int
    status: MoveStatus
    detail: str


class Applier:
    """Apply plans with revalidation, hashing, journaling, and no overwrites."""

    def __init__(self, config: ValetConfig, journal: Journal) -> None:
        self._config = config
        self._journal = journal
        self._watch_roots = tuple(
            _resolve(watch.path) for watch in config.watch
        )
        self._destination_roots = tuple(
            _resolve(watch.destination_root) for watch in config.watch
        )

    def preview(self, plan: Plan) -> tuple[MoveOutcome, ...]:
        """Return the planned effect without writing journal rows or moving files."""
        return tuple(MoveOutcome(move=move, status=ApplyStatus.PLANNED) for move in plan.moves)

    def apply(self, plan: Plan, run_id: str) -> tuple[MoveOutcome, ...]:
        outcomes: list[MoveOutcome] = []
        for move in plan.moves:
            outcomes.append(self._apply_one(move, run_id))
        return tuple(outcomes)

    def _apply_one(self, move: PlannedMove, run_id: str) -> MoveOutcome:
        try:
            self._validate_source(move)
            self._prepare_destination(move)
            digest, size = _sha256_and_size(move.source)
            if size != move.candidate.size:
                raise MoveError("source size changed after scanning")
        except (OSError, MoveError) as exc:
            return MoveOutcome(move=move, status=ApplyStatus.FAILED, error=str(exc))

        try:
            journal_id = self._journal.record_pending(
                run_id,
                source=move.source,
                destination=move.destination,
                sha256=digest,
                size=size,
                is_review=move.is_review,
            )
        except JournalError as exc:
            return MoveOutcome(move=move, status=ApplyStatus.FAILED, error=str(exc))

        try:
            self._move_file(
                move.source,
                move.destination,
                expected_hash=digest,
                expected_size=size,
            )
        except (OSError, MoveError) as exc:
            with contextlib.suppress(JournalError):
                self._journal.mark_failed(journal_id, str(exc))
            return MoveOutcome(
                move=move,
                status=ApplyStatus.FAILED,
                journal_id=journal_id,
                error=str(exc),
            )

        try:
            self._journal.mark_done(journal_id)
        except JournalError as exc:
            # The file moved successfully. Leave the pending row for reconciliation
            # rather than claiming failure or writing a contradictory state.
            return MoveOutcome(
                move=move,
                status=ApplyStatus.FAILED,
                journal_id=journal_id,
                error=f"move completed but journal update failed: {exc}",
            )

        status = ApplyStatus.REVIEWED if move.is_review else ApplyStatus.MOVED
        return MoveOutcome(move=move, status=status, journal_id=journal_id)

    def reconcile_pending(self) -> tuple[PendingRepair, ...]:
        """Resolve pending rows left by a crash without deleting any user file.

        A completed rename is marked done when the destination exists and hashes
        correctly. Every ambiguous case is marked failed with an explanation and
        leaves both copies for the user. The next scan can safely retry a source that
        is still present.
        """
        repairs: list[PendingRepair] = []
        for record in self._journal.pending_moves():
            repairs.append(self._reconcile_one(record))
        return tuple(repairs)

    def _reconcile_one(self, record: MoveRecord) -> PendingRepair:
        if record.id is None:
            raise JournalError("pending journal row has no id")
        source_exists = _is_regular_file(record.source)
        destination_exists = _is_regular_file(record.destination)
        expected_hash = record.sha256

        if not source_exists and destination_exists and expected_hash is not None:
            try:
                destination_hash = sha256_file(record.destination)
            except OSError as exc:
                detail = f"destination exists but cannot be hashed: {exc}"
                self._journal.mark_failed(record.id, detail)
                return PendingRepair(record.id, MoveStatus.FAILED, detail)
            if destination_hash == expected_hash:
                self._journal.mark_done(record.id)
                return PendingRepair(
                    record.id,
                    MoveStatus.DONE,
                    "destination verified; completed an interrupted move",
                )
            detail = "destination exists but its hash does not match the journal"
        elif source_exists and not destination_exists:
            detail = "source retained; the move did not complete and can be retried"
        elif source_exists and destination_exists:
            detail = (
                "source and destination both exist after an interrupted move; "
                "inspect both files before retrying"
            )
        else:
            detail = "source and destination are both missing; manual inspection needed"

        self._journal.mark_failed(record.id, detail)
        return PendingRepair(record.id, MoveStatus.FAILED, detail)

    def undo_run(self, run_id: str) -> tuple[UndoOutcome, ...]:
        """Restore done moves in reverse order when every safety check still holds."""
        records = self._journal.moves_for_run(
            run_id, statuses=(MoveStatus.DONE,)
        )
        outcomes: list[UndoOutcome] = []
        for record in reversed(records):
            outcomes.append(self._undo_one(record))
        return tuple(outcomes)

    def _undo_one(self, record: MoveRecord) -> UndoOutcome:
        if record.id is None or record.sha256 is None or record.size is None:
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "journal row is missing required undo data",
            )
        if not self._within_configured_roots(record):
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "paths are no longer inside a configured watch/destination root",
            )
        if not _is_regular_file(record.destination):
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "destination is missing or is not a regular file",
            )
        try:
            if record.destination.resolve(strict=True) != record.destination:
                return UndoOutcome(
                    record,
                    UndoStatus.SKIPPED,
                    "destination path now traverses a symlink",
                )
            destination_hash = sha256_file(record.destination)
        except (OSError, RuntimeError) as exc:
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                f"cannot verify destination: {exc}",
            )
        if destination_hash != record.sha256:
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "destination hash no longer matches the journal",
            )
        if os.path.lexists(record.source):
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "original path is occupied; refusing to overwrite",
            )
        if not record.source.parent.is_dir() or record.source.parent.is_symlink():
            return UndoOutcome(
                record,
                UndoStatus.SKIPPED,
                "original directory is missing or is a symlink",
            )

        try:
            self._move_file(
                record.destination,
                record.source,
                expected_hash=record.sha256,
                expected_size=record.size,
            )
        except (OSError, MoveError) as exc:
            return UndoOutcome(record, UndoStatus.SKIPPED, f"restore failed: {exc}")

        try:
            self._journal.mark_reverted(record.id)
        except JournalError as exc:
            return UndoOutcome(
                record,
                UndoStatus.RESTORED,
                f"file restored but journal update failed: {exc}",
            )
        return UndoOutcome(record, UndoStatus.RESTORED)

    def _within_configured_roots(self, record: MoveRecord) -> bool:
        source = _resolve(record.source)
        destination = _resolve(record.destination)
        return (
            any(source.is_relative_to(root) for root in self._watch_roots)
            and any(
                destination.is_relative_to(root)
                for root in self._destination_roots
            )
        )

    def _validate_source(self, move: PlannedMove) -> None:
        source = move.source
        try:
            source_stat = source.lstat()
        except OSError as exc:
            raise MoveError(f"cannot stat source: {exc}") from exc
        if stat.S_ISLNK(source_stat.st_mode):
            raise MoveError("source became a symlink")
        if not stat.S_ISREG(source_stat.st_mode):
            raise MoveError("source is no longer a regular file")
        if not move.candidate.matches_stat(source_stat):
            raise MoveError("source size, mtime, device, or inode changed after scanning")
        try:
            resolved = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MoveError(f"cannot resolve source: {exc}") from exc
        if resolved != source:
            raise MoveError("source path now traverses a symlink")
        watch_root = _resolve(move.candidate.watch_root)
        if not resolved.is_relative_to(watch_root):
            raise MoveError("source escaped its configured watch root")
        if not any(resolved.is_relative_to(root) for root in self._watch_roots):
            raise MoveError("source is outside all configured watch roots")

    def _prepare_destination(self, move: PlannedMove) -> None:
        destination_root = _resolve(move.candidate.destination_root)
        if not any(
            destination_root.is_relative_to(root)
            for root in self._destination_roots
        ):
            raise MoveError("destination root is not configured")
        parent = move.destination.parent
        if not _is_within(parent, destination_root):
            raise MoveError("destination escapes the configured destination root")
        try:
            parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MoveError(f"cannot create destination directory: {exc}") from exc
        if not resolved_parent.is_relative_to(destination_root):
            raise MoveError("destination directory resolves outside its root")
        if move.destination.name in {"", ".", ".."}:
            raise MoveError("destination has an unsafe name")
        if move.destination == move.source:
            raise MoveError("source and destination are the same path")
        if os.path.lexists(move.destination):
            raise MoveError("destination appeared after planning; refusing to overwrite")

    def _move_file(
        self,
        source: Path,
        destination: Path,
        *,
        expected_hash: str,
        expected_size: int,
    ) -> None:
        if os.path.lexists(destination):
            raise MoveError("destination already exists; refusing to overwrite")
        try:
            os.rename(source, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise MoveError(f"rename failed: {exc}") from exc
            self._copy_verify_and_remove(
                source,
                destination,
                expected_hash=expected_hash,
                expected_size=expected_size,
            )
            return
        _best_effort_fsync_directory(destination.parent)

    def _copy_verify_and_remove(
        self,
        source: Path,
        destination: Path,
        *,
        expected_hash: str,
        expected_size: int,
    ) -> None:
        created = False
        try:
            with source.open("rb") as source_file, destination.open("xb") as output_file:
                created = True
                shutil.copyfileobj(source_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
        except FileExistsError as exc:
            raise MoveError("destination appeared during copy; refusing to overwrite") from exc
        except OSError as exc:
            if created:
                _remove_created_file(destination)
            raise MoveError(f"cross-filesystem copy failed: {exc}") from exc

        try:
            destination_hash = sha256_file(destination)
        except OSError as exc:
            _remove_created_file(destination)
            raise MoveError(f"cannot verify copied file: {exc}") from exc
        if destination_hash != expected_hash:
            _remove_created_file(destination)
            raise MoveError("copied file failed SHA-256 verification")

        try:
            if destination.stat().st_size != expected_size:
                _remove_created_file(destination)
                raise MoveError("copied file has the wrong size")
            os.unlink(source)
        except OSError as exc:
            raise MoveError(
                "copy verified but source could not be removed; both files remain"
            ) from exc
        _best_effort_fsync_directory(destination.parent)
        _best_effort_fsync_directory(source.parent)


def sha256_file(path: Path) -> str:
    return _sha256_and_size(path)[0]


def _sha256_and_size(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _is_regular_file(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode)


def _remove_created_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _best_effort_fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _is_within(path: Path, root: Path) -> bool:
    return _resolve(path).is_relative_to(root)
