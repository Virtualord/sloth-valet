"""Domain objects and strict schemas for untrusted model output."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_CLASSIFICATION_BYTES = 64 * 1024
MAX_FILENAME_BYTES = 255
_EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,20}$")
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')
_CONTROL_OR_FORMAT = re.compile(r"[\x00-\x1f\x7f]")


class SkipReason(StrEnum):
    """Reasons a path was never shown to the model."""

    SYMLINK = "symlink"
    NOT_REGULAR = "not_regular"
    PROTECTED = "protected"
    IGNORED = "ignored"
    PARTIAL_DOWNLOAD = "partial_download"
    UNSETTLED = "unsettled"
    CHANGED = "changed"
    OUTSIDE_WATCH = "outside_watch"
    INSIDE_DESTINATION = "inside_destination"
    DUPLICATE = "duplicate"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FileCandidate:
    """A settled regular file that passed every pre-model safety check."""

    source: Path
    watch_root: Path
    destination_root: Path
    size: int
    mtime_ns: int
    device: int
    inode: int

    @property
    def relative_source(self) -> Path:
        return self.source.relative_to(self.watch_root)

    def matches_stat(self, stat_result: os.stat_result) -> bool:
        """Return whether a later stat still identifies the exact same file."""
        return (
            stat_result.st_size == self.size
            and stat_result.st_mtime_ns == self.mtime_ns
            and stat_result.st_dev == self.device
            and stat_result.st_ino == self.inode
        )


@dataclass(frozen=True, slots=True)
class SkippedPath:
    path: Path
    reason: SkipReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScanError:
    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    candidates: tuple[FileCandidate, ...]
    skipped: tuple[SkippedPath, ...]
    errors: tuple[ScanError, ...]


class Classification(BaseModel):
    """The only shape accepted from the local model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: str = Field(min_length=1, max_length=60)
    subfolder: str = Field(max_length=100)
    new_name: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(max_length=500)


@dataclass(frozen=True, slots=True)
class PlannedMove:
    """A validated candidate paired with its intended destination."""

    candidate: FileCandidate
    classification: Classification
    destination: Path

    @property
    def source(self) -> Path:
        return self.candidate.source


class MoveStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    REVERTED = "reverted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MoveRecord:
    """One journaled move, pending before the operation and done afterwards."""

    source: Path
    destination: Path
    status: MoveStatus
    created_at: datetime
    completed_at: datetime | None = None
    id: int | None = None


class ClassificationError(ValueError):
    """Raised when model output is not a valid classification."""


class FilenameError(ValueError):
    """Raised when model output cannot be reduced to a safe filename."""


def parse_classification_json(payload: str) -> Classification:
    """Parse exactly one JSON classification without repairing model output."""
    if len(payload.encode("utf-8")) > MAX_CLASSIFICATION_BYTES:
        raise ClassificationError("model output exceeds the classification size limit")
    try:
        return Classification.model_validate_json(payload)
    except ValidationError as exc:
        raise ClassificationError(
            "model output does not match the classification schema"
        ) from exc


def _truncate_utf8(value: str, max_bytes: int) -> str:
    while len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value


def sanitize_filename(raw: str, original_name: str) -> str:
    """Turn model-proposed text into one safe path component.

    Path separators, control characters, hidden-file prefixes, and Windows-reserved
    characters are rejected or removed. The source file's extension is retained when
    the model omits or changes it, and the result is capped at 255 UTF-8 bytes.
    """
    if not isinstance(raw, str):
        raise FilenameError("filename must be a string")

    normalized = unicodedata.normalize("NFKC", raw).strip()
    if not normalized:
        raise FilenameError("filename is empty")
    if "/" in normalized or "\\" in normalized:
        raise FilenameError("filename contains a path separator")
    if _CONTROL_OR_FORMAT.search(normalized):
        raise FilenameError("filename contains control characters")

    normalized = "".join(
        "_" if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    normalized = _UNSAFE_FILENAME_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.lstrip(". ")
    normalized = normalized.rstrip(". ")
    if not normalized or normalized in {".", ".."}:
        raise FilenameError("filename has no usable characters")

    original_suffix = Path(original_name).suffix
    if not _EXTENSION_PATTERN.fullmatch(original_suffix):
        original_suffix = ""

    proposed_suffix = Path(normalized).suffix
    if original_suffix and proposed_suffix.casefold() != original_suffix.casefold():
        normalized = f"{normalized}{original_suffix}"

    suffix = Path(normalized).suffix
    stem = normalized[: -len(suffix)] if suffix else normalized
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= MAX_FILENAME_BYTES:
        raise FilenameError("filename extension is too long")

    stem = _truncate_utf8(stem.rstrip(". "), MAX_FILENAME_BYTES - suffix_bytes).rstrip(". ")
    if not stem:
        stem = "file"
    return f"{stem}{suffix}"
