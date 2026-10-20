"""Pure planning: turn validated classifications into collision-free destinations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sloth_valet.config import ValetConfig
from sloth_valet.models import (
    MAX_FILENAME_CHARS,
    ClassificationResult,
    FileCandidate,
    FilenameError,
    PlannedMove,
    sanitize_filename,
    sanitize_subfolder,
)


@dataclass(frozen=True, slots=True)
class ClassifiedFile:
    """A candidate plus the exact classifier outcome that produced it."""

    candidate: FileCandidate
    result: ClassificationResult


@dataclass(frozen=True, slots=True)
class PlanSkip:
    """A candidate that will not be moved and why."""

    candidate: FileCandidate
    reason: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """A fully resolved, collision-free plan built without touching disk."""

    moves: tuple[PlannedMove, ...]
    skipped: tuple[PlanSkip, ...]

    @property
    def review_moves(self) -> tuple[PlannedMove, ...]:
        return tuple(move for move in self.moves if move.is_review)


class Planner:
    """Resolve destinations and names, reserving each chosen path in-memory."""

    def __init__(self, config: ValetConfig) -> None:
        self._config = config
        self._allowed = set(config.categories.allowed)
        self._reserved: set[Path] = set()

    def plan(self, classified_files: list[ClassifiedFile]) -> Plan:
        moves: list[PlannedMove] = []
        skipped: list[PlanSkip] = []
        for classified in classified_files:
            result = classified.result
            candidate = classified.candidate
            if result.classification is not None:
                try:
                    classification_destination = self._destination(
                        candidate, result.classification.category,
                        result.classification.subfolder,
                        result.classification.new_name,
                    )
                except (FilenameError, ValueError) as exc:
                    fallback = self._review_or_skip(
                        candidate,
                        reason="invalid_destination",
                        detail=str(exc),
                    )
                    if isinstance(fallback, PlannedMove):
                        moves.append(fallback)
                    else:
                        skipped.append(fallback)
                    continue
                destination = self._available(classification_destination)
                self._reserved.add(destination)
                moves.append(
                    PlannedMove(
                        candidate=candidate,
                        classification=result.classification,
                        destination=destination,
                    )
                )
                continue

            reason = result.failure.value if result.failure is not None else "unknown_failure"
            fallback = self._review_or_skip(candidate, reason=reason, detail=result.detail)
            if isinstance(fallback, PlannedMove):
                moves.append(fallback)
            else:
                skipped.append(fallback)

        return Plan(
            moves=tuple(sorted(moves, key=lambda move: move.source)),
            skipped=tuple(sorted(skipped, key=lambda item: item.candidate.source)),
        )

    def _destination(
        self,
        candidate: FileCandidate,
        category: str,
        subfolder: str,
        new_name: str,
    ) -> Path:
        if category not in self._allowed:
            raise ValueError(f"category {category!r} is not allowlisted")
        destination_root = _resolve(candidate.destination_root)
        safe_subfolder = sanitize_subfolder(subfolder)
        safe_name = sanitize_filename(new_name, candidate.source.name)
        target_dir = destination_root / category
        if safe_subfolder:
            target_dir = target_dir / safe_subfolder
        destination = target_dir / safe_name
        if not _is_within(destination.parent, destination_root):
            raise ValueError("destination escapes the configured destination root")
        return destination

    def _review_or_skip(
        self,
        candidate: FileCandidate,
        *,
        reason: str,
        detail: str | None,
    ) -> PlannedMove | PlanSkip:
        if not self._config.general.review_on_failure:
            return PlanSkip(
                candidate=candidate,
                reason=reason,
                detail=(
                    "review_on_failure is false; file left untouched"
                    + (f" ({detail})" if detail else "")
                ),
            )

        destination_root = _resolve(candidate.destination_root)
        try:
            safe_name = sanitize_filename(candidate.source.name, candidate.source.name)
        except FilenameError:
            safe_name = sanitize_filename("review-file", candidate.source.name)
        review_dir = destination_root / self._config.general.review_folder
        destination = self._available(review_dir / safe_name)
        if not _is_within(destination.parent, destination_root):
            return PlanSkip(
                candidate=candidate,
                reason=reason,
                detail="review destination escapes the configured destination root",
            )
        self._reserved.add(destination)
        return PlannedMove(
            candidate=candidate,
            classification=None,
            destination=destination,
            review_reason=reason,
            review_detail=detail,
        )

    def _available(self, path: Path) -> Path:
        if not _occupied(path) and path not in self._reserved:
            return path

        stem = path.stem
        suffix = path.suffix
        counter = 1
        while True:
            ending = f"-{counter}"
            budget = MAX_FILENAME_CHARS - len(suffix) - len(ending)
            candidate_stem = stem[: max(1, budget)].rstrip("-") or "file"
            candidate = path.with_name(f"{candidate_stem}{ending}{suffix}")
            if not _occupied(candidate) and candidate not in self._reserved:
                return candidate
            counter += 1


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _is_within(path: Path, root: Path) -> bool:
    return _resolve(path).is_relative_to(root)


def _occupied(path: Path) -> bool:
    return os.path.lexists(path)
